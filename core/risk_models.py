# 文件位置: contract-review/core/risk_models.py
# 功能说明: 电商业务事件风控子系统的核心数据模型（基于 pydantic）。
#          所有跨模块流转的结构化对象在此统一定义，前端/后端共用同一套字段约定。
#          本模块与合同审查（core/models.py）相互独立、互不影响，仅复用项目的
#          整体工程风格（dataclass 单例配置 + NodeBase 管线 + pydantic 模型）。
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------- #
# 枚举定义
# ---------------------------------------------------------------------- #
class EventType(str, Enum):
    """五类核心电商业务事件。"""
    REGISTER = "register"      # 账号注册
    LOGIN = "login"            # 账号登录
    ORDER = "order"            # 下单
    PAYMENT = "payment"        # 支付
    MARKETING = "marketing"    # 营销活动参与（领券 / 秒杀 / 拉新）


class DecisionAction(str, Enum):
    """风控决策动作。"""
    PASS = "pass"      # 放行
    REVIEW = "review"  # 转入人工审核
    BLOCK = "block"    # 拦截


class RiskLevel(str, Enum):
    """风险等级。"""
    HIGH = "high"      # 高风险
    MEDIUM = "medium"  # 中风险
    LOW = "low"        # 低风险


class CaseStatus(str, Enum):
    """风控案件状态机。"""
    PENDING = "pending"        # 待复核
    APPROVED = "approved"      # 复核通过（风险可接受 / 已处置）
    REJECTED = "rejected"      # 复核驳回（确认违规，已处置或转交）
    CLOSED = "closed"          # 已结案


# ---------------------------------------------------------------------- #
# 事件与设备环境
# ---------------------------------------------------------------------- #
class DeviceEnv(BaseModel):
    """事件携带的设备与环境信息（风控特征的直接来源）。"""
    device_id: Optional[str] = Field(default=None, description="设备指纹 / 设备唯一标识")
    ip: Optional[str] = Field(default=None, description="来源 IP")
    ip_location: Optional[str] = Field(default=None, description="IP 归属地（省市 / 国家）")
    device_type: Optional[str] = Field(default=None, description="设备类型：手机 / PC / 模拟器")
    os: Optional[str] = Field(default=None, description="操作系统及版本")
    network: Optional[str] = Field(default=None, description="网络环境：wifi / 4g / 5g / 未知")
    user_agent: Optional[str] = Field(default=None, description="User-Agent 头")
    # 以下布尔标记是判定「设备农场 / 秒拨 / 代理」的关键信号
    is_emulator: bool = Field(default=False, description="是否模拟器（沙盒 / 改机）")
    is_proxy: bool = Field(default=False, description="是否代理 IP")
    is_vpn: bool = Field(default=False, description="是否 VPN")
    is_ip_rotation: bool = Field(default=False, description="是否秒拨 / 频繁换 IP")


class RiskEvent(BaseModel):
    """一条接入的电商业务事件。"""
    event_id: str = Field(description="事件唯一 ID")
    event_type: EventType = Field(description="事件类型")
    user_id: str = Field(description="操作用户 ID")
    timestamp: str = Field(description="事件发生时间（ISO 或可读字符串）")
    device: Optional[DeviceEnv] = Field(default=None, description="设备与环境信息")
    # 业务载荷：不同事件类型关注不同字段
    #   register: { invite_code, channel, ... }
    #   login:    { login_result, fail_times, ... }
    #   order:    { order_amount, item_count, ... }
    #   payment:  { pay_amount, pay_channel, ... }
    #   marketing:{ activity_id, coupon_id, activity_type, ... }
    payload: Dict[str, object] = Field(default_factory=dict, description="业务载荷")


# ---------------------------------------------------------------------- #
# 特征快照
# ---------------------------------------------------------------------- #
class FeatureSnapshot(BaseModel):
    """针对某用户 / 设备实时计算出的风控特征快照。

    特征分为两类：
      ① 行为频次类：短时间窗口内的注册 / 登录 / 下单 / 支付 / 营销参与次数；
      ② 设备环境类：模拟器、代理、VPN、秒拨、IP 归属地异常等。
    快照在每次事件接入时基于「近期历史事件」滚动计算，是规则引擎的输入。
    """
    user_id: str = Field(description="用户 ID")
    device_id: Optional[str] = Field(default=None, description="设备 ID")
    ip: Optional[str] = Field(default=None, description="来源 IP")
    window: str = Field(default="近 1 小时", description="统计窗口说明")

    # —— 行为频次特征 ——
    reg_count: int = Field(default=0, description="窗口内注册次数")
    login_count: int = Field(default=0, description="窗口内登录次数")
    login_fail_times: int = Field(default=0, description="窗口内登录失败次数")
    order_count: int = Field(default=0, description="窗口内下单次数")
    pay_count: int = Field(default=0, description="窗口内支付次数")
    marketing_count: int = Field(default=0, description="窗口内营销参与次数")

    # —— 设备环境特征 ——
    is_emulator: bool = Field(default=False, description="是否模拟器")
    is_proxy: bool = Field(default=False, description="是否代理")
    is_vpn: bool = Field(default=False, description="是否 VPN")
    is_ip_rotation: bool = Field(default=False, description="是否秒拨 IP")
    ip_risk_score: float = Field(default=0.0, description="IP 风险分（0~1）")
    account_age_days: int = Field(default=0, description="账号年龄（天）")
    device_bind_count: int = Field(default=1, description="该设备绑定的账号数（设备农场信号）")
    distinct_device_count: int = Field(default=1, description="该账号使用过的设备数")
    distinct_ip_count: int = Field(default=1, description="该账号使用过的 IP 数")

    # 参与计算的历史事件 ID 列表（用于证据回溯）
    evidence_event_ids: List[str] = Field(default_factory=list, description="构成该快照的历史事件 ID")


# ---------------------------------------------------------------------- #
# 规则与决策
# ---------------------------------------------------------------------- #
class RuleDef(BaseModel):
    """一条风控规则定义（持久化于 data/risk_rules/risk_rules.yaml）。"""
    id: str = Field(description="规则 ID")
    name: str = Field(description="规则名称")
    category: str = Field(default="其他", description="规则分类：设备环境 / 行为频次 / 营销薅羊毛 / 账号异常 等")
    scope: List[str] = Field(default_factory=list, description="适用事件类型（空=全部）")
    # 触发条件：以「特征字段名 + 比较运算符 + 阈值」表达，规则引擎据此求值。
    # 形如：[{ "field": "reg_count", "op": ">=", "value": 5 }, ...]
    # 支持连接词 and / or（conditions 顶层用 match 指定：all=AND，any=OR）。
    match: str = Field(default="all", description="条件连接：all(AND) / any(OR)")
    conditions: List[Dict[str, object]] = Field(default_factory=list, description="条件列表")
    score: int = Field(default=0, description="命中后累加的风险分")
    level: RiskLevel = Field(default=RiskLevel.MEDIUM, description="命中后标记的风险等级")
    action: DecisionAction = Field(default=DecisionAction.REVIEW, description="命中后建议的决策动作")
    enabled: bool = Field(default=True, description="是否启用")
    advice: str = Field(default="", description="命中后给审核人的处置建议")


class RuleHit(BaseModel):
    """单条规则在一次判定中的命中明细。"""
    rule_id: str = Field(description="规则 ID")
    rule_name: str = Field(description="规则名称")
    score: int = Field(description="贡献分")
    level: RiskLevel = Field(description="风险等级")
    action: DecisionAction = Field(description="建议动作")
    matched: bool = Field(description="是否命中")
    detail: str = Field(default="", description="命中说明 / 未命中原因")


class RiskDecision(BaseModel):
    """一次事件的风控决策结论。"""
    action: DecisionAction = Field(description="最终决策：pass / review / block")
    total_score: int = Field(default=0, description="累计风险分")
    level: RiskLevel = Field(default=RiskLevel.LOW, description="综合风险等级")
    hits: List[RuleHit] = Field(default_factory=list, description="逐条规则命中明细（用于链路回溯）")
    reasons: List[str] = Field(default_factory=list, description="综合风险成因摘要")
    list_hit: Optional[str] = Field(default=None, description="命中名单类型：black / white（名单优先于规则）")


# ---------------------------------------------------------------------- #
# 名单库
# ---------------------------------------------------------------------- #
class ListEntry(BaseModel):
    """一条黑白名单记录。"""
    list_type: str = Field(description="名单类型：black / white")
    dimension: str = Field(description="维度：user / device / ip")
    value: str = Field(description="维度值（user_id / device_id / ip）")
    reason: str = Field(default="", description="列入原因")
    added_by: str = Field(default="system", description="操作人")
    added_at: str = Field(description="加入时间")


# ---------------------------------------------------------------------- #
# 用户画像
# ---------------------------------------------------------------------- #
class UserProfile(BaseModel):
    """用户风险画像（随事件累积更新）。"""
    user_id: str = Field(description="用户 ID")
    risk_score: int = Field(default=0, description="滚动风险分")
    total_events: int = Field(default=0, description="累计事件数")
    reg_count: int = Field(default=0, description="累计注册次数")
    order_count: int = Field(default=0, description="累计下单次数")
    pay_count: int = Field(default=0, description="累计支付次数")
    device_ids: List[str] = Field(default_factory=list, description="使用过的设备")
    ips: List[str] = Field(default_factory=list, description="使用过的 IP")
    first_seen: str = Field(default="", description="首次出现时间")
    last_seen: str = Field(default="", description="最近出现时间")
    block_count: int = Field(default=0, description="被拦截次数")
    case_count: int = Field(default=0, description="建案次数")


# ---------------------------------------------------------------------- #
# 风控案件
# ---------------------------------------------------------------------- #
class RiskCase(BaseModel):
    """中高风险事件自动生成的复核案件。"""
    case_id: str = Field(description="案件 ID")
    event_id: str = Field(description="关联事件 ID")
    event_type: EventType = Field(description="事件类型")
    user_id: str = Field(description="关联用户")
    created_at: str = Field(description="创建时间")
    status: CaseStatus = Field(default=CaseStatus.PENDING, description="案件状态")
    # 快照与结论在判定后回填
    decision_action: Optional[DecisionAction] = Field(default=None, description="决策动作")
    total_score: int = Field(default=0, description="风险分")
    level: Optional[RiskLevel] = Field(default=None, description="风险等级")
    hit_rule_ids: List[str] = Field(default_factory=list, description="命中规则")
    reasons: List[str] = Field(default_factory=list, description="风险成因")
    evidence_event_ids: List[str] = Field(default_factory=list, description="证据事件 ID")
    review_note: str = Field(default="", description="复核意见")
    reviewer: Optional[str] = Field(default=None, description="复核人")
    reviewed_at: Optional[str] = Field(default=None, description="复核时间")
    disposition: str = Field(default="", description="处置结果说明")
