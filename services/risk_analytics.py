# 文件位置: contract-review/services/risk_analytics.py
# 功能说明: 态势大盘数据统计。
#          输入判定历史（每次事件接入都会留一条），输出实时拦截率、风险分布、
#          规则命中排行等指标。统计只读、无状态，便于前端按不同时间窗重复调用。
from __future__ import annotations

from collections import Counter
from typing import Dict, List


class RiskAnalytics:
    """态势大盘指标计算（无状态函数式服务）。"""

    def compute(self, history: List[dict]) -> Dict:
        total = len(history)
        action_counter = Counter(h.get("action") for h in history)
        level_counter = Counter(h.get("level") for h in history)

        block = action_counter.get("block", 0)
        review = action_counter.get("review", 0)
        passed = action_counter.get("pass", 0)
        # 拦截率：被拦截事件 / 总处理事件；人审不计入拦截（仍放行待核）
        intercept_rate = round(block / total * 100, 1) if total else 0.0
        review_rate = round(review / total * 100, 1) if total else 0.0

        # 规则命中排行：统计每条规则在判定历史中被命中的次数
        rule_hits: Counter = Counter()
        rule_names: Dict[str, str] = {}
        for h in history:
            for r in h.get("hit_rules", []) or []:
                rid = r.get("rule_id") if isinstance(r, dict) else r
                rule_hits[rid] += 1
                if isinstance(r, dict) and r.get("rule_name"):
                    rule_names[rid] = r["rule_name"]
        top_rules = [
            {"rule_id": rid, "rule_name": rule_names.get(rid, rid), "hits": cnt}
            for rid, cnt in rule_hits.most_common(10)
        ]

        return {
            "total": total,
            "pass": passed,
            "review": review,
            "block": block,
            "intercept_rate": intercept_rate,
            "review_rate": review_rate,
            "risk_distribution": {
                "high": level_counter.get("high", 0),
                "medium": level_counter.get("medium", 0),
                "low": level_counter.get("low", 0),
            },
            "top_rules": top_rules,
            "recent": history[-12:][::-1],  # 最近 12 条（倒序，最新在前）
        }


# 模块级单例
risk_analytics = RiskAnalytics()
