# 文件位置: contract-review/services/export_queue.py
# 功能说明: 失败任务外发审核队列——"加条件审核"里的"审核"那一半。
#
# 链路：审查失败 → 条件门禁筛一遍 → 通过的进待审核队列 → 责任人确认 → 推送钉钉 → 留痕
#
# 为什么条件之后还要人工确认，而不是条件过了就自动发：
#   外发钉钉是对外动作（群里可能还有客户、合作方），条件只能筛掉"明显不该发"的，
#   筛不出"该发但现在时机不对"。把最后一次点头交给责任人，是这类通知功能的基本分寸。
#
# 设计要点：
#   ① **记录在入队时就冻结要发的正文**（record.message）。审核人预览到的与实际发出的
#      严格同一份，杜绝"看到的和发出去的不一样"。
#   ② **被条件拦下的也留痕**（FILTERED）。条件配错时（例如把 min_retries 设成 3
#      导致永远够不着），界面上必须还有东西可看，否则人会以为系统坏了。
#   ③ **驳回必须写理由**。理由为空直接拒绝——否则队列里的记录会被人无声无息清空，
#      而"为什么没发出去"恰恰是这类功能最需要能回溯的信息。
#   ④ 推送失败可重试，且**重试不改正文**：正文是审核过的，重试只是把同一份重发一次。
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional, Sequence

from config import cfg, DEFAULT_EXPORT_CONDITION
from core.models import (
    ExportRecord,
    ExportStatus,
    PushResult,
    ReviewTask,
)
from services.dingtalk import dingtalk
from services.failure_gate import (
    condition_labels,
    condition_to_dict,
    failure_gate,
    normalize_condition,
)

# 队列里"还没处理完"的状态：这类任务的失败事实不该被再次入队，否则每次扫描都堆一条重复项
_OPEN_STATUSES = {ExportStatus.PENDING.value, ExportStatus.PUSH_FAILED.value}
# 已终结的状态：dedupe 条件据此判断"该任务此前已处理过"
_HANDLED_STATUSES = {ExportStatus.PUSHED.value, ExportStatus.HUMAN_REJECTED.value}


class ExportQueue:
    """失败任务外发审核队列（演示用内存存储，进程重启即清空）。"""

    def __init__(self) -> None:
        # 记录列表，按入队时间倒序维护（最新的排前面，前端直接渲染）
        self._records: List[ExportRecord] = []
        # 记录操作日志（谁在什么时候做了什么），与回执分开存，便于审计追溯
        self._audit: List[dict] = []

    # ------------------------------------------------------------------ #
    # 条件配置
    # ------------------------------------------------------------------ #
    def current_condition(self) -> dict:
        """当前生效的外发条件（含人类可读描述与出厂默认）。

        defaults 给的是**出厂默认**而不是当前值：界面上的「恢复默认」要回到初始状态，
        如果这里回的是当前值，那个按钮会变成没有任何作用的样子。
        """
        cond = normalize_condition(cfg.EXPORT_CONDITION)
        return {
            "condition": condition_to_dict(cond),
            "labels": condition_labels(cond),
            "defaults": dict(DEFAULT_EXPORT_CONDITION),
        }

    def update_condition(self, raw: Optional[dict]) -> dict:
        """更新外发条件。

        条件改动**只影响后续扫描**，不会回溯改动已有记录的判定结果：
        已入队的记录保留入队当时的判定明细，否则审计链就断了
        （"这条为什么会在队列里"必须能按当时条件解释）。
        """
        cond = normalize_condition(raw)
        cfg.EXPORT_CONDITION = condition_to_dict(cond)
        self._log("condition_update", "-", "system", "更新外发条件："
                  + "；".join(condition_labels(cond)))
        return self.current_condition()

    # ------------------------------------------------------------------ #
    # 入队
    # ------------------------------------------------------------------ #
    def scan(self, tasks: Sequence[ReviewTask]) -> dict:
        """扫描全部任务：条件通过的入队待审核，未通过的留痕。

        Returns:
            本次扫描统计（新增待审核 / 被条件拦下 / 队列内已有 / 非失败任务数）。
        """
        cond = normalize_condition(cfg.EXPORT_CONDITION)
        # 队列内已有记录的任务：既不算"已处理过"（不能因此判去重不过），
        # 也不能重复入队，单独统计出来，让界面能解释"为什么这次没新增"
        in_queue = {r.task_id for r in self._records if r.status.value in _OPEN_STATUSES}
        handled = {r.task_id for r in self._records if r.status.value in _HANDLED_STATUSES}
        # 推送失败的任务允许重新入队（那是技术故障，不是"不该发"），
        # 但队列里已有待重试记录时不再新增，避免同一条失败反复堆叠
        already_pushed = handled

        passed, filtered, non_failure = failure_gate.collect(tasks, cond, already_pushed)

        added: List[ExportRecord] = []
        skipped_in_queue = 0
        for item in passed:
            if item["task_id"] in in_queue:
                skipped_in_queue += 1
                continue
            rec = self._make_record(item, ExportStatus.PENDING)
            self._records.insert(0, rec)
            added.append(rec)
            self._log("enqueue", rec.record_id, "system",
                      f"任务 {rec.task_id} 通过条件门禁，进入待审核队列")

        filtered_added = 0
        for item in filtered:
            if item["task_id"] in in_queue:
                continue
            # 同一任务反复扫描不重复留痕，但**失败原因变了就要留新的一条**：
            # "同一个文件一直失败"和"换了文件换了原因还是失败"是不同的信息。
            if any(r.task_id == item["task_id"] and r.status == ExportStatus.FILTERED
                   and r.reason == item["reason"] for r in self._records):
                continue
            rec = self._make_record(item, ExportStatus.FILTERED)
            self._records.insert(0, rec)
            filtered_added += 1
            # 被拦下也要留痕：否则"这个失败任务为什么没通知我"只能靠翻记录本身，
            # 审计日志里却查不到——而漏发正是这类功能最该被追责的场景。
            self._log("filtered", rec.record_id, "system",
                      f"任务 {rec.task_id} 未通过条件门禁，未进入外发队列：{rec.gate.summary}")

        self._trim()
        return {
            "queued": len(added),
            "filtered": filtered_added,
            "already_in_queue": skipped_in_queue,
            "non_failure": non_failure,
            "scanned": len(tasks),
            "queued_ids": [r.record_id for r in added],
            "condition": condition_labels(cond),
        }

    # ------------------------------------------------------------------ #
    # 人工审核
    # ------------------------------------------------------------------ #
    def approve(self, record_id: str, operator: str, note: str = "") -> ExportRecord:
        """审核通过并推送。"""
        rec = self._get(record_id)
        if rec.status not in (ExportStatus.PENDING, ExportStatus.PUSH_FAILED):
            raise ValueError(f"该记录当前状态为「{_status_label(rec.status)}」，无需再次审核")
        rec.reviewed_by = operator or rec.reviewed_by
        if note:
            rec.review_note = note
        return self._push(rec, operator, action="approve")

    def reject(self, record_id: str, operator: str, note: str) -> ExportRecord:
        """人工驳回：不外发，但必须留下理由。"""
        rec = self._get(record_id)
        if rec.status not in (ExportStatus.PENDING, ExportStatus.PUSH_FAILED):
            raise ValueError(f"该记录当前状态为「{_status_label(rec.status)}」，无需再次审核")
        # 强制理由：队列里的记录被无声清空后，"为什么没发出去"就再也查不到了
        if not (note or "").strip():
            raise ValueError("驳回必须填写理由（用于回溯「为何该失败未外发」）")
        rec.status = ExportStatus.HUMAN_REJECTED
        rec.reviewed_by = operator or rec.reviewed_by
        rec.review_note = note.strip()
        rec.updated_at = _now()
        self._log("reject", rec.record_id, operator, f"驳回：{rec.review_note}")
        return rec

    def retry_push(self, record_id: str, operator: str) -> ExportRecord:
        """推送失败后重试。正文沿用审核时冻结的那份，不重新生成。"""
        rec = self._get(record_id)
        if rec.status != ExportStatus.PUSH_FAILED:
            raise ValueError(f"仅「推送失败」的记录可重试，当前状态为「{_status_label(rec.status)}」")
        return self._push(rec, operator, action="retry")

    def revoke_pending(self, task_id: str, reason: str) -> int:
        """作废某任务所有待审核记录（任务被重新审查时调用）。

        为什么必须在**新一次审查启动时**就作废：队列里那条记录冻结的是上一次的
        失败原因与消息正文。若用户把加密件换成正常文件重试成功，而旧记录还在队列里，
        审核人一点「确认推送」，钉钉群里就会出现一条早就不成立的失败提醒——假警报
        比不提醒更伤信任。这里不做多版本比对，直接作废更简单也更安全。
        """
        n = 0
        for r in self._records:
            if r.task_id == task_id and r.status in (ExportStatus.PENDING, ExportStatus.PUSH_FAILED):
                r.status = ExportStatus.SUPERSEDED
                r.review_note = reason
                r.updated_at = _now()
                n += 1
        if n:
            self._log("revoke", task_id, "system", f"作废 {n} 条待审核记录：{reason}")
        return n

    def test_channel(self, operator: str) -> dict:
        """发送一条通道测试消息，用于确认 Webhook / 加签 / 关键词配置是否正确。"""
        title, text = dingtalk.build_test_message()
        result = dingtalk.send_text(title, text)
        self._log("test_channel", "-", operator,
                  f"通道测试：{'成功' if result.ok else '失败'}（{result.mode}）")
        return result.model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list_records(self, status: Optional[str] = None) -> List[dict]:
        rows = self._records
        if status:
            rows = [r for r in rows if r.status.value == status]
        return [r.model_dump(mode="json") for r in rows]

    def get_record(self, record_id: str) -> ExportRecord:
        return self._get(record_id)

    def stats(self) -> dict:
        counts: Dict[str, int] = {}
        for r in self._records:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        return {
            "total": len(self._records),
            "pending": counts.get(ExportStatus.PENDING.value, 0),
            "pushed": counts.get(ExportStatus.PUSHED.value, 0),
            "push_failed": counts.get(ExportStatus.PUSH_FAILED.value, 0),
            "human_rejected": counts.get(ExportStatus.HUMAN_REJECTED.value, 0),
            "filtered": counts.get(ExportStatus.FILTERED.value, 0),
            "superseded": counts.get(ExportStatus.SUPERSEDED.value, 0),
        }

    def audit_log(self, limit: int = 50) -> List[dict]:
        return self._audit[:limit]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _make_record(self, item: dict, status: ExportStatus) -> ExportRecord:
        now = _now()
        rec = ExportRecord(
            record_id="E" + uuid.uuid4().hex[:8].upper(),
            task_id=item["task_id"],
            filename=item["filename"],
            contract_type=item["contract_type"],
            failure_kind=item["failure_kind"],
            reason=item["reason"],
            retries=item["retries"],
            source=item["source"],
            gate=item["gate"],
            status=status,
            created_at=now,
            updated_at=now,
        )
        # 关键：正文在此刻冻结。审核人预览的、实际发出的，是同一个字符串。
        rec.message = dingtalk.build_message(rec)
        return rec

    def _push(self, rec: ExportRecord, operator: str, action: str) -> ExportRecord:
        rec.attempts += 1
        result: PushResult = dingtalk.send_text(dingtalk.message_title(rec), rec.message)
        rec.push = result
        rec.status = ExportStatus.PUSHED if result.ok else ExportStatus.PUSH_FAILED
        rec.updated_at = _now()
        self._log(
            "push" if action == "approve" else "retry_push",
            rec.record_id, operator,
            f"{'推送成功' if result.ok else '推送失败'}（{result.mode}）：{result.detail}",
        )
        return rec

    def _get(self, record_id: str) -> ExportRecord:
        for r in self._records:
            if r.record_id == record_id:
                return r
        raise KeyError(f"外发记录不存在: {record_id}")

    def _log(self, action: str, target: str, operator: str, detail: str) -> None:
        self._audit.insert(0, {
            "action": action,
            "target": target,
            "operator": operator or "system",
            "detail": detail,
            "ts": _now(),
        })
        del self._audit[200:]

    def _trim(self) -> None:
        """超出上限时丢弃最旧的已终结记录。

        待审核（PENDING）的记录永远保留——它代表"等人处理的事"，
        被容量限制悄悄丢掉会导致漏发。
        """
        if len(self._records) <= cfg.MAX_EXPORT_RECORDS:
            return
        keep: List[ExportRecord] = []
        dropped = 0
        budget = cfg.MAX_EXPORT_RECORDS
        for r in self._records:
            if r.status == ExportStatus.PENDING or len(keep) < budget:
                keep.append(r)
            else:
                dropped += 1
        self._records = keep
        if dropped:
            self._log("trim", "-", "system", f"记录超出上限，清理 {dropped} 条已终结记录")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_STATUS_LABEL = {
    ExportStatus.FILTERED.value: "条件未通过",
    ExportStatus.PENDING.value: "待人工审核",
    ExportStatus.PUSHED.value: "已推送",
    ExportStatus.PUSH_FAILED.value: "推送失败",
    ExportStatus.HUMAN_REJECTED.value: "已驳回",
    ExportStatus.SUPERSEDED.value: "已失效",
}


def _status_label(status: ExportStatus) -> str:
    return _STATUS_LABEL.get(status.value, status.value)


# 模块级单例：队列全局唯一，审核动作与统计口径才不会分叉
export_queue = ExportQueue()
