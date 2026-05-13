# 文件位置: contract-review/tests/demo_e2e.py
# 功能说明: 端到端演示脚本（不依赖前端）。
#          对示例合同跑「解析 → 审查 → 导出报告」全流程，并可选把报告
#          写回到一个本地真实 HTTP 回写桩（演示真实链路）。
#          运行后把 Markdown 审查报告写到 data/output/审查报告_*.md。
#
# 用法:
#   python tests/demo_e2e.py                         # 用软件采购合同跑全流程（模拟回写）
#   python tests/demo_e2e.py --sample 大额销售合同_违约责任不对等.pdf
#   python tests/demo_e2e.py --real-writeback        # 起本地回写桩并真实 POST
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _parse_args():
    here = Path(__file__).resolve().parent
    base = here.parent
    p = argparse.ArgumentParser(description="合同审查端到端演示")
    p.add_argument("--sample", default="软件采购合同_知识产权与付款风险.pdf",
                   help="示例合同文件名（位于 data/samples/）")
    p.add_argument("--type", default="采购", help="业务类型：采购/销售/服务/劳动")
    p.add_argument("--contract-no", default="HT-2026-DEMO-001", help="合同编号")
    p.add_argument("--dept", default="信息技术部", help="送审部门")
    p.add_argument("--applicant", default="李思琪", help="申请人")
    p.add_argument("--real-writeback", action="store_true",
                   help="启动本地回写桩并真实 POST（演示真实审批回写链路）")
    p.add_argument("--port", type=int, default=18099, help="回写桩端口（--real-writeback 时生效）")
    return p.parse_args(), base


# 模块级变量，供回写桩处理器引用
_received = []


class _StubHandler(BaseHTTPRequestHandler):
    """极简审批系统评论接口桩：收到 POST 就记下来并返回 200。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")}
        _received.append(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "received": len(_received)}).encode("utf-8"))

    def log_message(self, *a):  # 静默默认访问日志
        return


def _start_stub(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), _StubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # 等端口就绪
    for _ in range(40):
        try:
            import socket
            s = socket.socket(); s.settimeout(0.3)
            s.connect(("127.0.0.1", port)); s.close(); break
        except Exception:
            time.sleep(0.1)
    return srv


def main():
    args, base = _parse_args()
    sample_path = base / "data" / "samples" / args.sample
    if not sample_path.exists():
        print(f"[错误] 找不到示例合同：{sample_path}", file=sys.stderr)
        print("       可用示例：", ", ".join(sorted(
            p.name for p in (base / "data" / "samples").glob("*.pdf"))))
        sys.exit(2)

    # 若要求真实回写，必须在 import 应用模块前把环境变量设好
    # （config 为单例，构造时即读取环境变量，之后改 env 不生效）
    if args.real_writeback:
        os.environ["APPROVAL_API_BASE"] = f"http://127.0.0.1:{args.port}"
        # 内网桩免鉴权
        os.environ["APPROVAL_AUTH_SCHEME"] = "none"
        srv = _start_stub(args.port)
        print(f"[回写桩] 已启动本地审批评论接口：http://127.0.0.1:{args.port}/api/approval/comment")
    else:
        srv = None

    # —— 到这里再 import 应用模块，保证 env 已被读取 —— #
    # 把项目根目录加入 sys.path，使 `python tests/demo_e2e.py` 也能 import config / core
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    from config import cfg  # noqa: E402
    from core.review_pipeline import ReviewPipeline  # noqa: E402

    print(f"\n[1/4] 加载示例合同：{sample_path.name}")
    print(f"      业务类型={args.type}  合同编号={args.contract_no}  送审部门={args.dept}  申请人={args.applicant}")

    pipeline = ReviewPipeline()
    task = pipeline.create_task(
        filename=sample_path.name,
        contract_type=args.type,
        contract_no=args.contract_no,
        applicant_dept=args.dept,
        applicant=args.applicant,
    )

    print("[2/4] 解析 + 规则引擎 + 大模型审查 …")
    pipeline.run(task.task_id, str(sample_path))

    if task.status.value != "completed":
        print(f"[!] 审查未走完，当前状态：{task.status.value}（原因：{task.error or '-'}）")
        if srv:
            srv.shutdown()
        sys.exit(3)

    report = task.report
    print(f"      ✓ 状态={task.status.value}  综合风险等级={report.overall_level.value}  风险点={len(report.findings)}")

    print("[3/4] 导出审查报告（Markdown） …")
    out_dir = base / "data" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"审查报告_{task.task_id}.md"
    md = _render_report(report, task)
    md_path.write_text(md, encoding="utf-8")
    print(f"      ✓ 报告已写入：{md_path}")

    # 控制台摘要
    print("\n—— 审查结论摘要 ——")
    print(report.summary)
    for i, f in enumerate(report.findings, 1):
        print(f"  {i}. [{f.level.value}] {f.category}：{f.reason}")
        if f.legal_basis:
            print(f"       依据：{f.legal_basis}")
        if f.recommendation:
            print(f"       建议：{f.recommendation}")

    print("[4/4] 回写审批系统评论区 …")
    res = pipeline.write_back(task.task_id, approval_instance_id="DEMO_INSTANCE_001")
    print(f"      模式={res.get('mode')}  状态={res.get('status')}  明细={res.get('detail')}")
    if args.real_writeback and _received:
        print(f"      ✓ 回写桩已收到 {len(_received)} 条评论，实例号={_received[0].get(cfg.APPROVAL_INSTANCE_FIELD)}")

    print("\n[完成] 端到端演示跑通。可打开上面的报告文件查看完整审查意见。")
    if srv:
        srv.shutdown()


def _render_report(report, task) -> str:
    """把审查报告渲染成包含四个标准章节的 Markdown。"""
    lines = []
    lines.append(f"# 合同审查报告：{report.contract_name}\n")
    lines.append("## 一、合同基本信息\n")
    # 相对方/金额/周期来自解析出的元数据（task.meta），而非报告抬头字段
    meta = task.meta
    counterparty = (meta.party_b or meta.party_a) if meta else None
    amount = meta.amount if meta else None
    period = meta.duration if meta else None
    lines.append(f"- 合同编号：{report.contract_no or '（未提供）'}")
    lines.append(f"- 业务类型：{report.contract_type}")
    lines.append(f"- 送审部门：{report.applicant_dept or '（未提供）'}")
    lines.append(f"- 申请人：{report.applicant or '（未提供）'}")
    lines.append(f"- 相对方主体：{counterparty or '（见原文）'}")
    lines.append(f"- 合同金额：{amount if amount is not None else '（见原文）'}")
    lines.append(f"- 履约周期：{period or '（见原文）'}\n")

    lines.append("## 二、综合审查结论\n")
    lines.append(f"- 总风险等级：**{report.overall_level.value}**")
    verdict = {"high": "建议拒绝", "medium": "限期整改", "low": "通过"}.get(
        report.overall_level.value, "通过")
    lines.append(f"- 审查结论：**{verdict}**")
    lines.append(f"- 核心风险摘要：{report.summary}\n")

    lines.append("## 三、风险清单明细\n")
    if report.findings:
        for i, f in enumerate(report.findings, 1):
            lines.append(f"### 风险 {i}：{f.category}（{f.level.value}）")
            lines.append(f"- 风险成因：{f.reason}")
            if f.legal_basis:
                lines.append(f"- 法律合规依据：{f.legal_basis}")
            if f.quote:
                lines.append(f"- 原文片段：{f.quote}")
            if f.recommendation:
                lines.append(f"- 推荐修改示范条款：{f.recommendation}")
            lines.append("")
    else:
        lines.append("未发现明确风险点。\n")

    lines.append("## 四、审批回写评论文本（Markdown）\n")
    lines.append("```markdown")
    lines.append(report.writeback_comment)
    lines.append("```\n")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
