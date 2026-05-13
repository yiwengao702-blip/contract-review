# 文件位置: contract-review/services/case_manager.py
# 功能说明: 风控案件管理。
#          中高风险（review / block）事件自动生成复核案件；支持案件列表、详情、人工复核
#          与处置闭环（通过 / 驳回 / 结案）。案件落盘 data/risk_state/cases.jsonl（逐行 JSON）。
from __future__ import annotations

import threading
import time
import uuid
from typing import List, Optional

from config import cfg
from core.risk_models import (
    CaseStatus,
    DecisionAction,
    RiskCase,
    RiskDecision,
    RiskEvent,
    UserProfile,
)


class CaseManager:
    """风控案件管理器（内存为主、追加写 jsonl 落盘）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = cfg.RISK_STATE_DIR / "cases.jsonl"
        self._cases: List[RiskCase] = self._load()

    def _load(self) -> List[RiskCase]:
        out: List[RiskCase] = []
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(RiskCase(**__import__("json").loads(line)))
                except Exception:
                    continue
        return out

    def _append(self, case: RiskCase) -> None:
        import json
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")

    def create_from_decision(self, event: RiskEvent, feature: UserProfile,
                             decision: RiskDecision) -> Optional[RiskCase]:
        """仅当中高风险（非 pass）时建案；返回新建案件或 None。"""
        if decision.action == DecisionAction.PASS:
            return None
        case = RiskCase(
            case_id="C" + uuid.uuid4().hex[:10].upper(),
            event_id=event.event_id,
            event_type=event.event_type,
            user_id=event.user_id,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            status=CaseStatus.PENDING,
            decision_action=decision.action,
            total_score=decision.total_score,
            level=decision.level,
            hit_rule_ids=[h.rule_id for h in decision.hits if h.matched],
            reasons=list(decision.reasons),
            evidence_event_ids=list(feature.evidence_event_ids) if hasattr(feature, "evidence_event_ids") else [],
        )
        with self._lock:
            self._cases.append(case)
        self._append(case)
        return case

    def list_cases(self, status: Optional[str] = None) -> List[RiskCase]:
        with self._lock:
            cases = [c for c in self._cases]
        if status:
            cases = [c for c in cases if c.status.value == status]
        # 最新在前
        return sorted(cases, key=lambda c: c.created_at, reverse=True)

    def get_case(self, case_id: str) -> Optional[RiskCase]:
        with self._lock:
            for c in self._cases:
                if c.case_id == case_id:
                    return c
        return None

    def review(self, case_id: str, reviewer: str, action: str,
               note: str = "", disposition: str = "") -> RiskCase:
        """人工复核闭环。

        action: approve（风险可接受/处置完成） / reject（确认违规，转处置） / close（结案）
        状态映射：approve -> approved，reject -> rejected，close -> closed。
        复核一旦落库，更新内存与落盘文件（整文件重写，案件量不大，重写更安全）。
        """
        case = self.get_case(case_id)
        if case is None:
            raise KeyError(f"案件不存在：{case_id}")
        mapping = {"approve": CaseStatus.APPROVED, "reject": CaseStatus.REJECTED,
                   "close": CaseStatus.CLOSED}
        if action not in mapping:
            raise ValueError(f"未知复核动作：{action}")
        case.status = mapping[action]
        case.reviewer = reviewer
        case.review_note = note
        case.disposition = disposition
        case.reviewed_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._rewrite()
        return case

    def _rewrite(self) -> None:
        import json
        with self._lock:
            lines = [json.dumps(c.model_dump(), ensure_ascii=False) for c in self._cases]
        self._path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# 模块级单例
case_manager = CaseManager()
