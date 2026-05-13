# 文件位置: contract-review/services/rule_engine.py
# 功能说明: 合规规则引擎。
#          加载 data/rules/review_rules.yaml 中的规则，对解析结果与元数据进行模式匹配，
#          输出结构化风险发现（高风险/中风险），覆盖文档 2.4.4 的判定清单。
#          每条发现均记录命中原文片段与全文偏移，供前端双向高亮定位。
#          支持两个"规则库配置"维度（与「合规规则与模板」页联动）：
#            · scope   适用范围：按合同业务类型过滤，["全部类型"] 表示全业务类型生效；
#            · enabled 启用开关：置 false 的规则不参与审查，页面切换后即时生效。
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config import cfg
from core.models import ClauseSegment, ContractMeta, RiskFinding, RiskLevel


class RuleEngine:
    """规则驱动的合规审查引擎（无状态，规则可热重载）。"""

    def __init__(self) -> None:
        self.rules: List[dict] = []
        self.reload()

    def reload(self) -> List[dict]:
        """（重新）从 YAML 加载规则，返回规则列表。

        「合规规则与模板」页切换启用开关或新增规则后调用本方法，
        即可让变更即时生效于后续审查任务（无需重启服务）。
        """
        self.rules = self._load_rules()
        return self.rules

    def _load_rules(self) -> List[dict]:
        path = cfg.RULES_DIR / "review_rules.yaml"
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("rules", [])

    @staticmethod
    def rule_applies(rule: dict, contract_type: str) -> bool:
        """判断规则是否适用于该业务类型：启用开关 + 适用范围双重过滤。"""
        if not rule.get("enabled", True):
            return False
        scope = rule.get("scope") or ["全部类型"]
        if "全部类型" in scope:
            return True
        if not contract_type or contract_type in ("未分类", "其他"):
            # 业务类型未分类时，领域专用规则（劳动/服务等）不参与，避免跨类型误报
            return False
        return contract_type in scope

    # ------------------------------------------------------------------ #
    # 拍照识别来源时，追加在"字段缺失"类告警末尾的不确定性说明。
    # 之所以要加这句：OCR 没认出「甲方」与合同真没写「甲方」，在这条规则上表现完全一样，
    # 但结论天差地别——前者是识别问题，后者才是合同缺陷。
    # 不加说明，法务看到的是一条言之凿凿的高风险，实际可能只是照片没拍清楚。
    OCR_CAVEAT = (
        "\n\n【识别来源提示】本文正文来自拍照识别，字段抽取可能受识别准确率影响；"
        "请先对照照片确认该字段是「合同确实未写明」，还是「识别未认出」，再作判定。"
    )

    def review(self, full_text: str, meta: ContractMeta, segments: List[ClauseSegment],
               contract_type: str = "未分类", source_is_ocr: bool = False) -> List[RiskFinding]:
        """执行规则审查，返回风险发现列表。

        Args:
            contract_type: 任务登记的业务类型，用于按 scope 过滤领域专用规则。
            source_is_ocr: 正文是否来自拍照识别。为真时，"字段缺失"类告警会附上
                不确定性说明，避免把识别失败当成合同缺陷报成高风险。
        """
        findings: List[RiskFinding] = []
        counter = 0
        for rule in self.rules:
            if not self.rule_applies(rule, contract_type):
                continue
            hits = self._apply_rule(rule, full_text, meta, segments)
            for hit in hits:
                counter += 1
                reason = rule.get("reason", "")
                if source_is_ocr and str(hit.get("quote", "")).startswith("缺失字段"):
                    reason += self.OCR_CAVEAT
                findings.append(
                    RiskFinding(
                        finding_id=f"F{counter:03d}",
                        level=RiskLevel(rule["level"]),
                        category=rule.get("category", "其他"),
                        segment_ref=hit.get("segment_ref"),
                        quote=hit.get("quote", ""),
                        start_offset=hit.get("start_offset"),
                        end_offset=hit.get("end_offset"),
                        reason=reason,
                        legal_basis=rule.get("legal_basis", ""),
                        recommendation="",  # 由 ClauseRecommend 填充
                        recommend_key=rule.get("recommend_key") or None,
                        source="rule",
                    )
                )
        return findings

    # ------------------------------------------------------------------ #
    def _apply_rule(self, rule: dict, full_text: str, meta: ContractMeta,
                    segments: List[ClauseSegment]) -> List[dict]:
        rtype = rule.get("type")
        if rtype == "meta_missing":
            return self._rule_meta_missing(rule, meta)
        if rtype == "meta_or_regex":
            return self._rule_meta_or_regex(rule, meta, full_text, segments)
        if rtype == "text_regex":
            return self._rule_text_regex(rule, full_text, segments)
        if rtype == "text_absent":
            return self._rule_text_absent(rule, full_text, segments)
        if rtype == "clause_missing":
            return self._rule_clause_missing(rule, full_text, segments)
        return []

    def _rule_meta_missing(self, rule: dict, meta: ContractMeta) -> List[dict]:
        """检查元数据字段缺失。命中任一缺失字段即报一条发现。

        注意：本规则仅在文档已通过合同闸门后执行，
        因此这里的"缺失"是合同要素确实未被抽取/未约定，而非"整份文档不是合同"。
        """
        missing = [f for f in rule.get("fields", []) if not getattr(meta, f, None)]
        if not missing:
            return []
        return [{
            "segment_ref": None,
            "quote": f"缺失字段：{', '.join(missing)}",
            "start_offset": None,
            "end_offset": None,
        }]

    def _rule_meta_or_regex(self, rule: dict, meta: ContractMeta, full_text: str,
                            segments: List[ClauseSegment]) -> List[dict]:
        """主体资质类复合判定：元数据缺失 或 全文命中失信/异常表述，任一命中即报警。

        合并"字段缺失"与"措辞命中"两类信号，是因为二者指向同一业务结论
        （签约主体资质无法确认或存在瑕疵），合成一条规则更贴合审查清单的粒度，
        也让法务看到的是"主体资质"这一个待办项而非两条重复告警。
        """
        hits = self._rule_meta_missing(rule, meta)
        hits.extend(self._rule_text_regex(rule, full_text, segments))
        return hits

    def _rule_text_absent(self, rule: dict, full_text: str,
                          segments: List[ClauseSegment]) -> List[dict]:
        """条款内"缺失必备要件"判定。

        与 text_regex 相反：不是"出现某措辞就有问题"，而是
        「相关条款存在，却缺少应当具备的要件」才构成风险。
        典型场景：
          · 付款条款存在，但未把"验收合格"设为付款前置条件；
          · 保密条款存在，但未约定保密期限；
          · 提及不可抗力，但未约定通知时限。

        适用范围由两个字段决定：
          · target_category：限定在某个条款类别内检查；
          · if_present：要求片段先命中某措辞才纳入检查（用于类别粒度太粗的场景）。
        两者取并集，至少提供一个。
        这样可避免"只要出现'付款'字样就报风险"这类明显的假阳性。
        """
        target_cat = rule.get("target_category")
        if_present = rule.get("if_present", "")
        required = rule.get("required_pattern", "")
        if not required or (not target_cat and not if_present):
            return []

        if if_present:
            # 片段需先含该措辞才纳入检查（类别粒度不足时使用）
            scope = [s for s in segments if re.search(if_present, s.text)]
        else:
            scope = [s for s in segments if s.category == target_cat]
        if not scope:
            return []  # 相关条款整体不存在，属"条款缺失"另一维度，不在本规则职责内

        req_re = re.compile(required)
        hits: List[dict] = []
        for seg in scope:
            if req_re.search(seg.text):
                continue  # 已含必备要件 -> 合规
            quote = self._first_sentence(seg.text)
            start = (seg.start_offset + seg.text.find(quote)) if quote in seg.text else seg.start_offset
            hits.append({
                "segment_ref": seg.segment_id,
                "quote": quote,
                "start_offset": start,
                "end_offset": start + len(quote),
            })
            if len(hits) >= 2:  # 同类问题最多举证 2 处
                break
        return hits

    def _rule_clause_missing(self, rule: dict, full_text: str,
                             segments: List[ClauseSegment]) -> List[dict]:
        """"条款整体缺失"判定：整份合同里找不到该条款才报警。

        与 text_absent 的区别：
          · text_absent     —— 条款存在，但缺少必备要件（如保密条款无期限）；
          · clause_missing  —— 条款本身就不存在（如通篇没有保密条款、服务合同无数据安全条款）。
        两者互补，共同覆盖文档 2.4.4 中"必备条款缺失"这一类风险。

        判据二选一（按优先级）：
          · required_category / required_categories：按条款类别判定（多类别时命中任一缺失即报一条汇总发现）；
          · required_pattern：按措辞判定（全文连提都没提到才算缺失）。
        缺失类发现没有可高亮的原文片段，故 quote 用可读说明、偏移置空。
        """
        # 判据一：按条款类别
        cats: List[str] = list(rule.get("required_categories") or [])
        if not cats and rule.get("required_category"):
            cats = [rule["required_category"]]
        if cats:
            present = {s.category.value for s in segments}
            missing = [c for c in cats if c not in present]
            if not missing:
                return []
            quote = (f"缺少必备条款：{'、'.join(missing)}" if len(cats) > 1
                     else f"全文未检出「{missing[0]}」条款")
            return [{"segment_ref": None, "quote": quote,
                     "start_offset": None, "end_offset": None}]

        # 判据二：按措辞
        pattern = rule.get("required_pattern")
        if pattern and not re.search(pattern, full_text):
            return [{"segment_ref": None, "quote": f"全文未检出「{pattern}」相关约定",
                     "start_offset": None, "end_offset": None}]
        return []

    @staticmethod
    def _first_sentence(text: str, max_len: int = 48) -> str:
        """截取片段首句作为风险证据（优先断在句号/分号处，允许跨行）。"""
        flat = text.strip()
        m = re.search(r"^(.{4,%d}?)[。；;]" % max_len, flat, re.DOTALL)
        if m:
            return m.group(1)
        first_line = flat.split("\n")[0].strip()
        return (first_line or flat)[:max_len]

    def _rule_text_regex(self, rule: dict, full_text: str,
                         segments: List[ClauseSegment]) -> List[dict]:
        """在全文（或限定条款类别）内正则命中。每个匹配片段仅产出一条发现，避免同条款重复报警。"""
        pattern = rule.get("pattern", "")
        if not pattern:
            return []
        target_cat = rule.get("target_category")

        # 先确定待检索的条款片段集合
        if target_cat:
            scope = [s for s in segments if s.category == target_cat]
        else:
            scope = segments

        # 记录每个片段是否已命中（一个片段最多一条发现）
        seen_seg: set = set()
        hits: List[dict] = []

        def add_hit(quote: str, seg: ClauseSegment | None):
            # 偏移量优先在所属片段内定位，避免同一措辞在全文多处出现时锚错位置
            if seg is not None and quote in seg.text:
                start = seg.start_offset + seg.text.find(quote)
            else:
                start = full_text.find(quote)
            end = start + len(quote) if start >= 0 else None
            hits.append({
                "segment_ref": seg.segment_id if seg else None,
                "quote": quote,
                "start_offset": start if start >= 0 else None,
                "end_offset": end,
            })

        if target_cat:
            # 按条款类别：在对应片段内检索，每片段取首个命中
            for seg in scope:
                m = re.search(pattern, seg.text)
                if m and seg.segment_id not in seen_seg:
                    seen_seg.add(seg.segment_id)
                    add_hit(m.group(0), seg)
        else:
            # 全文检索：按命中位置归属片段去重
            for m in re.finditer(pattern, full_text):
                quote = m.group(0)
                seg = self._find_segment(segments, quote)
                # 去重键：命中落在某条款内则按片段去重，否则按命中原文去重
                key = seg.segment_id if seg is not None else quote
                if key in seen_seg:
                    continue
                seen_seg.add(key)
                add_hit(quote, seg)
                if len(hits) >= 3:
                    break
        return hits

    @staticmethod
    def _find_segment(segments: List[ClauseSegment], quote: str) -> Optional[ClauseSegment]:
        """按命中原文反查所属条款片段（用于把偏移量锚定到片段内，避免全文多处命中时错位）。"""
        for s in segments:
            if quote in s.text:
                return s
        return None
