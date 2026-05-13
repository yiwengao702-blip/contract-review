# 文件位置: contract-review/tests/integration_check.py
# 功能说明: 真实接入（LLM 直连 + 审批系统回写）的离线集成自检。
#          关键验证点：
#            ① LLM=mock            -> 产出离线语义补充发现；
#            ② LLM=openai 但无凭据 -> 自动降级 mock，不报错；
#            ③ LLM=openai 且接口不可达 -> 调用失败仍降级 mock，审查不中断；
#            ④ 回写未配置            -> 模拟回写 SUCCESS，并落盘审计日志；
#            ⑤ 回写配置为本地真服务   -> 真实 POST 成功，payload 含 instance_id/comment；
#            ⑥ 回写地址不可达         -> 判定 FAILED，错误明细与审计日志齐全；
#            ⑦ 评论文本含法律依据与合同基本信息；
#            ⑧ 管理员通道 status() 正确反映 real / simulated。
#          直接运行：python tests/integration_check.py
from __future__ import annotations

import json
import sys
import threading
import http.server
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import pytest  # 可选依赖：本文件既可被 pytest 调起，也可直接 python 运行
except ImportError:  # 未安装 pytest 时不影响独立运行
    pytest = None

from config import cfg  # noqa: E402
from core.models import (  # noqa: E402
    ClauseCategory,
    ClauseSegment,
    ContractMeta,
    ReviewReport,
    RiskFinding,
    RiskLevel,
)
from services.llm_review import LLMReview  # noqa: E402
from services.approval_writeback import approval_writeback  # noqa: E402


# ---------------------------------------------------------------------- #
# 辅助：本地回写服务（用于验证真实 HTTP 回写路径）
# ---------------------------------------------------------------------- #
class _Handler(http.server.BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            _Handler.received = json.loads(raw.decode("utf-8"))
        except Exception:
            _Handler.received = {}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # 静默
        pass


def _start_local_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _sample_segments():
    return [
        ClauseSegment(segment_id="S1", category=ClauseCategory.LIABILITY,
                      text="任何一方均不承担连带责任。", start_offset=0, end_offset=12),
        ClauseSegment(segment_id="S2", category=ClauseCategory.PAYMENT,
                      text="合同签订后支付全部款项。", start_offset=20, end_offset=34, page=2, paragraph_index=3),
    ]


def _sample_report():
    findings = [
        RiskFinding(finding_id="R1", level=RiskLevel.HIGH, category="违约责任",
                    segment_ref="S2", quote="合同签订后支付全部款项",
                    start_offset=20, end_offset=34,
                    reason="违约责任严重不对等。",
                    legal_basis="《民法典》第五百七十七条",
                    recommendation="建议修改为双方对等承担违约责任。",
                    source="rule"),
    ]
    meta = ContractMeta(party_a="甲方", party_b="乙方", amount=1000000.0, currency="元")
    return ReviewReport(
        contract_name="测试合同.pdf", contract_type="采购", overall_level=RiskLevel.HIGH,
        summary="综合风险等级为【high】；共识别风险 1 项。", findings=findings,
        contract_no="HT-2026-001", applicant_dept="采购部", applicant="李思齐",
    )


# ---------------------------------------------------------------------- #
# 测试用例
# ---------------------------------------------------------------------- #
def test_llm_mock_returns_findings():
    llm = LLMReview(provider="mock")
    segs = _sample_segments()
    meta = ContractMeta()
    # 构造一段含"尽快"的条款，mock 应能识别模糊表述
    segs.append(ClauseSegment(segment_id="S3", category=ClauseCategory.PAYMENT,
                              text="请尽快完成付款。", start_offset=40, end_offset=50))
    out = llm.review("请尽快完成付款。", meta, segs)
    assert any(f.source == "llm" for f in out), "mock 模式应产出 LLM 来源发现"


def test_llm_real_without_credential_falls_back_to_mock():
    cfg.LLM_PROVIDER = "openai"
    cfg.LLM_API_KEY = None
    cfg.LLM_BASE_URL = None
    llm = LLMReview()
    out = llm.review("正文", ContractMeta(), _sample_segments())
    # 不抛异常，且能返回（降级为 mock 结果）
    assert isinstance(out, list)


def test_llm_real_unreachable_falls_back_to_mock():
    cfg.LLM_PROVIDER = "openai"
    cfg.LLM_API_KEY = "fake-key"
    cfg.LLM_BASE_URL = "http://127.0.0.1:1/v1"  # 端口 1 必然连接被拒
    cfg.LLM_TIMEOUT = 1.0
    llm = LLMReview()
    out = llm.review("正文", ContractMeta(), _sample_segments())
    assert isinstance(out, list), "真实 LLM 不可达时应降级而非崩溃"


def test_writeback_simulated_logs_to_disk():
    cfg.APPROVAL_API_BASE = None  # 强制模拟
    report = _sample_report()
    res = approval_writeback.write_back("T_SIM", report, segments=_sample_segments())
    assert res["mode"] == "simulated"
    assert res["status"] == "success"
    log_file = ROOT / "data" / "writeback_log.jsonl"
    assert log_file.exists(), "回写必须落盘审计日志"
    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any("T_SIM" in ln for ln in lines), "审计日志应含本次回写记录"


def test_writeback_real_success_posts_payload():
    srv, port = _start_local_server()
    cfg.APPROVAL_API_BASE = f"http://127.0.0.1:{port}"
    cfg.APPROVAL_COMMENT_PATH = "comment"
    cfg.APPROVAL_API_TOKEN = "secret-token"
    cfg.APPROVAL_AUTH_SCHEME = "bearer"
    cfg.APPROVAL_INSTANCE_FIELD = "instance_id"
    report = _sample_report()
    res = approval_writeback.write_back(
        "T_REAL", report, approval_instance_id="APPROVAL_999", segments=_sample_segments())
    assert res["mode"] == "real"
    assert res["status"] == "success"
    body = _Handler.received
    assert body.get("instance_id") == "APPROVAL_999", "回写 payload 必须携带审批实例号"
    assert "测试合同" in (body.get("comment") or ""), "回写 payload 必须携带评论文本"
    assert body.get("overall_level") == "high"
    srv.shutdown()


def test_writeback_real_failure_marks_failed():
    cfg.APPROVAL_API_BASE = "http://127.0.0.1:1"  # 不可达
    cfg.APPROVAL_COMMENT_PATH = "comment"
    cfg.APPROVAL_API_TOKEN = None
    cfg.APPROVAL_AUTH_SCHEME = "none"
    cfg.APPROVAL_MAX_RETRIES = 2
    cfg.APPROVAL_TIMEOUT = 1.0
    report = _sample_report()
    res = approval_writeback.write_back("T_FAIL", report, segments=_sample_segments())
    assert res["status"] == "failed", "不可达应判定为回写失败"
    assert res.get("detail"), "失败应记录错误明细"


def test_comment_includes_legal_basis_and_info():
    report = _sample_report()
    comment = approval_writeback.build_comment(report, _sample_segments())
    assert "《民法典》第五百七十七条" in comment, "评论文本应含法律依据"
    assert "HT-2026-001" in comment, "评论文本应含合同编号"
    assert "第2页" in comment and "第3段" in comment, "评论文本应含条款页码定位"


def test_approval_status_reflects_mode():
    cfg.APPROVAL_API_BASE = None
    st = approval_writeback.status()
    assert st["mode"] == "simulated"
    cfg.APPROVAL_API_BASE = "https://approval.example.com"
    st2 = approval_writeback.status()
    assert st2["mode"] == "real"
    cfg.APPROVAL_API_BASE = None  # 复原


# ---------------------------------------------------------------------- #
# 直接运行入口（不依赖 pytest）
# ---------------------------------------------------------------------- #
def _run_standalone():
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = []
    for fn in funcs:
        try:
            fn()
            print(f"  [通过] {fn.__name__}")
        except Exception as exc:
            failures.append((fn.__name__, repr(exc)))
            print(f"  [失败] {fn.__name__}: {exc}")
    print("=" * 60)
    if failures:
        print(f"集成自检：失败（{len(failures)}/{len(funcs)}）")
        for name, err in failures:
            print(f"  - {name}: {err}")
        return 1
    print(f"集成自检：全部通过（{len(funcs)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
