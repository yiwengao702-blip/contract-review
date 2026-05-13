# 文件位置: contract-review/services/failure_gate.py
# 功能说明: 失败任务外发的「条件门禁」——"加条件审核"里的那层条件。
#
# 为什么需要它：
#   "审查失败就发钉钉"听起来合理，实际会把群变成噪音源。失败里混着两类完全不同的东西：
#     · 真问题：合同是加密扫描件、文件损坏、解析器啃不动 —— 需要人介入；
#     · 正常业务流转：经办人随手传了个技术文档，被闸门按"非合同"拦下 —— 本就该拦，无需打扰。
#   还有一类是"换一份文件就好了"的首次失败，立刻告警同样过敏感。
#   所以这里把"要不要外发"拆成若干条独立、可配置、**可解释**的条件。
#
# 设计要点：
#   ① 判定结果保留**逐条明细**，而不是只回一个布尔值。审核人看到的应当是
#      "这条为什么值得推到钉钉群"，而不是系统替他做的判断——这与项目里
#      合同闸门、OCR 降级一样，坚持"拒绝也要讲清理由"。
#   ② **空条件 = 不限**（而不是 = 全不选）。配置项留空是"不启用这项约束"，
#      这是最符合直觉的语义；对应判定项标记 skipped，不参与最终结论。
#   ③ **未通过也留痕**。条件拦下的任务同样生成记录（状态 filtered），
#      否则条件一旦配错（例如 min_retries 设成 3，导致永远不推），
#      界面上会静悄悄什么都不显示，人根本发现不了。
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from core.models import (
    FailureKind,
    GateCheck,
    GateItem,
    NotifyCondition,
    ReviewTask,
    TaskStatus,
)

# 可外发的失败类型及其可读说明（前端下拉与消息文案共用同一份定义）
FAILURE_KINDS: Dict[str, dict] = {
    FailureKind.BLOCKED.value: {
        "label": "解析受阻",
        "hint": "文档没能解析出正文（加密、无文本层的扫描件、格式异常等），审查未走完。",
    },
    FailureKind.REJECTED.value: {
        "label": "非合同被拒",
        "hint": "合同闸门判定输入并非合同类文档，未进入条款审查。属闸门正常工作结果。",
    },
}

# 可选的正文来源（与 ReviewTask.source 对齐）
SOURCES: Dict[str, str] = {"upload": "上传电子文档", "scan": "拍照识别"}


class FailureGate:
    """失败任务识别 + 外发条件判定。"""

    # ------------------------------------------------------------------ #
    # 失败识别
    # ------------------------------------------------------------------ #
    @staticmethod
    def failure_kind(task: ReviewTask) -> Optional[str]:
        """判断任务是否属于可外发的失败态，返回失败类型或 None。

        只有"审查这一步没走完"才算失败：
        pending / parsing 等中间态不算，completed（含发现高风险）也不算
        —— 审查成功但风险高，那是回写审批单要解决的事，不是失败外发。
        """
        if task.status == TaskStatus.BLOCKED:
            return FailureKind.BLOCKED.value
        if task.status == TaskStatus.REJECTED:
            return FailureKind.REJECTED.value
        return None

    # ------------------------------------------------------------------ #
    # 条件判定
    # ------------------------------------------------------------------ #
    def check(self, task: ReviewTask, condition: NotifyCondition,
              already_pushed: bool = False) -> GateCheck:
        """逐条判定外发条件。

        Args:
            already_pushed: 该任务此前是否已推送过（用于去重条件；由队列侧查历史得出）。
        """
        kind = self.failure_kind(task)
        items: List[GateItem] = []

        # ① 失败类型
        kind_label = FAILURE_KINDS.get(kind or "", {}).get("label", "非失败任务")
        want_labels = "、".join(FAILURE_KINDS.get(k, {}).get("label", k) for k in condition.kinds)
        items.append(GateItem(
            key="kind", label="失败类型",
            expected=f"属于 {want_labels or '（未指定）'}",
            actual=kind_label,
            passed=bool(kind and kind in condition.kinds),
        ))

        # ② 已重试次数
        if condition.min_retries > 0:
            items.append(GateItem(
                key="retries", label="重试次数",
                expected=f"已重试 ≥ {condition.min_retries} 次",
                actual=f"已重试 {task.retries} 次",
                passed=task.retries >= condition.min_retries,
            ))
        else:
            items.append(GateItem(
                key="retries", label="重试次数", expected="不限", actual="不限", passed=True, skipped=True,
            ))

        # ③ 文件后缀
        suffix = Path(task.filename or "").suffix.lower()
        if condition.extensions:
            items.append(GateItem(
                key="extensions", label="文件类型",
                expected=f"属于 {'、'.join(condition.extensions)}",
                actual=suffix or "（无后缀）",
                passed=suffix in condition.extensions,
            ))
        else:
            items.append(GateItem(
                key="extensions", label="文件类型",
                expected="不限", actual=suffix or "（无后缀）", passed=True, skipped=True,
            ))

        # ④ 正文来源
        src_text = SOURCES.get(task.source, task.source)
        if condition.sources:
            want = "、".join(SOURCES.get(s, s) for s in condition.sources)
            items.append(GateItem(
                key="sources", label="正文来源",
                expected=f"属于 {want}", actual=src_text,
                passed=task.source in condition.sources,
            ))
        else:
            items.append(GateItem(
                key="sources", label="正文来源", expected="不限", actual=src_text,
                passed=True, skipped=True,
            ))

        # ⑤ 失败原因关键词
        reason = task.error or ""
        if condition.reason_keywords:
            hits = [k for k in condition.reason_keywords if k.lower() in reason.lower()]
            items.append(GateItem(
                key="reason_keywords", label="失败原因关键词",
                expected=f"含 {'、'.join(condition.reason_keywords)}",
                actual=f"命中 {('、'.join(hits))}" if hits else "未命中",
                passed=bool(hits),
            ))
        else:
            items.append(GateItem(
                key="reason_keywords", label="失败原因关键词", expected="不限",
                actual=(reason[:24] + "…") if len(reason) > 24 else (reason or "（无）"),
                passed=True, skipped=True,
            ))

        # ⑥ 去重
        if condition.dedupe:
            items.append(GateItem(
                key="dedupe", label="重复外发",
                expected="该任务此前未推送过",
                actual="此前已推送过" if already_pushed else "尚未推送",
                passed=not already_pushed,
            ))
        else:
            items.append(GateItem(
                key="dedupe", label="重复外发", expected="不校验", actual="—", passed=True, skipped=True,
            ))

        effective = [it for it in items if not it.skipped]
        failed = [it for it in effective if not it.passed]
        passed = not failed
        if passed:
            summary = f"启用条件 {len(effective)} 项全部命中，进入人工审核队列。"
        else:
            detail = "；".join(f"{it.label}（要求 {it.expected}，实际 {it.actual}）" for it in failed)
            summary = f"未通过 {len(failed)} 项条件：{detail}，本次不进入外发队列。"
        return GateCheck(passed=passed, items=items, summary=summary)

    # ------------------------------------------------------------------ #
    # 批量扫描
    # ------------------------------------------------------------------ #
    def collect(self, tasks: Sequence[ReviewTask], condition: NotifyCondition,
                pushed_task_ids: Sequence[str]) -> Tuple[List[dict], List[dict], int]:
        """扫描全部任务，分出「可入队」与「被条件拦下」两拨。

        Returns:
            (passed, filtered, non_failure_count)
            passed  —— 条件通过的候选（含任务与判定明细）
            filtered —— 属失败态但条件未过的候选（同样带判定明细，用于留痕）
            non_failure_count —— 既非失败态、也无需解释的任务数
        """
        pushed = set(pushed_task_ids)
        passed: List[dict] = []
        filtered: List[dict] = []
        non_failure = 0

        for task in tasks:
            kind = self.failure_kind(task)
            if not kind:
                non_failure += 1
                continue
            check = self.check(task, condition, already_pushed=task.task_id in pushed)
            item = {
                "task_id": task.task_id,
                "filename": task.filename,
                "contract_type": task.contract_type,
                "failure_kind": kind,
                "reason": task.error or "",
                "retries": task.retries,
                "source": task.source,
                "gate": check,
            }
            (passed if check.passed else filtered).append(item)
        return passed, filtered, non_failure


# ---------------------------------------------------------------------- #
# 条件规范化
# ---------------------------------------------------------------------- #
def _as_list(raw, lower: bool = False) -> List[str]:
    """把前端传来的值统一成去重字符串列表。

    兼容三种输入：数组、逗号/顿号/空格分隔的字符串、None。
    前端用逗号分隔的输入框填关键词最省事，所以后端必须吃掉这个差异，
    而不是要求调用方必须先切成数组。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，、;；\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = []
        for v in raw:
            parts.extend(re.split(r"[,，、;；\s]+", str(v)))
    else:
        parts = [str(raw)]
    out: List[str] = []
    for p in parts:
        s = p.strip()
        if lower:
            s = s.lower()
        if s and s not in out:
            out.append(s)
    return out


def _as_extensions(raw) -> List[str]:
    """后缀统一成带点的小写形式：用户填 pdf / .PDF / .pdf 都该等价。"""
    out = []
    for s in _as_list(raw, lower=True):
        if not s.startswith("."):
            s = "." + s
        if s not in out:
            out.append(s)
    return out


def normalize_condition(raw: Optional[dict]) -> NotifyCondition:
    """把任意来源的条件字典规范成 NotifyCondition。

    非法值（如 kinds 里出现拼错的值）直接丢弃而不是报错：
    条件来自界面与配置文件两处，宁可退化到"少一条约束"也不要让整个外发链路 500。
    """
    raw = raw or {}
    kinds = [k for k in _as_list(raw.get("kinds"), lower=True) if k in FAILURE_KINDS]
    sources = [s for s in _as_list(raw.get("sources"), lower=True) if s in SOURCES]
    try:
        min_retries = max(0, int(raw.get("min_retries") or 0))
    except (TypeError, ValueError):
        min_retries = 0
    return NotifyCondition(
        # kinds 是全空就退回 blocked：与"空=不限"不同，失败类型必须有明确范围，
        # 否则一次误操作就会把闸门拦下的技术文档全部涌进钉钉群
        kinds=kinds or [FailureKind.BLOCKED.value],
        min_retries=min_retries,
        extensions=_as_extensions(raw.get("extensions")),
        sources=sources,
        reason_keywords=_as_list(raw.get("reason_keywords")),
        dedupe=bool(raw.get("dedupe", True)),
    )


def condition_to_dict(cond: NotifyCondition) -> dict:
    """转回普通 dict（存进配置 / 返回给前端）。"""
    return {
        "kinds": list(cond.kinds),
        "min_retries": cond.min_retries,
        "extensions": list(cond.extensions),
        "sources": list(cond.sources),
        "reason_keywords": list(cond.reason_keywords),
        "dedupe": cond.dedupe,
    }


def condition_labels(cond: NotifyCondition) -> List[str]:
    """把条件渲染成人话，供界面上的「当前生效条件」一行展示。"""
    out = [f"失败类型：{'、'.join(FAILURE_KINDS.get(k, {}).get('label', k) for k in cond.kinds)}"]
    if cond.min_retries > 0:
        out.append(f"已重试 ≥ {cond.min_retries} 次")
    if cond.extensions:
        out.append(f"文件类型：{'、'.join(cond.extensions)}")
    if cond.sources:
        out.append(f"来源：{'、'.join(SOURCES.get(s, s) for s in cond.sources)}")
    if cond.reason_keywords:
        out.append(f"原因含：{'、'.join(cond.reason_keywords)}")
    out.append("自动去重" if cond.dedupe else "允许重复外发")
    return out


# 模块级单例
failure_gate = FailureGate()
