# 文件位置: contract-review/core/risk_pipeline.py
# 功能说明: 电商风控流水线编排（沿用项目 NodeBase 节点链约定）。
#          链路：特征计算 → 名单判定 → 规则求值与决策仲裁 → 自动建案 → 落历史/画像。
#          RiskEngine 持有运行态（近期事件窗口、用户画像、判定历史），并对外提供
#          事件接入、仿真回溯、案件/画像/态势查询等入口。
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from config import cfg
from core.node_base import NodeBase
from core.risk_models import (
    DecisionAction,
    RiskCase,
    RiskDecision,
    RiskEvent,
    UserProfile,
)
from services.case_manager import case_manager
from services.event_parser import parse_event
from services.feature_service import feature_service
from services.list_manager import list_manager
from services.risk_analytics import risk_analytics
from services.risk_rule_engine import risk_rule_engine

# 近期事件窗口上限：超过则丢弃最旧，保证特征计算只看「近期」且内存有界
_MAX_RECENT_EVENTS = 500
# 判定历史保留上限
_MAX_HISTORY = 1000


# ---------------------------------------------------------------------- #
# 流水线节点（每个节点就地更新 context）
# ---------------------------------------------------------------------- #
class FeatureNode(NodeBase):
    """根据近期事件与用户画像，计算当前事件的特征快照。"""
    node_name = "feature"

    def run(self, ctx: dict) -> dict:
        eng: "RiskEngine" = ctx["engine"]
        ctx["feature"] = feature_service.compute(
            ctx["event"], eng.recent_events(), eng.profiles)
        return ctx


class ListNode(NodeBase):
    """名单判定：黑名单优先拦截、白名单免审放行。"""
    node_name = "list"

    def run(self, ctx: dict) -> dict:
        ctx["list_hit"] = list_manager.check_event(ctx["event"])
        return ctx


class RuleNode(NodeBase):
    """规则求值 + 决策仲裁。"""
    node_name = "rule"

    def run(self, ctx: dict) -> dict:
        hits = risk_rule_engine.evaluate(ctx["event"], ctx["feature"])
        ctx["hits"] = hits
        ctx["decision"] = risk_rule_engine.arbitrate(hits, ctx.get("list_hit"))
        return ctx


class CaseNode(NodeBase):
    """中高风险事件自动建案。"""
    node_name = "case"

    def run(self, ctx: dict) -> dict:
        if ctx["decision"].action != DecisionAction.PASS:
            case = case_manager.create_from_decision(
                ctx["event"], ctx["feature"], ctx["decision"])
            ctx["case_id"] = case.case_id if case else None
        return ctx


class HistoryNode(NodeBase):
    """把判定结论追加进历史，并更新用户画像累积量。"""
    node_name = "history"

    def run(self, ctx: dict) -> dict:
        eng: "RiskEngine" = ctx["engine"]
        d: RiskDecision = ctx["decision"]
        eng.append_history({
            "event_id": ctx["event"].event_id,
            "event_type": ctx["event"].event_type.value,
            "user_id": ctx["event"].user_id,
            "action": d.action.value,
            "level": d.level.value,
            "total_score": d.total_score,
            "list_hit": d.list_hit,
            "hit_rules": [{"rule_id": h.rule_id, "rule_name": h.rule_name}
                          for h in d.hits if h.matched],
            "reasons": d.reasons,
            "case_id": ctx.get("case_id"),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        eng.touch_profile(ctx["event"], d.action)
        return ctx


# 节点执行顺序
_CHAIN = ["feature", "list", "rule", "case", "history"]


class RiskEngine:
    """风控引擎：持有运行态并编排节点链。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: List[RiskEvent] = []
        self._profiles: Dict[str, UserProfile] = {}
        self._history: List[dict] = []

    # —— 运行态访问（加锁，保证并发接入安全） —— #
    @property
    def profiles(self) -> Dict[str, UserProfile]:
        return self._profiles

    def recent_events(self) -> List[RiskEvent]:
        with self._lock:
            return list(self._events)

    def events(self) -> List[RiskEvent]:
        return self.recent_events()

    def history(self) -> List[dict]:
        with self._lock:
            return list(self._history)

    def append_history(self, rec: dict) -> None:
        with self._lock:
            self._history.append(rec)
            if len(self._history) > _MAX_HISTORY:
                self._history = self._history[-_MAX_HISTORY:]

    # —— 用户画像累积 —— #
    def touch_profile(self, event: RiskEvent, action: DecisionAction) -> None:
        with self._lock:
            p = self._profiles.get(event.user_id)
            if p is None:
                p = UserProfile(user_id=event.user_id,
                                first_seen=event.timestamp)
                self._profiles[event.user_id] = p
            p.total_events += 1
            if event.event_type.value == "register":
                p.reg_count += 1
            elif event.event_type.value == "order":
                p.order_count += 1
            elif event.event_type.value == "payment":
                p.pay_count += 1
            if action == DecisionAction.BLOCK:
                p.block_count += 1
            if event.event_type.value in ("review", "block"):
                p.case_count = len([c for c in case_manager.list_cases()
                                    if c.user_id == event.user_id])
            dev = event.device
            if dev:
                if dev.device_id and dev.device_id not in p.device_ids:
                    p.device_ids.append(dev.device_id)
                if dev.ip and dev.ip not in p.ips:
                    p.ips.append(dev.ip)
            p.last_seen = event.timestamp

    # —— 事件接入（persist=True：真实接入，落历史与画像） —— #
    def ingest(self, raw: dict, persist: bool = True) -> dict:
        """接入一条事件（原始 dict），返回完整处理结果与判定链路。"""
        event = parse_event(raw)
        with self._lock:
            self._events.append(event)
            if len(self._events) > _MAX_RECENT_EVENTS:
                self._events = self._events[-_MAX_RECENT_EVENTS:]

        if not persist:
            # 仿真模式：不污染运行态，仍在只读副本上跑一遍链路
            return self._run_chain(event, persist=False)

        return self._run_chain(event, persist=True)

    def _run_chain(self, event: RiskEvent, persist: bool) -> dict:
        """执行节点链；persist=False 时跳过会改写运行态的节点（case/history）。"""
        ctx: dict = {"engine": self, "event": event}
        for name in _CHAIN:
            if not persist and name in ("case", "history"):
                # 仿真只算特征/名单/规则/决策，不建案不落历史
                if name == "rule":
                    # rule 节点在持久模式下会建案（case 节点），这里手动补决策
                    hits = risk_rule_engine.evaluate(event, ctx["feature"])
                    ctx["hits"] = hits
                    ctx["decision"] = risk_rule_engine.arbitrate(hits, ctx.get("list_hit"))
                continue
            # get_node 返回的是节点类，需先实例化再执行 run（run 是实例方法）
            NodeBase.get_node(name)().run(ctx)

        return {
            "event": event.model_dump(mode="json"),
            "feature": ctx["feature"].model_dump(mode="json") if ctx.get("feature") else None,
            "list_hit": ctx.get("list_hit"),
            "decision": ctx["decision"].model_dump(mode="json") if ctx.get("decision") else None,
            "case_id": ctx.get("case_id"),
        }

    def simulate(self, raw: dict) -> dict:
        """仿真单条事件：返回完整判定链路（含逐条规则命中明细），但不写运行态。"""
        return self.ingest(raw, persist=False)

    # —— 查询入口 —— #
    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        return self._profiles.get(user_id)

    def list_profiles(self) -> List[UserProfile]:
        with self._lock:
            return list(self._profiles.values())

    def analytics(self) -> dict:
        return risk_analytics.compute(self.history())

    def reset(self) -> None:
        """清空运行态（演示用）。"""
        with self._lock:
            self._events.clear()
            self._profiles.clear()
            self._history.clear()


# 全局唯一引擎实例（与合同审查 ReviewPipeline 同构：进程内单例）
risk_engine = RiskEngine()
