# 文件位置: contract-review/services/event_parser.py
# 功能说明: 电商业务事件解析器。
#          把外部接入（API 或示例文件）的原始事件字典，规整为统一的 RiskEvent 结构。
#          五类事件：注册 / 登录 / 下单 / 支付 / 营销参与。
#          解析层只做「结构归一化 + 基础校验」，不做风险评估（那是规则引擎的职责）。
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

from core.risk_models import DeviceEnv, EventType, RiskEvent

# 事件类型白名单：接入层只认这几类，未知类型一律拒绝（避免脏数据污染特征计算）
_VALID_TYPES = {e.value for e in EventType}

# 设备/环境布尔字段的合法键（用于把 payload 里的 device 信息安全转成 DeviceEnv）
_DEVICE_BOOL_KEYS = ("is_emulator", "is_proxy", "is_vpn", "is_ip_rotation")


def _parse_device(raw: Optional[dict]) -> Optional[DeviceEnv]:
    """把原始 dict 安全地转成 DeviceEnv；非 dict / 空则视为无设备信息。"""
    if not isinstance(raw, dict):
        return None
    dev = DeviceEnv()
    for f in ("device_id", "ip", "ip_location", "device_type", "os", "network", "user_agent"):
        if raw.get(f) not in (None, ""):
            setattr(dev, f, str(raw[f]))
    for b in _DEVICE_BOOL_KEYS:
        v = raw.get(b)
        if isinstance(v, bool):
            setattr(dev, b, v)
        elif str(v).strip().lower() in ("1", "true", "yes", "y"):
            setattr(dev, b, True)
    return dev


def parse_event(raw: dict) -> RiskEvent:
    """解析单条原始事件；字段缺失或类型非法时抛出 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("事件必须为对象")

    etype = raw.get("event_type")
    if etype not in _VALID_TYPES:
        raise ValueError(f"未知事件类型：{etype!r}，应为 {sorted(_VALID_TYPES)}")

    user_id = str(raw.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("event.user_id 不能为空")

    # 时间戳：调用方传入则用，否则服务端补（保证特征窗口统计口径一致）
    ts = str(raw.get("timestamp") or "").strip()
    if not ts:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

    event_id = str(raw.get("event_id") or "").strip() or ("E" + uuid.uuid4().hex[:12].upper())

    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    return RiskEvent(
        event_id=event_id,
        event_type=EventType(etype),
        user_id=user_id,
        timestamp=ts,
        device=_parse_device(raw.get("device")),
        payload=payload,
    )


def parse_events(raw_list: List[dict]) -> List[RiskEvent]:
    """批量解析；返回成功解析的事件列表，并在返回里附带 skipped（被跳过的原因）。

    注意：这里不因为单条错就整体失败——真实接入大多是「一条坏数据不能拖垮整批」，
    所以采用「能解析的进管道、坏的记原因返回」的容错策略。
    """
    out: List[RiskEvent] = []
    skipped: List[Dict[str, str]] = []
    for i, raw in enumerate(raw_list):
        try:
            out.append(parse_event(raw))
        except ValueError as e:
            skipped.append({"index": str(i), "reason": str(e)})
    return out, skipped
