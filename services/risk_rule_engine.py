# 文件位置: contract-review/services/risk_rule_engine.py
# 功能说明: 风控规则引擎。
#          1) 从 data/risk_rules/risk_rules.yaml 加载规则；2) 对单条事件 + 特征快照做
#          逐条规则求值（返回命中明细，供仿真回溯）；3) 按「名单优先 → 风险分阈值 →
#          规则建议动作」做决策仲裁，输出 pass / review / block 三类结论。
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

import yaml

from config import cfg
from core.risk_models import (
    DecisionAction,
    RiskDecision,
    RiskEvent,
    RiskLevel,
    RuleDef,
    RuleHit,
)

# 决策仲裁阈值：风险分累加超过「拦截线」直接拦截；超过「人审线」或命中建议人审的规则
# 则转人工审核；二者都不到则放行。阈值取经验值，集中在此便于调参。
REVIEW_THRESHOLD = 30
BLOCK_THRESHOLD = 60

# 等级排序权重（用于仲裁时取「命中规则里最高等级」）
_LEVEL_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}

# 条件运算符支持表
_CMP = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b if isinstance(b, (list, tuple, set)) else False,
    "not_in": lambda a, b: a not in b if isinstance(b, (list, tuple, set)) else True,
    "contains": lambda a, b: b in a if isinstance(a, str) else False,
}


def _read_yaml(path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resolve_field(field: str, feature, event: RiskEvent):
    """按字段名从特征快照或事件载荷取值，取不到返回 None。"""
    if field.startswith("payload."):
        return event.payload.get(field[len("payload."):])
    return getattr(feature, field, None)


def _eval_condition(cond: Dict[str, Any], feature, event: RiskEvent):
    """求值单条条件 -> (ok, actual_value)。"""
    field = cond.get("field")
    op = cond.get("op")
    expected = cond.get("value")
    if field is None or op not in _CMP:
        return False, None
    actual = _resolve_field(field, feature, event)
    if actual is None:
        return False, None
    try:
        ok = _CMP[op](actual, expected)
    except TypeError:
        # 类型不可比（如字符串与数字比大小）视为不命中
        return False, actual
    return bool(ok), actual


class RiskRuleEngine:
    """风控规则引擎（线程安全；规则可在界面启停后 reload）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: List[RuleDef] = []
        self.reload()

    # —— 规则加载 —— #
    def reload(self) -> int:
        """重新从 YAML 加载规则；返回规则条数。"""
        data = _read_yaml(cfg.RISK_RULES_DIR / "risk_rules.yaml")
        raw = data.get("rules", []) or []
        rules: List[RuleDef] = []
        for r in raw:
            try:
                rules.append(RuleDef(**r))
            except Exception:
                # 单条规则结构损坏不应拖垮整个引擎，跳过并继续
                continue
        with self._lock:
            self._rules = rules
        return len(rules)

    def list_rules(self) -> List[RuleDef]:
        with self._lock:
            return [r for r in self._rules]

    def get_rule(self, rule_id: str) -> Optional[RuleDef]:
        with self._lock:
            for r in self._rules:
                if r.id == rule_id:
                    return r
        return None

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        """改写 YAML 中规则的 enabled 开关（保留注释，沿用合同审查项目的文本级替换策略）。"""
        import re
        path = cfg.RISK_RULES_DIR / "risk_rules.yaml"
        text = path.read_bytes().decode("utf-8")
        pat = re.compile(
            r"(?m)(^  - id: " + re.escape(rule_id) + r"\b[\s\S]*?\n    enabled: )(true|false)"
        )
        new_text, n = pat.subn(lambda m: m.group(1) + ("true" if enabled else "false"), text, count=1)
        if n == 0:
            return False
        path.write_bytes(new_text.encode("utf-8"))
        self.reload()
        return True

    @staticmethod
    def _rule_block(text: str, rule_id: str) -> "re.Match":
        import re
        return re.search(r"(?m)^  - id: " + re.escape(rule_id) + r"\b[\s\S]*?(?=\n  - id: |\Z)", text)

    def add_rule(self, rule: dict) -> RuleDef:
        """新增规则，追加写入 risk_rules.yaml（保留文件原有注释）。"""
        rid = str(rule.get("id") or "").strip()
        if not rid:
            raise ValueError("规则 id 不能为空")
        if self.get_rule(rid) is not None:
            raise ValueError(f"规则 id 已存在：{rid}")
        try:
            rdef = RuleDef(**rule)
        except Exception as e:
            raise ValueError(f"规则结构非法：{e}")

        # 以 YAML 序列化后整体缩进 2 空格续写到 rules 列表之下
        block = yaml.safe_dump([rdef.model_dump()], allow_unicode=True,
                               default_flow_style=False, sort_keys=False)
        indented = "\n".join(("  " + ln) if ln.strip() else ln
                             for ln in block.rstrip("\n").split("\n"))
        path = cfg.RISK_RULES_DIR / "risk_rules.yaml"
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + indented + "\n")
        self.reload()
        return rdef

    def delete_rule(self, rule_id: str) -> bool:
        """从 YAML 中删除整条规则（文本级块删除，保留其余注释）。"""
        import re
        path = cfg.RISK_RULES_DIR / "risk_rules.yaml"
        text = path.read_bytes().decode("utf-8")
        m = self._rule_block(text, rule_id)
        if not m:
            return False
        new_text = text[:m.start()] + text[m.end():]
        path.write_bytes(new_text.encode("utf-8"))
        self.reload()
        return True

    def update_rule(self, rule_id: str, fields: dict) -> bool:
        """更新规则的若干标量字段（name/score/level/action/advice/category）。"""
        import re
        path = cfg.RISK_RULES_DIR / "risk_rules.yaml"
        text = path.read_bytes().decode("utf-8")
        m = self._rule_block(text, rule_id)
        if not m:
            return False
        block = m.group(0)
        # 逐字段做文本级替换（只动标量行，不动 conditions 结构，避免破坏复杂表达式）
        mappings = {
            "name": r"(name: ).*$",
            "category": r"(category: ).*$",
            "score": r"(score: ).*$",
            "level": r"(level: ).*$",
            "action": r"(action: ).*$",
            "advice": r"(advice: ).*$",
        }
        for key, pat in mappings.items():
            if key not in fields:
                continue
            val = fields[key]
            block = re.sub(r"(?m)" + pat,
                           lambda mm: mm.group(1) + str(val),
                           block, count=1)
        new_text = text[:m.start()] + block + text[m.end():]
        path.write_bytes(new_text.encode("utf-8"))
        self.reload()
        return True

    # —— 规则求值 —— #
    def evaluate(self, event: RiskEvent, feature) -> List[RuleHit]:
        """逐条求值规则，返回全部 RuleHit（命中与否都返回，供链路回溯）。"""
        hits: List[RuleHit] = []
        for r in self.list_rules():
            if not r.enabled:
                continue
            # 作用域过滤：scope 非空且事件类型不在其中则跳过该规则
            if r.scope and event.event_type.value not in r.scope:
                continue

            conds = r.conditions or []
            if not conds:
                # 无条件规则视为不命中（避免误伤）
                hits.append(self._mk_hit(r, False, "无条件定义"))
                continue

            results = [_eval_condition(c, feature, event) for c in conds]
            matched = (all(x[0] for x in results) if r.match == "all"
                       else any(x[0] for x in results))
            if matched:
                detail = r.advice or "命中规则条件"
            else:
                # 取第一个未命中的条件作为「未命中原因」，便于回溯
                first_fail = next((x for x in results if not x[0]), None)
                detail = "条件未满足" if first_fail is None else "条件未满足"
            hits.append(self._mk_hit(r, matched, detail))
        return hits

    @staticmethod
    def _mk_hit(r: RuleDef, matched: bool, detail: str) -> RuleHit:
        return RuleHit(
            rule_id=r.id, rule_name=r.name, score=r.score,
            level=r.level, action=r.action, matched=matched, detail=detail,
        )

    # —— 决策仲裁 —— #
    def arbitrate(self, hits: List[RuleHit], list_hit: Optional[str]) -> RiskDecision:
        """综合规则命中与名单命中，给出最终决策。"""
        # 名单优先：黑名单直接拦截，白名单直接放行（覆盖规则结论）
        if list_hit == "black":
            return RiskDecision(
                action=DecisionAction.BLOCK, total_score=0,
                level=RiskLevel.HIGH, hits=hits,
                reasons=["命中黑名单（user/device/ip）"], list_hit="black",
            )
        if list_hit == "white":
            return RiskDecision(
                action=DecisionAction.PASS, total_score=0,
                level=RiskLevel.LOW, hits=hits,
                reasons=["命中白名单，免审放行"], list_hit="white",
            )

        matched = [h for h in hits if h.matched]
        total = sum(h.score for h in matched)
        # 取命中规则中的最高等级
        level = RiskLevel.LOW
        for h in matched:
            if _LEVEL_RANK[h.level] > _LEVEL_RANK[level]:
                level = h.level

        # 动作仲裁：黑名单已处理；这里看「是否有规则建议拦截」「分数是否过线」「是否建议人审」
        has_block = any(h.action == DecisionAction.BLOCK for h in matched)
        has_review = any(h.action == DecisionAction.REVIEW for h in matched)

        if has_block or total >= BLOCK_THRESHOLD:
            action = DecisionAction.BLOCK
        elif has_review or total >= REVIEW_THRESHOLD or level == RiskLevel.HIGH:
            action = DecisionAction.REVIEW
        else:
            action = DecisionAction.PASS

        reasons = [f"命中规则「{h.rule_name}」（{h.level.value} +{h.score}）" for h in matched]
        if not reasons:
            reasons = ["未命中任何启用规则，正常放行"]

        return RiskDecision(
            action=action, total_score=total, level=level,
            hits=hits, reasons=reasons, list_hit=None,
        )


# 模块级单例
risk_rule_engine = RiskRuleEngine()
