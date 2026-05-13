# 临时验证脚本：失败任务外发钉钉（条件门禁 + 人工审核）后端链路
import pathlib
import sys

sys.path.insert(0, ".")

from core.models import ExportStatus, TaskStatus
from core.review_pipeline import ReviewPipeline
from services.export_queue import export_queue
from services.failure_gate import condition_labels, normalize_condition
from config import cfg

pipe = ReviewPipeline()
UP = cfg.UPLOAD_DIR


def make_blocked(tag: str, filename: str = "加密合同.pdf"):
    """造一个真实解析受阻的任务。"""
    p = UP / f"_tmp_{tag}.pdf"
    p.write_bytes(b"\x00\x01broken-not-a-real-pdf")
    t = pipe.create_task(filename=filename, contract_type="采购")
    pipe.run(t.task_id, str(p))
    return t


def make_rejected(tag: str):
    """造一个非合同被拒的任务（用真夹具，确保是闸门判的而非造出来的）。"""
    src = pathlib.Path("tests/fixtures/非合同样例_Python技术笔记.docx")
    dst = UP / f"_tmp_{tag}_note.docx"
    dst.write_bytes(src.read_bytes())
    t = pipe.create_task(filename="Python技术笔记.docx", contract_type="未分类")
    pipe.run(t.task_id, str(dst))
    return t


print("=" * 74)
print("1. 默认条件下：解析受阻入队、非合同被拒只留痕")
print("=" * 74)
print("  当前条件:", "；".join(condition_labels(normalize_condition(cfg.EXPORT_CONDITION))))
b1 = make_blocked("b1")
print(f"  造任务 {b1.task_id} 状态={b1.status.value} 重试={b1.retries} 原因={b1.error[:40]!r}")
r1 = export_queue.scan([b1])
print(f"  扫描结果: 入队 {r1['queued']} / 拦下 {r1['filtered']} / 队列已有 {r1['already_in_queue']}")

b2 = make_rejected("r1")
print(f"\n  造任务 {b2.task_id} 状态={b2.status.value} 原因={b2.error[:36]!r}")
# 重新扫描全部（这次也扫到了 b1，应识别为"队列已有"而不是新增）
r2 = export_queue.scan([b1, b2])
print(f"  扫描结果: 入队 {r2['queued']} / 拦下 {r2['filtered']} / 队列已有 {r2['already_in_queue']} / 非失败 {r2['non_failure']}")

recs = export_queue.list_records()
print(f"\n  记录总数 {len(recs)}：")
for r in recs:
    print(f"    [{r['record_id']}] {r['status']:9} {r['failure_kind']:8} {r['filename'][:20]:22} {r['gate']['summary'][:40]}")

print()
print("=" * 74)
print("2. 待审核记录的消息正文与判定明细")
print("=" * 74)
pend = [r for r in recs if r["status"] == "pending"]
print(f"  待审核 {len(pend)} 条")
if pend:
    p0 = pend[0]
    print("  条件判定明细：")
    for it in p0["gate"]["items"]:
        flag = "—" if it["skipped"] else ("✓" if it["passed"] else "✗")
        print(f"    {flag} {it['label']:14} 要求 {it['expected']:22} 实际 {it['actual']}")
    print("\n  将推送出去的消息正文：")
    for ln in p0["message"].splitlines():
        print("   |", ln)

print()
print("=" * 74)
print("3. 人工审核：驳回必须写理由；通过则推送")
print("=" * 74)
rid = pend[0]["record_id"] if pend else None
if rid:
    try:
        export_queue.reject(rid, "苏析", "   ")
        print("  ✗ 空理由竟然通过了（应该有校验）")
    except ValueError as e:
        print("  ✓ 空理由被拒:", e)

    rec = export_queue.approve(rid, "周鸣远", "确属加密件，通知业务方换文件")
    print(f"  ✓ 审核通过 -> 状态={rec.status.value} 推送模式={rec.push.mode} 成功={rec.push.ok}")
    print(f"    说明: {rec.push.detail}")
    print(f"    审核人={rec.reviewed_by} 尝试次数={rec.attempts}")

    try:
        export_queue.approve(rid, "周鸣远")
        print("  ✗ 已推送记录被重复审核（应该报错）")
    except ValueError as e:
        print("  ✓ 重复审核被拒:", e)

print()
print("=" * 74)
print("4. 条件调整：要求「重试 ≥ 2 次」后，首次失败不再打扰人")
print("=" * 74)
export_queue.update_condition({"kinds": ["blocked", "rejected"], "min_retries": 2})
print("  新条件:", "；".join(condition_labels(normalize_condition(cfg.EXPORT_CONDITION))))
b3 = make_blocked("b3", "扫描件.pdf")
print(f"  造任务 {b3.task_id} 重试={b3.retries} 次")
r3 = export_queue.scan([b3])
print(f"  扫描结果: 入队 {r3['queued']} / 拦下 {r3['filtered']}")
new = [r for r in export_queue.list_records() if r["task_id"] == b3.task_id][0]
print(f"  该任务记录状态: {new['status']} —— {new['gate']['summary']}")

print("\n  重试两次后再扫：")
for i in range(2):
    pipe.run(b3.task_id, str(UP / "_tmp_b3.pdf"))
print(f"  重试计数={b3.retries}（注意：重试会先作废旧记录）")
r4 = export_queue.scan([b3])
print(f"  扫描结果: 入队 {r4['queued']} / 拦下 {r4['filtered']}")
mine = [r for r in export_queue.list_records() if r["task_id"] == b3.task_id]
print(f"  该任务记录 {len(mine)} 条：")
for r in mine:
    print(f"    [{r['record_id']}] {r['status']:10} 重试={r['retries']} {r['gate']['summary'][:52]}")

print()
print("=" * 74)
print("5. 作废：任务重试成功后，队列里的失败提醒自动失效")
print("=" * 74)
pend_before = [r for r in export_queue.list_records() if r["status"] == "pending" and r["task_id"] == b3.task_id]
print(f"  作废前该任务待审核 {len(pend_before)} 条")
n = export_queue.revoke_pending(b3.task_id, "任务已重新发起审查，原失败记录失效")
print(f"  作废 {n} 条")
after = [r for r in export_queue.list_records() if r["task_id"] == b3.task_id and r["status"] == "pending"]
print(f"  作废后待审核 {len(after)} 条（期望 0）")

print()
print("=" * 74)
print("6. 条件规范化（前端可能传字符串/数组/脏值）")
print("=" * 74)
for raw in [
    {"kinds": "blocked,rejected", "min_retries": "3", "extensions": "pdf, .DOCX"},
    {"kinds": ["BLOCKED", "打错了"], "reason_keywords": "加密、扫描件 OCR"},
    {"kinds": [], "min_retries": -5, "dedupe": None},
]:
    c = normalize_condition(raw)
    print(f"  输入 {raw}")
    print(f"   -> {condition_labels(c)}")

print()
print("=" * 74)
print("7. 统计与审计日志")
print("=" * 74)
print("  统计:", export_queue.stats())
print("  审计日志（最近 6 条）:")
for a in export_queue.audit_log(6):
    print(f"    {a['ts']} [{a['action']:16}] {a['operator']:8} {a['detail'][:56]}")

# 清理临时文件
for f in UP.glob("_tmp_*"):
    f.unlink()
print("\n  临时文件已清理")
