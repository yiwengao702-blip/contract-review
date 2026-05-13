# 文件位置: contract-review/services/approval_writeback.py
# 功能说明: 审批回写模块。
#          将结构化审查意见格式化为 Markdown 评论文本，并写回企业审批系统的评论区。
#          两类运行模式：
#            · 真实回写（配置了 APPROVAL_API_BASE）：POST 到审批系统评论接口，
#              支持 Bearer / 自定义请求头鉴权，失败自动重试，并记录 HTTP 状态与错误明细；
#            · 模拟回写（未配置）：内存模拟写回（记录回写日志），便于本地演示与联调，
#              填上地址即自动切真实回写，代码零改动。
#          回写状态机：not_written -> writing -> success / failed。
#          审计：每一条回写（成功/失败）都追加写入 data/writeback_log.jsonl，
#                进程重启不丢、可供管理员核查"意见到底有没有落到审批系统"。
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from config import cfg
from core.models import ClauseSegment, ReviewReport, WritebackStatus


def _mask_url(url: Optional[str]) -> str:
    """脱敏展示审批接口地址，避免把内网地址/路径完整暴露在界面上。"""
    if not url:
        return ""
    u = url.rstrip("/")
    if len(u) <= 12:
        return u[:3] + "***"
    return u[:12] + "***" + u[-4:]


class ApprovalWriteback:
    """审批回写服务（真实接口 / 模拟 双模式）。"""

    # ------------------------------------------------------------------ #
    # 评论文本构建
    # ------------------------------------------------------------------ #
    def build_comment(self, report: ReviewReport,
                      segments: Optional[List[ClauseSegment]] = None) -> str:
        """根据审查报告构建回写评论文本（Markdown）。

        segments 用于把风险点定位到「第 N 页 第 M 段」，让审批人能直接翻到原文。
        """
        seg_by_id = {s.segment_id: s for s in (segments or [])}
        lines: List[str] = []
        lines.append(f"## 合同智能审查意见：{report.contract_name}")
        lines.append(f"- 业务类型：{report.contract_type}")
        lines.append(f"- 综合风险等级：**{report.overall_level.value}**")

        # 合同基本信息（有则展示）
        info: List[str] = []
        if report.contract_no:
            info.append(f"合同编号 {report.contract_no}")
        if report.applicant_dept:
            info.append(f"送审部门 {report.applicant_dept}")
        if report.applicant:
            info.append(f"申请人 {report.applicant}")
        if info:
            lines.append("- 合同基本信息：" + " / ".join(info))

        lines.append("")
        lines.append(f"> {report.summary}")
        if report.gate_override:
            lines.append("")
            lines.append(f"> ⚠️ 本次审查为人工重申放行：{report.gate_override}")
        lines.append("")
        lines.append("### 风险清单")
        if report.findings:
            for i, f in enumerate(report.findings, 1):
                loc = ""
                seg = seg_by_id.get(f.segment_ref) if f.segment_ref else None
                if seg and (seg.page or seg.paragraph_index is not None):
                    parts = []
                    if seg.page:
                        parts.append(f"第{seg.page}页")
                    if seg.paragraph_index is not None:
                        parts.append(f"第{seg.paragraph_index}段")
                    loc = "（" + " ".join(parts) + "）"
                lines.append(f"{i}. **[{f.level.value}] {f.category}**{loc} —— {f.reason}")
                if f.legal_basis:
                    lines.append(f"   - 法律依据：{f.legal_basis}")
                if f.recommendation:
                    lines.append(f"   - 建议修改：{f.recommendation}")
        else:
            lines.append("未发现明确风险点。")
        lines.append("")
        lines.append("> 本意见由智能审查系统自动生成，供法务复核参考。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 回写执行
    # ------------------------------------------------------------------ #
    def write_back(self, task_id: str, report: ReviewReport,
                   approval_instance_id: Optional[str] = None,
                   segments: Optional[List[ClauseSegment]] = None) -> dict:
        """执行回写，返回包含状态与日志条目的字典。

        approval_instance_id: 审批单/实例 ID；缺省回退到 task_id（兼容无审批系统串联的演示）。
        """
        comment = self.build_comment(report, segments)
        instance_id = approval_instance_id or task_id
        log_entry = {
            "task_id": task_id,
            "instance_id": instance_id,
            "status": WritebackStatus.WRITING.value,
            "comment": comment,
            "ts": time.time(),
        }
        try:
            if cfg.APPROVAL_API_BASE:
                ok, detail, http_status = self._post_real(comment, instance_id, report)
                log_entry["status"] = WritebackStatus.SUCCESS.value if ok else WritebackStatus.FAILED.value
                log_entry["mode"] = "real"
                log_entry["detail"] = detail
                if http_status is not None:
                    log_entry["http_status"] = http_status
            else:
                # 内存模拟：sleep 模拟网络写回，保持整体行为一致
                time.sleep(0.3)
                ok = True
                log_entry["status"] = WritebackStatus.SUCCESS.value
                log_entry["mode"] = "simulated"
                log_entry["detail"] = "未配置 APPROVAL_API_BASE，按模拟通道记录（未实际发出）。"
        except Exception as exc:  # 兜底：任何意外都不应让调用方崩溃
            log_entry["status"] = WritebackStatus.FAILED.value
            log_entry["mode"] = "real" if cfg.APPROVAL_API_BASE else "simulated"
            log_entry["detail"] = f"{type(exc).__name__}: {exc}"

        self._append_disk_log(log_entry)
        return log_entry

    def _post_real(self, comment: str, instance_id: str,
                   report: ReviewReport) -> Tuple[bool, str, Optional[int]]:
        """真实 POST 到审批系统评论接口，含失败重试。返回 (成功?, 明细, HTTP 状态)。"""
        base = (cfg.APPROVAL_API_BASE or "").rstrip("/")
        path = (cfg.APPROVAL_COMMENT_PATH or "api/approval/comment").strip("/")
        url = f"{base}/{path}"
        headers = {"Content-Type": "application/json"}
        token = cfg.APPROVAL_API_TOKEN
        if token:
            if cfg.APPROVAL_AUTH_SCHEME == "bearer":
                headers["Authorization"] = f"Bearer {token}"
            elif cfg.APPROVAL_AUTH_SCHEME == "token-header":
                headers[cfg.APPROVAL_AUTH_HEADER] = token
            # scheme == "none" 时仅带 Content-Type

        body = {
            cfg.APPROVAL_INSTANCE_FIELD: instance_id,
            "task_id": None,  # 由调用方填充（此处占位，真正对象在下方覆盖）
            "comment": comment,
            "overall_level": report.overall_level.value,
            "summary": report.summary,
            "findings_count": len(report.findings),
        }
        # task_id 作为独立键保留，避免与 instance_id 混用
        body["task_id"] = instance_id if cfg.APPROVAL_INSTANCE_FIELD != "task_id" else instance_id

        last_err = "未知错误"
        for attempt in range(cfg.APPROVAL_MAX_RETRIES):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=cfg.APPROVAL_TIMEOUT)
                if resp.status_code < 300:
                    return True, f"回写成功（HTTP {resp.status_code}）", resp.status_code
                last_err = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            # 末次不再 sleep
            if attempt < cfg.APPROVAL_MAX_RETRIES - 1:
                time.sleep(0.5 * (attempt + 1))
        return False, last_err, None

    # ------------------------------------------------------------------ #
    # 状态与审计
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        """回写通道能力现状（管理员配置页消费）。"""
        base = (cfg.APPROVAL_API_BASE or "").strip()
        configured = bool(base)
        warnings: List[str] = []
        if not configured:
            warnings.append(
                "未配置 APPROVAL_API_BASE，当前为**模拟回写**：审查意见会生成并落库，"
                "但评论不会真的写到审批系统。填好接口地址即自动切真实回写。"
            )
        elif not cfg.APPROVAL_API_TOKEN and cfg.APPROVAL_AUTH_SCHEME != "none":
            warnings.append(
                "已配置接口地址但未配置令牌，且鉴权方案非 none：若审批系统要求鉴权，回写会被拒。"
            )
        return {
            "channel": "approval",
            "label": "企业审批系统评论区",
            "configured": configured,
            "mode": "real" if configured else "simulated",
            "mode_label": "真实接口" if configured else "模拟回写",
            "base_masked": _mask_url(base),
            "token_set": bool(cfg.APPROVAL_API_TOKEN),
            "comment_path": cfg.APPROVAL_COMMENT_PATH,
            "auth_scheme": cfg.APPROVAL_AUTH_SCHEME,
            "instance_field": cfg.APPROVAL_INSTANCE_FIELD,
            "warnings": warnings,
            "hint": (
                "在「合规规则与模板」页的审批对接卡片填入审批系统评论接口地址与令牌即可切真实回写"
                "（落盘到 data/settings.json，重启与重新发布都不会丢）；也可用同名环境变量注入，"
                "环境变量优先级更高。"
            ),
        }

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _log_path(self) -> Path:
        return cfg.BASE_DIR / "data" / "writeback_log.jsonl"

    def _append_disk_log(self, entry: dict) -> None:
        """追加一条回写审计记录（JSONL，一行一条，重启不丢）。"""
        try:
            p = self._log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # 审计写入失败绝不该影响主流程
            pass


# 模块级单例：回写通道全局唯一，配置（cfg）改动即时生效。
approval_writeback = ApprovalWriteback()
