# 文件位置: contract-review/services/doc_parser.py
# 功能说明: 文档解析引擎。
#          支持 DOCX / PDF / 图片扫描件（OCR 可选降级）三类合同附件解析，
#          输出：① 合同全文纯文本（带偏移量，供前端高亮）；② 核心元数据；
#          ③ 按类别层级切分的条款段落树（每条记录页码与字符偏移，可双向溯源）。
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from core.models import ClauseCategory, ClauseSegment, ContractMeta
# 复用 OCR 侧的页分隔符常量，保证"提交时怎么拼"与"解析时怎么切"永远一致
# （依赖方向是单向的：doc_parser -> ocr_service，不会成环）
from services.ocr_service import PAGE_SEPARATOR

# 条款类别关键词 -> 类别枚举（用于层级切分与风险锚定）
CATEGORY_KEYWORDS: List[Tuple[str, ClauseCategory]] = [
    ("标的", ClauseCategory.SUBJECT),
    ("价款", ClauseCategory.PAYMENT),
    ("付款", ClauseCategory.PAYMENT),
    ("支付", ClauseCategory.PAYMENT),
    ("验收", ClauseCategory.ACCEPTANCE),
    ("违约", ClauseCategory.LIABILITY),
    ("赔偿", ClauseCategory.LIABILITY),
    ("保密", ClauseCategory.CONFIDENTIALITY),
    ("个人信息", ClauseCategory.PII),
    ("数据", ClauseCategory.DATA_SECURITY),
    ("安全", ClauseCategory.DATA_SECURITY),
    ("争议", ClauseCategory.JURISDICTION),
    ("管辖", ClauseCategory.JURISDICTION),
    ("仲裁", ClauseCategory.JURISDICTION),
    ("诉讼", ClauseCategory.JURISDICTION),
    ("纠纷", ClauseCategory.JURISDICTION),
    # 廉洁合规（反商业贿赂）：仅在标题行含以下关键词时才归类为廉洁合规条款，
    # 缺失该类标题即被 clause_missing 判为「缺少反商业贿赂条款」。
    ("廉洁", ClauseCategory.COMPLIANCE),
    ("贿赂", ClauseCategory.COMPLIANCE),
    ("回扣", ClauseCategory.COMPLIANCE),
    ("商业贿赂", ClauseCategory.COMPLIANCE),
    ("反腐败", ClauseCategory.COMPLIANCE),
    # 合同解除与终止：放在违约责任（违约/赔偿）之后，
    # 保证「违约责任与合同解除」这类复合标题仍优先归入「违约责任」，不被抢类。
    ("解除", ClauseCategory.TERMINATION),
    ("终止", ClauseCategory.TERMINATION),
    ("续约", ClauseCategory.TERMINATION),
    ("续展", ClauseCategory.TERMINATION),
]


class DocParser:
    """合同文档解析服务（无状态，可复用实例）。"""

    def parse(self, file_path: str) -> dict:
        """解析单个合同文件。

        Returns:
            {
              "full_text": str,
              "pages": List[str],          # 每页文本
              "meta": ContractMeta,
              "segments": List[ClauseSegment],
              "parse_method": str,         # docx / pdf / ocr
              "ocr_used": bool,
            }
        Raises:
            ValueError: 文件类型不支持或文本为空。
        """
        path = Path(file_path)
        suffix = path.suffix.lower()

        if suffix in (".docx", ".doc"):
            full_text, pages = self._parse_docx(path)
            parse_method = "docx"
            ocr_used = False
        elif suffix == ".pdf":
            # PDF 优先尝试文本层；文本层为空（扫描件）则交给 OCR 适配层
            full_text, pages = self._parse_pdf(path)
            parse_method = "pdf"
            ocr_used = False
            if not full_text.strip():
                full_text, pages, ocr_used = self._try_ocr(path)
                parse_method = "ocr"
        elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"):
            # 图片/照片：走可插拔 OCR 适配层；无可用引擎时抛带指引的 ValueError
            full_text, pages, ocr_used = self._try_ocr(path)
            parse_method = "ocr"
        elif suffix in (".txt", ".md"):
            # 纯文本：来自「拍照识别」弹窗中的人工录入/校正结果，直接作为合同正文
            full_text = path.read_text(encoding="utf-8", errors="ignore")
            # 空白页不过滤：保留它占的分页位置，页码才能与用户拍的照片一一对应
            # （详见 _segment_clauses 中 page_of 的说明）
            pages = full_text.split(PAGE_SEPARATOR) or [full_text]
            parse_method = "text"
            ocr_used = False
        else:
            raise ValueError(f"不支持的文件类型: {suffix}")

        if not full_text.strip():
            # 文本层与 OCR 均无内容 -> 交由上层进入 blocked 状态
            raise ValueError("未能从文档提取到任何文本内容（可能为加密/严重模糊扫描件）")

        meta = self._extract_meta(full_text)
        segments = self._segment_clauses(full_text, pages)
        return {
            "full_text": full_text,
            "pages": pages,
            "meta": meta,
            "segments": segments,
            "parse_method": parse_method,
            "ocr_used": ocr_used,
        }

    # ------------------------------------------------------------------ #
    # 具体格式解析
    # ------------------------------------------------------------------ #
    def _parse_docx(self, path: Path) -> Tuple[str, List[str]]:
        """用 python-docx 提取文本，按段落聚合为“页”（段落分组模拟分页）。"""
        try:
            from docx import Document
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("缺少依赖 python-docx，请执行 pip install python-docx") from e

        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        full_text = "\n".join(paragraphs)
        # DOCX 无物理页码，按每 30 段模拟一页便于定位展示
        pages = []
        for i in range(0, len(paragraphs), 30):
            pages.append("\n".join(paragraphs[i:i + 30]))
        return full_text, pages or [full_text]

    def _parse_pdf(self, path: Path) -> Tuple[str, List[str]]:
        """用 pdfplumber 提取 PDF 文本，保留真实页码。"""
        try:
            import pdfplumber
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("缺少依赖 pdfplumber，请执行 pip install pdfplumber") from e

        pages: List[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                pages.append(txt)
        full_text = "\n".join(pages)
        return full_text, pages

    def _try_ocr(self, path: Path) -> Tuple[str, List[str], bool]:
        """对图片/扫描件执行 OCR，交由可插拔的 OCRService 按优先级尝试各引擎。

        与旧实现的区别：引擎全部不可用时不再静默返回空文本（上层只能报"解析受阻"），
        而是抛出带有明确原因与出路的 ValueError —— 用户看到的是
        "本机未安装可用的 OCR 引擎…… 可改用拍照识别弹窗人工录入"，
        而不是一句无从下手的"未能提取到文本"。
        """
        from services.ocr_service import ocr_service

        result = ocr_service.recognize(path)
        if result.ok:
            text = result.text
            return text, [text], True
        # 降级：把图片体检建议一并写进错误信息，让用户知道是"图源质量"还是"没装引擎"
        # 措辞用"图片质量"而非"拍摄"，因为这条路径同样服务于 PDF 扫描件（无法重拍、只能重新扫描）
        tips = "；".join(result.quality.warnings)
        detail = result.reason + (f"（图片质量提示：{tips}）" if tips else "")
        raise ValueError(detail + " " + result.hint)

    # ------------------------------------------------------------------ #
    # 元数据抽取
    # ------------------------------------------------------------------ #
    def _extract_meta(self, text: str) -> ContractMeta:
        """基于正则与启发式规则抽取核心元数据。"""
        meta = ContractMeta()

        # 统一社会信用代码（18 位，含数字与大写字母）
        m = re.search(r"统一社会信用代码[：:]\s*([0-9A-HJ-NPQRTUWXY]{18})", text)
        if not m:
            m = re.search(r"信用代码[：:]\s*([0-9A-HJ-NPQRTUWXY]{18})", text)
        if m:
            meta.credit_code = m.group(1)

        # 合同金额（支持 “¥1,200,000 元” / “120 万元” 等写法）
        amt = re.search(
            r"(合同金额|总金额|总价|价款)[：:]?\s*[¥￥]?\s*([0-9][0-9,]*\.?[0-9]*)[\s]*((万)?元)?",
            text,
        )
        if amt:
            num_str = amt.group(2).replace(",", "")
            try:
                value = float(num_str)
                if amt.group(3) and "万" in amt.group(3):
                    value *= 10000
                meta.amount = value
            except ValueError:
                pass
            if amt.group(3) and "万" in amt.group(3):
                meta.currency = meta.currency or "人民币"

        # 币种
        cur = re.search(r"(币种|货币)[：:]\s*(人民币|美元|欧元|港币|日元)", text)
        if cur:
            meta.currency = cur.group(1) if False else cur.group(2)
        elif "美元" in text and "US$" in text:
            meta.currency = "美元"

        # 履行期限
        dur = re.search(r"(履行期限|合同期限|期限)[：:]\s*([^\n；;]{2,40})", text)
        if dur:
            meta.duration = dur.group(2).strip()

        # 生效条件
        eff = re.search(r"(生效条件|自[^\n]{0,10}起生效|本合同自)[：:]?\s*([^\n；;]{2,50})", text)
        if eff:
            meta.effective_condition = eff.group(2).strip()

        # 甲方 / 乙方主体名称（兼容「甲方：公司名」与「甲方（角色）：公司名」两种写法）
        # 主体名遇到这些字符即截断：全角空格/冒号/顿号等分隔符都表示"字段到此结束"，
        # 否则「甲方：A公司　乙方：B公司」这种同行的写法会把乙方一起并进甲方字段。
        stop = r"[^\n，,；;：:\u3000]"
        pa = re.search(r"甲方(?:\s*[（(][^）)]{0,10}[）)])?\s*[：:]\s*(" + stop + r"{2,40})", text)
        pb = re.search(r"乙方(?:\s*[（(][^）)]{0,10}[）)])?\s*[：:]\s*(" + stop + r"{2,40})", text)
        if pa:
            meta.party_a = pa.group(1).strip()
        if pb:
            meta.party_b = pb.group(1).strip()

        return meta

    # ------------------------------------------------------------------ #
    # 条款层级切分
    # ------------------------------------------------------------------ #
    def _segment_clauses(self, full_text: str, pages: List[str]) -> List[ClauseSegment]:
        """将全文按“第X条”条款边界切分为可溯源片段。

        规则：
          - 以换行切分段落；
          - 命中「第X条」标题的段落开启新片段，并以标题中的关键词判定类别；
          - 非标题段落作为上一片段的续写（继承其类别）；
          - 标题之前的元信息（合同名称/主体/金额等）归为“其他”片段；
          - 每个片段记录起止偏移与估算页码，供前端双向高亮定位。
        """
        paragraphs = [p for p in re.split(r"\n+", full_text) if p.strip()]
        segments: List[ClauseSegment] = []
        page_text_len = sum(len(p) + 1 for p in pages) or 1

        # 页码归属：文本带分页符（拍照多页合同）时按分页符精确归属到页，
        # 让报告里的「第 N 页」严格等于用户拍的第 N 张照片；
        # 无分页符（DOCX/PDF 会把各页拼成一段）时解析器根本无从得知真实页边界，
        # 只能按字符长度估算 —— 那种情况下估算已是能做到的最好。
        page_starts: List[int] = []
        if PAGE_SEPARATOR in full_text:
            cursor = 0
            for part in pages:
                page_starts.append(cursor)
                cursor += len(part) + len(PAGE_SEPARATOR)

        def page_of(offset: int) -> int:
            if page_starts:
                # 取"最后一个起始偏移不超过该段落"的页；空页虽然内容为空，
                # 但它占住了位次，后续段落不会被误算成前一页。
                page = 1
                for i, start in enumerate(page_starts):
                    if start <= offset:
                        page = i + 1
                return page
            return max(1, int(offset / page_text_len * len(pages)) + 1) if pages else 1
        heading_re = re.compile(r"^第[一二三四五六七八九十百零0-9]+条")

        cur_cat = ClauseCategory.OTHER
        cur_parts: List[str] = []
        cur_start: int = 0
        pos = 0  # 全文扫描游标，保证偏移单调递增不重叠
        seg_id = 0

        def flush():
            nonlocal cur_parts, cur_start, seg_id
            if not cur_parts:
                return
            seg_text = "\n".join(cur_parts).strip()
            if seg_text:
                start = cur_start
                end = start + len(seg_text)
                page_no = page_of(start)
                segments.append(
                    ClauseSegment(
                        segment_id=f"seg_{seg_id}",
                        category=cur_cat,
                        text=seg_text,
                        start_offset=start,
                        end_offset=end,
                        page=page_no,
                        paragraph_index=seg_id,
                    )
                )
                seg_id += 1
            cur_parts = []

        for p in paragraphs:
            is_heading = bool(heading_re.match(p.strip()))
            start = full_text.find(p, pos)
            if start < 0:
                start = pos
            if is_heading:
                flush()  # 收尾上一片段（若有）
                cur_cat = self._detect_category(p) or ClauseCategory.OTHER
                cur_start = start
                cur_parts = [p]
            else:
                if not cur_parts:  # 首个非标题段（元信息）
                    cur_cat = ClauseCategory.OTHER
                    cur_start = start
                cur_parts.append(p)
            pos = start + len(p) + 1

        flush()

        if not segments:
            segments.append(
                ClauseSegment(
                    segment_id="seg_0",
                    category=ClauseCategory.OTHER,
                    text=full_text.strip(),
                    start_offset=0,
                    end_offset=len(full_text),
                    page=1,
                    paragraph_index=0,
                )
            )
        return segments

    @staticmethod
    def _detect_category(paragraph: str) -> Optional[ClauseCategory]:
        """根据段落文本判定其所属条款类别（首命中优先）。"""
        for kw, cat in CATEGORY_KEYWORDS:
            if kw in paragraph:
                return cat
        return None
