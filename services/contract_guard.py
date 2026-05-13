# 文件位置: contract-review/services/contract_guard.py
# 功能说明: 合同文档闸门（DocumentGuard）——进入条款审查前的"是不是合同"准入校验。
#
#           设计缘由：
#             规则引擎与语义审查都假定"输入是合同"。若把技术笔记、面试题、产品说明、
#             规章制度等非合同文本直接送进去，会出现大面积假阳性：
#               · 主体信息规则报"主体资质高风险"（其实文档根本没有签约主体）；
#               · 条款类别兜底成"其他"，模糊措辞规则把正文里的"适当/尽快"当成条款瑕疵。
#             因此在解析成功之后、规则审查之前，必须先过这道闸门。
#
#           判定方式（可解释、可调参）：
#             ① 正向特征加权计分：标题 / 签约主体标识 / 条款结构 / 合同要素 / 签署要素；
#             ② 负向特征扣分：技术文档与代码类特征词、Markdown 代码块；
#             ③ 双条件把关：必须命中"标题或主体标识"之一，且总分达到阈值，才判为合同；
#             ④ 强否定短路：负向特征密集且无合同标题时，直接判非合同。
#
#           继承 NodeBase 并声明 node_name，自动登记进节点注册表，
#           后续若要追加"合同类型分类""语言/地区校验"等维度，可平行扩展新节点。
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from core.models import DocumentCheck
from core.node_base import NodeBase


class ContractGuard(NodeBase):
    """合同文档闸门：判定输入文档是否属于合同/协议类文本。"""

    # 自动注册键
    node_name = "contract_guard"

    # ---- 判为合同所需的最低得分（正向特征满分约 13 分，真实合同通常 >= 10）----
    SCORE_THRESHOLD: float = 6.0
    # ---- 强否定：负向特征命中数达到该值且无合同标题，直接判非合同 ----
    NEGATIVE_CUTOFF: int = 5

    # 合同标题特征（命中即认为文档自述为合同类）
    TITLE_PATTERN = re.compile(
        r"(合同|协议书|协议|契约|订单|意向书|备忘录|承诺书|框架协议|补充协议|条款书)"
    )

    # 签约主体标识（合同必有相对方角色）
    PARTY_PATTERN = re.compile(
        r"(甲方|乙方|丙方|买方|卖方|需方|供方|委托方|受托方|发包方|承包方|"
        r"出租方|承租方|出租人|承租人|服务方|客户方|供方单位|采购方|供应商)"
    )

    # 条款结构特征：「第X条」「第X章」「一、」式编号
    CLAUSE_PATTERN = re.compile(
        r"(第[一二三四五六七八九十百零0-9]{1,4}条|第[一二三四五六七八九十]{1,3}[章节]|^[一二三四五六七八九十]{1,2}[、.])",
        re.MULTILINE,
    )

    # 合同要素关键词（出现越多越像合同）
    ELEMENT_KEYWORDS: List[str] = [
        "标的", "价款", "付款", "交付", "验收", "违约", "赔偿",
        "保密", "争议", "管辖", "仲裁", "诉讼", "不可抗力",
        "生效", "解除", "知识产权", "履行期限", "结算",
    ]

    # 签署要素
    SIGN_PATTERN = re.compile(
        r"(签字|签章|盖章|法定代表人|授权代表|签订日期|签署日期|落款|合同专用章)"
    )

    # 非合同负向特征：技术文档 / 教学材料 / 代码痕迹
    NEGATIVE_PATTERN = re.compile(
        r"(函数|变量|数据结构|闭包|递归|算法|时间复杂度|面试|笔试题|真题|题库|"
        r"知识点|返回值|参数|形参|实参|import\s|def\s|print\s*\(|console\.log|"
        r"报错|异常信息|安装教程|环境配置|命名空间|编译|语法|代码示例|示例代码)"
    )

    # Markdown 代码块/行内代码痕迹
    CODE_BLOCK_PATTERN = re.compile(r"(```|`[^`\n]{2,}`|\*\*[^*\n]{2,}\*\*)")

    # ------------------------------------------------------------------ #
    def run(self, context: dict) -> dict:
        """NodeBase 统一接口：从上下文取全文，写入 doc_check 结果。"""
        full_text = context.get("full_text", "") or ""
        context["doc_check"] = self.check(full_text)
        return context

    # ------------------------------------------------------------------ #
    def check(self, full_text: str) -> DocumentCheck:
        """执行合同属性校验，返回可解释的判定结果。

        Args:
            full_text: 解析后的文档全文纯文本。
        Returns:
            DocumentCheck：是否合同、得分、命中信号明细与拒绝原因。
        """
        text = full_text or ""
        if not text.strip():
            return DocumentCheck(
                is_contract=False,
                score=0.0,
                threshold=self.SCORE_THRESHOLD,
                clause_count=0,
                reject_reason="文档未提取到任何文本内容，无法判定文档类型。",
            )

        # ---- ① 正向特征采集 ----
        title_m = self.TITLE_PATTERN.search(text)
        party_m = self.PARTY_PATTERN.search(text)
        clause_count = len(self.CLAUSE_PATTERN.findall(text))
        element_hits = [kw for kw in self.ELEMENT_KEYWORDS if kw in text]
        sign_m = self.SIGN_PATTERN.search(text)

        # ---- ② 负向特征采集 ----
        negative_hits = self.NEGATIVE_PATTERN.findall(text)
        code_block_hits = self.CODE_BLOCK_PATTERN.findall(text)

        # ---- ③ 加权计分 ----
        score = 0.0
        if title_m:
            score += 3.0                                  # 自述为合同类
        if party_m:
            score += 3.0                                  # 存在签约主体角色
        score += min(clause_count, 3)                     # 条款结构（最多 3 分）
        score += min(len(element_hits), 5) * 0.6          # 合同要素（最多 3 分）
        if sign_m:
            score += 1.0                                  # 签署要素
        # 负向扣分：技术/代码特征（每项 0.7 分，最多扣 5 分），代码块再扣 2 分
        score -= min(len(negative_hits) * 0.7, 5.0)
        if code_block_hits:
            score -= 2.0

        has_title = bool(title_m)
        has_party = bool(party_m)

        # ---- ④ 强否定短路：技术特征密集且无合同标题 ----
        if len(negative_hits) >= self.NEGATIVE_CUTOFF and not has_title:
            reason = (
                "文档大量出现技术/教学类特征词（如 " + "、".join(negative_hits[:5]) +
                "），且无合同或协议类标题，判定为非合同文档，已跳过条款审查以避免误报。"
            )
            return self._result(False, "not_contract", score, title_m, party_m, clause_count,
                                element_hits, sign_m, negative_hits, code_block_hits, reason)

        # ---- ⑤ 双条件把关 ----
        passed = (has_title or has_party) and score >= self.SCORE_THRESHOLD
        if passed:
            return self._result(True, "contract", score, title_m, party_m, clause_count,
                                element_hits, sign_m, negative_hits, code_block_hits, "")

        # ---- ⑥ 证据矛盾：标题自述为合同，正文却读不出任何合同要件 ----
        # 典型成因是 OCR 失败（照片模糊/分辨率过低/表格版面），而非"这份文件不是合同"。
        # 若仍按"非合同"拒绝，用户看到"您传的不是合同"只会重传同一张照片、再失败一次；
        # 判为 undetermined 才能把处置指向"提高照片质量 / 人工校对正文"。
        body_evidence = int(has_party) + clause_count + len(element_hits)
        if has_title and body_evidence == 0:
            reason = (
                f"文档标题自称「{title_m.group(0)}」，但正文未识别到任何签约主体、条款结构或合同要素"
                f"（合同特征得分 {score:.1f}）。这通常是文字识别失败所致，而非该文档不是合同："
                "建议重新拍摄清晰、平整、分辨率更高的照片，或在人工校对区补录正文后重新提交。"
            )
            return self._result(False, "undetermined", score, title_m, party_m, clause_count,
                                element_hits, sign_m, negative_hits, code_block_hits, reason)

        # 未通过：拼装可读的拒绝原因，指明缺了哪些合同要件
        missing: List[str] = []
        if not has_title:
            missing.append("合同/协议类标题")
        if not has_party:
            missing.append("签约主体（甲方/乙方等）")
        if clause_count == 0:
            missing.append("条款结构（第X条）")
        if len(element_hits) < 2:
            missing.append("合同要素（标的/价款/违约/保密等）")
        reason = (
            f"文档缺少{'、'.join(missing) or '必要的合同要件'}，"
            f"合同特征得分 {score:.1f}（阈值 {self.SCORE_THRESHOLD:.0f}），"
            "判定为非合同文档，未进入条款审查。"
        )
        return self._result(False, "not_contract", score, title_m, party_m, clause_count,
                            element_hits, sign_m, negative_hits, code_block_hits, reason)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _result(is_contract: bool, verdict: str, score: float,
                title_m: Optional[re.Match], party_m: Optional[re.Match],
                clause_count: int, element_hits: List[str],
                sign_m: Optional[re.Match], negative_hits: List[str],
                code_block_hits: List[str], reason: str) -> DocumentCheck:
        """把各项命中信号整理为统一的 DocumentCheck 结构（供前端展示判定依据）。

        verdict 与 is_contract 不是一回事：
          is_contract 只回答"能不能进条款审查"；
          verdict 回答"为什么"——contract / not_contract / undetermined。
          区分出的 undetermined 用于把处置指向"提高照片质量"，而不是让用户重传同一张照片。
        """
        return DocumentCheck(
            is_contract=is_contract,
            verdict=verdict,
            score=round(score, 2),
            threshold=ContractGuard.SCORE_THRESHOLD,
            title_hit=title_m.group(0) if title_m else None,
            party_hit=party_m.group(0) if party_m else None,
            clause_count=clause_count,
            element_hits=element_hits,
            sign_hit=bool(sign_m),
            negative_hits=list(dict.fromkeys(negative_hits)),   # 去重但保留原始顺序
            code_block_hits=len(code_block_hits),
            reject_reason=reason,
        )
