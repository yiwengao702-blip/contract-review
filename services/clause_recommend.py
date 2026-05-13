# 文件位置: contract-review/services/clause_recommend.py
# 功能说明: 条款推荐引擎。
#          为每条风险发现生成可落地的修改示范条款：优先从标准条款库（clause_library.yaml）
#          按 recommend_key 查表取模板；无模板时按风险维度给出通用改写建议。
#          生成的建议文本支持法务在前端在线编辑、采纳。
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import yaml

from config import cfg
from core.models import RiskFinding


class ClauseRecommend:
    """条款推荐服务（条款库可热重载）。"""

    def __init__(self) -> None:
        self.library: Dict[str, dict] = {}
        self.reload()

    def reload(self) -> Dict[str, dict]:
        """（重新）加载标准示范条款库。

        「合规规则与模板」页新增示范条款后调用本方法，新条款即可被后续审查任务推荐使用。
        """
        self.library = self._load_library()
        return self.library

    def _load_library(self) -> Dict[str, dict]:
        """加载条款库，统一归一为 {key: {title, category, text}} 结构。

        兼容两种写法：
          · 旧版纯文本 —— `key: "条款正文"`
          · 新版结构体 —— `key: {title, category, text}`
        统一归一后，前端「标准示范条款库」页可展示名称与关联维度，
        而推荐引擎只取 text 字段即可，无需再关心 YAML 的具体形态。
        """
        path = cfg.RULES_DIR / "clause_library.yaml"
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        lib: Dict[str, dict] = {}
        for key, val in (data.get("clauses") or {}).items():
            if isinstance(val, dict):
                lib[key] = {
                    "key": key,
                    "title": val.get("title") or key,
                    "category": val.get("category") or "其他",
                    "text": str(val.get("text") or "").strip(),
                }
            else:
                lib[key] = {"key": key, "title": key, "category": "其他", "text": str(val).strip()}
        return lib

    def fill(self, findings: List[RiskFinding]) -> List[RiskFinding]:
        """为发现列表填充 recommendation 字段（就地更新并返回）。"""
        for f in findings:
            f.recommendation = self._recommend_for(f)
        return findings

    def _recommend_for(self, f: RiskFinding) -> str:
        # 1) 优先使用规则关联的示范条款
        item = self.library.get(f.recommend_key or "")
        if item and item.get("text"):
            return item["text"]
        # 2) 按风险维度回退到通用建议
        return self._generic(f)

    @staticmethod
    def _generic(f: RiskFinding) -> str:
        cat = f.category
        if cat == "保密义务":
            return "双方应对履约中知悉的对方商业秘密承担保密义务，保密期限建议约定为合同终止后【3】年。"
        if cat == "违约责任":
            return "建议调整为双方对等责任：任一方违约应赔偿对方实际损失，违约金以违约部分金额的【20】%为上限。"
        if cat == "争议解决管辖":
            return "建议约定与争议有实际联系的管辖地（合同签订地/被告住所地/合同履行地/标的物所在地）法院管辖。"
        if cat == "付款进度":
            return "建议增加“验收合格后凭发票付款”的前置条件，未通过验收可拒付。"
        if cat == "主体资质":
            return "请补全甲乙双方全称与统一社会信用代码，并与营业执照核对一致。"
        return f"建议针对【{cat}】维度补充明确、可量化的约定，避免歧义与履约争议。"
