# 文件位置: contract-review/services/audit_center.py
# 功能说明: 合同审计中心。把分散在任务里的审查结果，聚合成审计视角的四种资产：
#             ① 合同台账   —— 可检索、可筛选、可排序的合同清单与汇总指标
#             ② 相对方画像 —— 按签约主体聚合的合作次数、累计金额、风险分布
#             ③ 风险洞察   —— 风险等级分布、维度分布、高发规则排行
#             ④ 黑名单     —— 对高风险相对方设限，审查时给出提示
#           设计原则：
#             · 不另存副本。所有数字都从 pipeline 的任务实时派生，
#               避免"台账与审查结果对不上"这类双写不一致。
#             · 只有黑名单是真正的持久化状态（它不属于任何单份合同）。
#           对外接口：ledger() / counterparties() / insights() / blacklist 系列
from __future__ import annotations

import json
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import cfg

# 风险等级排序权重：用于排序与"加权风险分"计算
LEVEL_WEIGHT = {"high": 3, "medium": 2, "low": 1}
LEVEL_TEXT = {"high": "高风险", "medium": "中风险", "low": "低风险", "none": "无风险"}


def _norm(v) -> str:
    """把可能为 None 的字段安全转成字符串。"""
    return str(v or "").strip()


def _num(v) -> float:
    """把可能为 None 的金额安全转成数字。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class AuditCenter:
    """合同审计中心：台账 / 相对方画像 / 风险洞察 / 黑名单。"""

    def __init__(self, task_provider: Optional[Callable[[], List]] = None):
        # task_provider 传进来而不是直接 import pipeline：
        # 服务层不反向依赖上层，既便于单测注入，也避免循环导入。
        self._task_provider = task_provider
        self._lock = threading.RLock()
        self._blacklist: Dict[str, dict] = {}
        self._load_blacklist()

    # ------------------------------------------------------------------ #
    # 数据源
    # ------------------------------------------------------------------ #
    def _tasks(self) -> List:
        if self._task_provider is None:
            return []
        try:
            return list(self._task_provider())
        except Exception:
            return []

    @staticmethod
    def _rows(task) -> List[dict]:
        """把单个任务摊平成台账行所需的字段。

        只取"审查已完成"的任务参与审计统计：解析受阻/被拒的任务没有
        风险结论，混进去会把"高风险率"这类指标算歪。
        """
        report = getattr(task, "report", None)
        meta = getattr(task, "meta", None)
        findings = list(getattr(task, "findings", None) or [])
        levels = Counter(f.level.value if hasattr(f.level, "value") else str(f.level)
                         for f in findings)
        overall = report.overall_level.value if report and getattr(report, "overall_level", None) else "none"
        return {
            "task_id": task.task_id,
            "filename": task.filename,
            "contract_type": task.contract_type or "未分类",
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "source": getattr(task, "source", "upload") or "upload",
            "party_a": _norm(meta.party_a) if meta else "",
            "party_b": _norm(meta.party_b) if meta else "",
            "amount": _num(meta.amount) if meta else 0.0,
            "currency": _norm(meta.currency) if meta and meta.currency else "CNY",
            "duration": _norm(meta.duration) if meta else "",
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "retries": getattr(task, "retries", 0) or 0,
            "risk_level": overall,
            "risk_text": LEVEL_TEXT.get(overall, "无风险"),
            "findings_total": len(findings),
            "findings_high": levels.get("high", 0),
            "findings_medium": levels.get("medium", 0),
            "findings_low": levels.get("low", 0),
            # 加权风险分：3×高 + 2×中 + 1×低，用于排序与"重点关注"排序
            "risk_score": LEVEL_WEIGHT.get("high", 3) * levels.get("high", 0)
            + LEVEL_WEIGHT.get("medium", 2) * levels.get("medium", 0)
            + LEVEL_WEIGHT.get("low", 1) * levels.get("low", 0),
            "auditable": bool(report),
        }

    # ------------------------------------------------------------------ #
    # ① 合同台账
    # ------------------------------------------------------------------ #
    def ledger(self, keyword: str = "", contract_type: str = "", risk_level: str = "",
               status: str = "", min_amount: Optional[float] = None,
               max_amount: Optional[float] = None,
               sort: str = "created_at", order: str = "desc",
               only_auditable: bool = True) -> dict:
        """合同台账：检索 + 筛选 + 排序 + 汇总。

        Args:
            only_auditable: 默认只纳入已出具审查报告的任务。
                审计台账统计的是"审过的合同"，未完成的任务属于待办而非审计对象。
        """
        rows = [self._rows(t) for t in self._tasks()]
        if only_auditable:
            rows = [r for r in rows if r["auditable"]]

        kw = _norm(keyword).lower()
        if kw:
            rows = [r for r in rows if any(kw in _norm(r[k]).lower() for k in
                    ("filename", "party_a", "party_b", "task_id", "contract_type"))]
        if contract_type:
            rows = [r for r in rows if r["contract_type"] == contract_type]
        if risk_level:
            rows = [r for r in rows if r["risk_level"] == risk_level]
        if status:
            rows = [r for r in rows if r["status"] == status]
        if min_amount is not None:
            rows = [r for r in rows if r["amount"] >= min_amount]
        if max_amount is not None:
            rows = [r for r in rows if r["amount"] <= max_amount]

        # 排序：金额/时间/风险分/风险等级
        key_map = {
            "amount": lambda r: r["amount"],
            "created_at": lambda r: r["created_at"],
            "risk_score": lambda r: r["risk_score"],
            "risk_level": lambda r: LEVEL_WEIGHT.get(r["risk_level"], 0),
            "findings": lambda r: r["findings_total"],
        }
        key_fn = key_map.get(sort, key_map["created_at"])
        rows.sort(key=key_fn, reverse=(order.lower() != "asc"))

        return {
            "rows": rows,
            "summary": self._summary(rows),
            "total": len(rows),
        }

    @staticmethod
    def _summary(rows: List[dict]) -> dict:
        """台账汇总指标。"""
        total_amount = sum(r["amount"] for r in rows)
        levels = Counter(r["risk_level"] for r in rows)
        n = len(rows)
        return {
            "count": n,
            "total_amount": round(total_amount, 2),
            "avg_amount": round(total_amount / n, 2) if n else 0.0,
            "high": levels.get("high", 0),
            "medium": levels.get("medium", 0),
            "low": levels.get("low", 0),
            "none": levels.get("none", 0),
            # 高风险率：审计最关心的一个数
            "high_rate": round(levels.get("high", 0) / n * 100, 1) if n else 0.0,
            "findings_total": sum(r["findings_total"] for r in rows),
            "findings_high": sum(r["findings_high"] for r in rows),
        }

    def ledger_facets(self) -> dict:
        """台账的可选筛选项（从现有数据反推，不硬编码）。"""
        rows = [self._rows(t) for t in self._tasks()]
        rows = [r for r in rows if r["auditable"]]
        return {
            "contract_types": sorted({r["contract_type"] for r in rows}),
            "risk_levels": [
                {"value": k, "label": LEVEL_TEXT.get(k, k),
                 "count": Counter(r["risk_level"] for r in rows).get(k, 0)}
                for k in ("high", "medium", "low", "none")
            ],
            "statuses": sorted({r["status"] for r in rows}),
        }

    # ------------------------------------------------------------------ #
    # ② 相对方画像
    # ------------------------------------------------------------------ #
    def counterparties(self) -> dict:
        """按签约主体聚合合作画像。

        甲乙方都纳入统计：甲方多是"我方/委托方"，乙方是"相对方"，
        审计视角下两者都需要画像（例如同一主体既做过甲方也做过乙方）。
        """
        rows = [self._rows(t) for t in self._tasks()]
        rows = [r for r in rows if r["auditable"]]

        agg: Dict[str, dict] = {}
        for r in rows:
            for side, name in (("party_a", r["party_a"]), ("party_b", r["party_b"])):
                if not name:
                    continue
                item = agg.setdefault(name, {
                    "name": name, "as_party_a": 0, "as_party_b": 0,
                    "contracts": 0, "total_amount": 0.0,
                    "high": 0, "medium": 0, "low": 0, "findings": 0,
                    "types": Counter(), "last_at": "", "task_ids": [],
                })
                if side == "party_a":
                    item["as_party_a"] += 1
                else:
                    item["as_party_b"] += 1
                item["contracts"] += 1
                item["total_amount"] += r["amount"]
                item["high"] += r["findings_high"]
                item["medium"] += r["findings_medium"]
                item["low"] += r["findings_low"]
                item["findings"] += r["findings_total"]
                item["types"][r["contract_type"]] += 1
                if r["created_at"] > item["last_at"]:
                    item["last_at"] = r["created_at"]
                item["task_ids"].append(r["task_id"])

        out = []
        for name, it in agg.items():
            bl = self._blacklist.get(name)
            out.append({
                "name": name,
                "as_party_a": it["as_party_a"],
                "as_party_b": it["as_party_b"],
                "contracts": it["contracts"],
                "total_amount": round(it["total_amount"], 2),
                "high": it["high"], "medium": it["medium"], "low": it["low"],
                "findings": it["findings"],
                "risk_score": 3 * it["high"] + 2 * it["medium"] + it["low"],
                "types": it["types"].most_common(),
                "last_at": it["last_at"],
                "task_ids": it["task_ids"],
                "blacklisted": bool(bl),
                "blacklist_reason": (bl or {}).get("reason", ""),
            })
        # 默认按风险分降序：高风险相对方排最前，符合审计关注顺序
        out.sort(key=lambda x: (-x["risk_score"], -x["total_amount"]))
        return {
            "rows": out,
            "total": len(out),
            "blacklisted": sum(1 for x in out if x["blacklisted"]),
        }

    # ------------------------------------------------------------------ #
    # ③ 风险洞察
    # ------------------------------------------------------------------ #
    def insights(self) -> dict:
        """风险洞察：等级分布、维度分布、高发规则排行、业务类型分布。"""
        tasks = self._tasks()
        rows = [self._rows(t) for t in tasks]
        rows = [r for r in rows if r["auditable"]]

        by_level = Counter(r["risk_level"] for r in rows)
        by_type: Dict[str, dict] = defaultdict(
            lambda: {"count": 0, "amount": 0.0, "high": 0, "findings": 0})
        by_dim: Counter = Counter()
        by_rule: Dict[str, dict] = {}

        for t in tasks:
            if not getattr(t, "report", None):
                continue
            r = self._rows(t)
            bucket = by_type[r["contract_type"]]
            bucket["count"] += 1
            bucket["amount"] += r["amount"]
            bucket["high"] += r["findings_high"]
            bucket["findings"] += r["findings_total"]

            for f in (t.findings or []):
                cat = _norm(getattr(f, "category", "")) or "其他"
                by_dim[cat] += 1
                # 高发规则：按"规则成因"归并（同一成因反复出现即为系统性问题）
                reason = _norm(getattr(f, "reason", ""))[:60] or cat
                item = by_rule.setdefault(reason, {
                    "reason": reason, "category": cat, "count": 0,
                    "level": f.level.value if hasattr(f.level, "value") else str(f.level),
                })
                item["count"] += 1

        top_rules = sorted(by_rule.values(), key=lambda x: -x["count"])[:8]
        return {
            "summary": self._summary(rows),
            "by_level": [{"value": k, "label": LEVEL_TEXT.get(k, k),
                          "count": by_level.get(k, 0)}
                         for k in ("high", "medium", "low", "none")],
            "by_dimension": [{"name": k, "count": v} for k, v in by_dim.most_common()],
            "by_type": [{"name": k, "count": v["count"],
                         "amount": round(v["amount"], 2),
                         "high": v["high"], "findings": v["findings"]}
                        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]["count"])],
            "top_rules": top_rules,
        }

    # ------------------------------------------------------------------ #
    # ④ 黑名单（唯一需要持久化的状态）
    # ------------------------------------------------------------------ #
    def _bl_path(self) -> Path:
        return Path(cfg.SETTINGS_FILE).parent / "blacklist.json"

    def _load_blacklist(self) -> None:
        p = self._bl_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._blacklist = {k: v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            # 黑名单损坏不该让审计中心起不来：以空表继续
            self._blacklist = {}

    def _save_blacklist(self) -> None:
        p = self._bl_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._blacklist, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)

    def blacklist(self) -> dict:
        with self._lock:
            return {
                "rows": [{"name": k, "reason": v.get("reason", ""),
                          "at": v.get("at", ""), "operator": v.get("operator", "")}
                         for k, v in self._blacklist.items()],
                "total": len(self._blacklist),
            }

    def add_blacklist(self, name: str, reason: str = "", operator: str = "") -> dict:
        name = _norm(name)
        if not name:
            raise ValueError("相对方名称不能为空")
        with self._lock:
            self._blacklist[name] = {
                "reason": _norm(reason),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "operator": _norm(operator) or "系统",
            }
            self._save_blacklist()
        return {"name": name, **self._blacklist[name]}

    def remove_blacklist(self, name: str) -> dict:
        name = _norm(name)
        with self._lock:
            if name not in self._blacklist:
                raise ValueError(f"「{name}」不在黑名单中")
            self._blacklist.pop(name)
            self._save_blacklist()
        return {"name": name, "removed": True}

    def check_party(self, name: str) -> dict:
        """审查前体检：给定相对方名称，返回其历史画像与黑名单状态。"""
        name = _norm(name)
        if not name:
            return {"hit": False}
        bl = self._blacklist.get(name)
        cp = next((x for x in self.counterparties()["rows"] if x["name"] == name), None)
        return {
            "hit": bool(bl) or bool(cp),
            "blacklisted": bool(bl),
            "blacklist_reason": (bl or {}).get("reason", ""),
            "profile": cp,
        }


# 模块级单例：由 main.py 在启动时注入 task_provider
audit_center = AuditCenter()
