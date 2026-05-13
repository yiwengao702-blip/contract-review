# 文件位置: contract-review/services/llm_review.py
# 功能说明: 大模型语义审查适配器。
#          设计目标：审查系统可离线运行（默认 mock 提供者，无需任何密钥），
#          也可一键切换到真实 LLM（OpenAI 兼容 /chat/completions 接口）做语义增强审查。
#          mock 提供器基于规则引擎结果做轻量语义补充（识别模糊/歧义表述），
#          真实提供器则调用模型对全文做合规研判并回传结构化发现。
#
#          关键工程决策（相对旧版 openai 包实现）：
#            · 用 requests 直连 OpenAI 兼容端点，而非依赖 openai 包——
#              托管运行环境（Python 3.13）未安装该包，直连方式任何装了 requests 的环境都能跑，
#              并天然兼容 DeepSeek / 通义 / 本地 vLLM / Azure 等任意 OpenAI 兼容网关。
#            · 未配置 LLM_API_KEY / LLM_BASE_URL 时，自动降级回 mock，审查不会因为 LLM 未就绪而整体失败。
#            · 真实调用失败（网络/超时/返回非法 JSON）一律吞掉并降级 mock，并打日志，
#              保证审查流水线始终能产出结果（规则引擎结果不受影响）。
from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

import requests

from config import cfg
from core.models import ClauseCategory, ClauseSegment, ContractMeta, RiskFinding, RiskLevel


# 合法风险等级归一映射：模型可能返回中文/英文/带后缀，统一成枚举值。
_LEVEL_MAP = {
    "high": RiskLevel.HIGH, "高": RiskLevel.HIGH, "高风险": RiskLevel.HIGH, "high_risk": RiskLevel.HIGH,
    "medium": RiskLevel.MEDIUM, "中": RiskLevel.MEDIUM, "中风险": RiskLevel.MEDIUM, "medium_risk": RiskLevel.MEDIUM,
    "low": RiskLevel.LOW, "低": RiskLevel.LOW, "低风险": RiskLevel.LOW, "low_risk": RiskLevel.LOW,
}


def _norm_level(value) -> RiskLevel:
    """把模型返回的风险等级文本安全地归一为 RiskLevel，无法识别一律按低风险处理。"""
    if isinstance(value, RiskLevel):
        return value
    key = str(value or "").strip().lower()
    return _LEVEL_MAP.get(key, RiskLevel.LOW)


class LLMReview:
    """LLM 语义审查服务（策略模式：mock / 真实 OpenAI 兼容接口）。"""

    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or cfg.LLM_PROVIDER

    def review(self, full_text: str, meta: ContractMeta,
               segments: List[ClauseSegment]) -> List[RiskFinding]:
        """执行大模型审查，返回额外风险发现（不含规则引擎已覆盖项）。

        路由逻辑：
          provider == "mock"                          -> 直接走离线 mock；
          provider != "mock" 但未配置 key/base_url    -> 自动降级 mock（不报错）；
          provider != "mock" 且已配置                 -> 调真实接口，失败再降级 mock。
        """
        if self.provider == "mock":
            return self._review_mock(full_text, meta, segments)

        if not (cfg.LLM_API_KEY and cfg.LLM_BASE_URL):
            # 选了真实模式却没给凭据：宁可降级也不让审查挂掉，但明确提示配置缺失。
            print("[llm_review] LLM_PROVIDER 为真实模式，但缺少 LLM_API_KEY / LLM_BASE_URL，"
                  "自动降级为 mock（规则引擎结果不受影响）。")
            return self._review_mock(full_text, meta, segments)

        try:
            return self._review_real(full_text, meta, segments)
        except Exception as exc:  # 真实调用任何异常都降级，保证审查不中断
            print(f"[llm_review] 真实 LLM 调用失败，降级为 mock：{type(exc).__name__}: {exc}")
            return self._review_mock(full_text, meta, segments)

    # ------------------------------------------------------------------ #
    # 离线 mock：基于规则的语义补充（识别模糊/歧义表述）
    # ------------------------------------------------------------------ #
    def _review_mock(self, full_text: str, meta: ContractMeta,
                     segments: List[ClauseSegment]) -> List[RiskFinding]:
        """离线 mock：识别语义层面的模糊/歧义表述，给出低风险提示。

        两个关键约束（此前缺失，是假阳性的主要来源）：
          1. 作用域：只在「已被识别为合同条款」的片段内扫描（类别非"其他"）。
             合同正文里的说明文字、附件内容、非条款段落不参与，避免把
             无关文本中的"适当/尽快"当成条款瑕疵；
          2. 去重：以「片段 + 措辞」为键，同一片段同一措辞只报一次，
             消除同一条款重复报警。
        """
        findings: List[RiskFinding] = []
        # 模糊表述模式（如“适当”“尽快”“合理”等无明确标准的措辞）
        vague_patterns = [
            (r"尽快", "时间约定模糊，未明确具体时限，易生履约争议。"),
            (r"适当", "“适当”标准主观，建议在合同中量化为可考核指标。"),
            (r"合理期限", "“合理期限”缺乏客观标准，建议明确天数。"),
            (r"等费用", "费用列举含“等”，开口条款易致金额失控，建议封闭列举。"),
        ]
        # 作用域：仅已识别类别的条款片段
        scope = [s for s in segments if s.category != ClauseCategory.OTHER]

        seen: set = set()      # (segment_id, pattern) 去重键
        counter = 0
        for pat, reason in vague_patterns:
            for seg in scope:
                m = re.search(pat, seg.text)
                if not m:
                    continue
                key = (seg.segment_id, pat)
                if key in seen:
                    continue
                seen.add(key)

                counter += 1
                quote = m.group(0)
                # 偏移量在片段内定位，保证高亮与原文严格对齐
                start = seg.start_offset + seg.text.find(quote)
                findings.append(
                    RiskFinding(
                        finding_id=f"L{counter:03d}",
                        level=RiskLevel.LOW,
                        category="条款表述",
                        segment_ref=seg.segment_id,
                        quote=quote,
                        start_offset=start,
                        end_offset=start + len(quote),
                        reason=reason,
                        legal_basis="",
                        recommendation="建议将模糊表述替换为可量化、可考核的具体标准。",
                        source="llm",
                    )
                )
                if counter >= 4:
                    return findings
        return findings

    # ------------------------------------------------------------------ #
    # 真实 LLM（OpenAI 兼容 /chat/completions）
    # ------------------------------------------------------------------ #
    def _endpoint(self) -> str:
        """拼出 /chat/completions 完整地址，兼容「带或不带后缀」两种 base 写法。"""
        base = (cfg.LLM_BASE_URL or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _review_real(self, full_text: str, meta: ContractMeta,
                     segments: List[ClauseSegment]) -> List[RiskFinding]:
        """调用 OpenAI 兼容接口做语义审查，含一次重试。失败由调用方兜底降级。"""
        url = self._endpoint()
        headers = {"Content-Type": "application/json"}
        if cfg.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {cfg.LLM_API_KEY}"

        prompt = (
            "你是一名资深企业法务 AI。请审查以下合同文本，识别合规与商业风险，"
            "仅输出 JSON 数组，每个元素包含字段：\n"
            "  level: 风险等级，取值 high / medium / low；\n"
            "  category: 风险维度，如 违约责任 / 管辖地 / 保密义务 / 付款条件 / 知识产权；\n"
            "  quote: 命中的原文片段（必须原样、连续，便于在原文中定位高亮）；\n"
            "  reason: 违规或风险成因说明；\n"
            "  legal_basis: 对应的合规/法律依据（如《民法典》第xxx条，没有则填空串）；\n"
            "  recommendation: 针对该风险的修改建议或示范条款。\n"
            "不要输出 JSON 以外的任何内容，不要用代码围栏包裹。\n\n合同文本：\n"
            + full_text[:12000]
        )
        payload = {
            "model": cfg.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg.LLM_TEMPERATURE,
            "stream": False,
        }

        last_err: Optional[Exception] = None
        for _ in range(2):  # 网络抖动重试一次
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=cfg.LLM_TIMEOUT)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"] or "[]"
                return self._parse_llm(content, full_text, segments)
            except Exception as exc:  # 网络/超时/HTTP 非 2xx/JSON 解析失败
                last_err = exc
                time.sleep(1.0)
        assert last_err is not None
        raise last_err

    def _parse_llm(self, content: str, full_text: str,
                   segments: List[ClauseSegment]) -> List[RiskFinding]:
        """把模型返回的（可能带代码围栏/说明文字的）文本解析为结构化风险发现。"""
        raw = self._extract_json_array(content)
        if raw is None:
            return []
        findings: List[RiskFinding] = []
        for i, item in enumerate(raw[:12]):
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "").strip()
            start = full_text.find(quote) if quote else -1
            # 命中片段定位到条款：优先用全文偏移反查，其次用片段包含关系
            seg_ref = None
            if start >= 0:
                for s in segments:
                    if s.start_offset <= start < s.end_offset:
                        seg_ref = s.segment_id
                        break
            if seg_ref is None:
                seg_ref = next((s.segment_id for s in segments if quote and quote in s.text), None)
            findings.append(
                RiskFinding(
                    finding_id=f"L{i+1:03d}",
                    level=_norm_level(item.get("level", "low")),
                    category=str(item.get("category") or "其他"),
                    segment_ref=seg_ref,
                    quote=quote,
                    start_offset=start if start >= 0 else None,
                    end_offset=(start + len(quote)) if start >= 0 else None,
                    reason=str(item.get("reason") or ""),
                    legal_basis=str(item.get("legal_basis") or ""),
                    recommendation=str(item.get("recommendation") or ""),
                    source="llm",
                )
            )
        return findings

    @staticmethod
    def _extract_json_array(text: str):
        """从模型输出里抠出 JSON 数组：先尝试整体 parse，再剥离 ```json 围栏，
        最后用首个 '[' 到最后一个 ']' 截取（兼容模型在前/后夹了说明文字的情况）。"""
        text = (text or "").strip()
        if not text:
            return None
        # 1) 直接就是 JSON
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, list) else None
        except json.JSONDecodeError:
            pass
        # 2) 剥离代码围栏 ```json ... ``` 或 ``` ... ```
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            inner = fence.group(1).strip()
            try:
                obj = json.loads(inner)
                if isinstance(obj, list):
                    return obj
            except json.JSONDecodeError:
                pass
        # 3) 截取首个 [ 到最后一个 ]
        a, b = text.find("["), text.rfind("]")
        if a != -1 and b != -1 and b > a:
            try:
                obj = json.loads(text[a:b + 1])
                return obj if isinstance(obj, list) else None
            except json.JSONDecodeError:
                pass
        return None
