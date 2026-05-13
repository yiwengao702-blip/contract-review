# 文件位置: contract-review/services/feature_service.py
# 功能说明: 实时风控特征计算。
#          输入一条事件 + 近期历史事件 + 用户画像，输出 FeatureSnapshot（特征快照）。
#          特征分两类：行为频次类（注册/登录/下单/支付/营销参与次数）与设备环境类
#          （模拟器 / 代理 / VPN / 秒拨 / IP 风险分 / 账号年龄 / 设备聚集度等）。
#          规则引擎只认 FeatureSnapshot，从而与「事件长什么样」解耦。
from __future__ import annotations

import time
from typing import Dict, List

from core.risk_models import EventType, FeatureSnapshot, RiskEvent, UserProfile

# 行为频次统计的窗口说明（本服务对传入的 events 视为「窗口内的近期事件」，
# 由流水线负责只喂入近窗口的事件，这里不重复做时间过滤，保证可复现、可测试）。
_WINDOW_DESC = "近 1 小时（近期事件窗口）"


def _days_between(first_seen: str) -> int:
    """从 now 到首次出现时间的天数；解析失败返回 0（视为新号）。"""
    try:
        t = time.mktime(time.strptime(first_seen, "%Y-%m-%d %H:%M:%S"))
        return max(0, int((time.time() - t) / 86400))
    except Exception:
        return 0


class FeatureService:
    """特征计算服务（无状态：每次调用都基于传入的近期事件与画像重新计算）。"""

    def compute(self, event: RiskEvent,
                recent_events: List[RiskEvent],
                profiles: Dict[str, UserProfile]) -> FeatureSnapshot:
        uid = event.user_id
        dev = event.device

        # —— 行为频次：在近期事件窗口内，按用户维度统计各类型次数 —— #
        reg = login = login_fail = order = pay = mkt = 0
        evidence: List[str] = []
        for e in recent_events:
            if e.user_id != uid:
                continue
            evidence.append(e.event_id)
            et = e.event_type
            if et == EventType.REGISTER:
                reg += 1
            elif et == EventType.LOGIN:
                login += 1
                # 登录失败次数从 payload.login_result / fail_times 推断
                if str(e.payload.get("login_result", "")).lower() in ("fail", "failed", "0"):
                    login_fail += 1
                elif isinstance(e.payload.get("fail_times"), int):
                    login_fail += e.payload["fail_times"]
            elif et == EventType.ORDER:
                order += 1
            elif et == EventType.PAYMENT:
                pay += 1
            elif et == EventType.MARKETING:
                mkt += 1

        # —— 设备环境特征：直接取当前事件的 device —— #
        is_emu = bool(dev.is_emulator) if dev else False
        is_proxy = bool(dev.is_proxy) if dev else False
        is_vpn = bool(dev.is_vpn) if dev else False
        is_rot = bool(dev.is_ip_rotation) if dev else False
        ip = dev.ip if dev else None
        device_id = dev.device_id if dev else None

        # IP 风险分：代理/秒拨/VPN 任一命中即显著抬高；再加归属地异常（境外/非常用）
        ip_risk = 0.0
        if is_proxy:
            ip_risk += 0.35
        if is_vpn:
            ip_risk += 0.30
        if is_rot:
            ip_risk += 0.35
        if dev and dev.ip_location:
            loc = dev.ip_location
            if any(k in loc for k in ("境外", "海外", "未知", "数据中心", "IDC")):
                ip_risk += 0.25
        ip_risk = min(1.0, ip_risk)

        # —— 账号与设备聚集度 —— #
        profile = profiles.get(uid)
        account_age = _days_between(profile.first_seen) if profile and profile.first_seen else 0

        # 设备聚集度：该 device_id 在近期事件里绑定了多少不同账号（设备农场信号）
        device_bind = 1
        if device_id:
            device_bind = len({e.user_id for e in recent_events
                              if e.device and e.device.device_id == device_id})
        # 账号分散度：该用户近期使用过多少设备 / IP
        distinct_dev = len({e.device.device_id for e in recent_events
                          if e.user_id == uid and e.device and e.device.device_id})
        distinct_ip = len({e.device.ip for e in recent_events
                          if e.user_id == uid and e.device and e.device.ip})

        return FeatureSnapshot(
            user_id=uid,
            device_id=device_id,
            ip=ip,
            window=_WINDOW_DESC,
            reg_count=reg,
            login_count=login,
            login_fail_times=login_fail,
            order_count=order,
            pay_count=pay,
            marketing_count=mkt,
            is_emulator=is_emu,
            is_proxy=is_proxy,
            is_vpn=is_vpn,
            is_ip_rotation=is_rot,
            ip_risk_score=round(ip_risk, 2),
            account_age_days=account_age,
            device_bind_count=max(1, device_bind),
            distinct_device_count=max(1, distinct_dev),
            distinct_ip_count=max(1, distinct_ip),
            evidence_event_ids=evidence,
        )


# 模块级单例（与项目其他服务一致：全局唯一、状态由流水线持有）
feature_service = FeatureService()
