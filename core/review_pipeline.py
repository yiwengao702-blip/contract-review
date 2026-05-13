# 文件位置: contract-review/core/review_pipeline.py
# 功能说明: 审查流水线编排器（含任务状态机与阻塞重试）。
#          串联：解析 -> 规则审查 -> 大模型语义审查 -> 条款推荐 -> 报告装配。
#          并通过 NodeBase 自动注册的节点扩展点，便于后续追加审查阶段。
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

from core.models import (
    ContractMeta,
    ClauseSegment,
    ReaffirmRecord,
    ReviewReport,
    ReviewTask,
    RiskFinding,
    RiskLevel,
    TaskStatus,
    WritebackStatus,
)
from config import cfg
from services.doc_parser import DocParser
from services.contract_guard import ContractGuard
from services.rule_engine import RuleEngine
from services.llm_review import LLMReview
from services.clause_recommend import ClauseRecommend
from services.approval_writeback import approval_writeback
from services.export_queue import export_queue


class ReviewPipeline:
    """合同审查流水线：编排文档校验、解析、审查、推荐、报告与回写。"""

    def __init__(self) -> None:
        self.doc_parser = DocParser()
        self.contract_guard = ContractGuard()
        self.rule_engine = RuleEngine()
        self.llm_review = LLMReview()
        self.clause_recommend = ClauseRecommend()
        # 使用模块单例：管理员在界面修改回写通道配置时，cfg 实时变更，
        # 后续回写立即按新配置执行（与钉钉通道一致）。
        self.writeback = approval_writeback
        # 任务存储（演示用内存字典；生产可替换为数据库）
        self._tasks: Dict[str, ReviewTask] = {}

    # ------------------------------------------------------------------ #
    # 任务生命周期
    # ------------------------------------------------------------------ #
    def create_task(self, filename: str, contract_type: str = "未分类",
                    source: str = "upload",
                    approval_instance_id: Optional[str] = None,
                    contract_no: Optional[str] = None,
                    applicant_dept: Optional[str] = None,
                    applicant: Optional[str] = None) -> ReviewTask:
        task_id = "T" + uuid.uuid4().hex[:10].upper()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        task = ReviewTask(
            task_id=task_id,
            filename=filename,
            contract_type=contract_type,
            source=source,
            status=TaskStatus.PENDING,
            writeback_status=WritebackStatus.NOT_WRITTEN,
            created_at=now,
            updated_at=now,
            approval_instance_id=approval_instance_id,
            contract_no=contract_no,
            applicant_dept=applicant_dept,
            applicant=applicant,
        )
        self._tasks[task_id] = task
        return task

    def run(self, task_id: str, file_path: str,
            gate_override: Optional[dict] = None) -> ReviewTask:
        """执行一次完整审查（校验 + 解析 + 审查 + 报告）。异常进入 blocked。

        Args:
            gate_override: 人工重申凭据（``ReaffirmRecord`` 的字典形式）。
                非空表示本次审查由人工放行，闸门判定**照常执行并保留**，
                只是不再据此拦截。这不是"信任人工"——恰恰相反：
                闸门的客观结论原样留在 doc_check 里供复盘，
                人工的判断单独记进 reaffirms，两者不互相覆盖。
        """
        task = self._get(task_id)
        # 重申留痕要记录"重申前是什么状态"（blocked / rejected）。
        # 必须在下面 _touch(PARSING) 之前取——晚一步拿到的就是 PARSING 了，
        # 留痕里就再也看不出这次放行到底是从哪种拦截态救回来的。
        prev_status = task.status.value
        # 重试计数：执行前任务已处于终态/失败态，说明这是一次重跑。
        # 该计数是外发条件之一（"重试过了还是失败"才更值得人介入），必须准确。
        if task.status in (TaskStatus.BLOCKED, TaskStatus.REJECTED, TaskStatus.COMPLETED):
            task.retries += 1
        # 重新审查意味着上一次的失败记录可能已过时（典型：把加密件换成正常文件重试成功）。
        # 立刻作废队列中该任务的待审核记录，避免审核人一点就把过期失败信息发进群。
        export_queue.revoke_pending(task_id, "任务已重新发起审查，原失败记录失效")
        self._touch(task, status=TaskStatus.PARSING)
        try:
            parsed = self.doc_parser.parse(file_path)
        except Exception as e:  # 解析受阻（加密/模糊/无文本）
            self._touch(task, status=TaskStatus.BLOCKED, error=str(e))
            self._notify_failure(task)
            return task

        full_text: str = parsed["full_text"]
        meta: ContractMeta = parsed["meta"]
        segments: List[ClauseSegment] = parsed["segments"]

        # 0) 合同文档闸门：非合同文本在此拦下，避免后续规则/语义审查大面积误报
        self._touch(task, status=TaskStatus.CHECKING)
        doc_check = self.contract_guard.check(full_text)

        # 解析结果先落库，便于前端在"非合同"场景也能展示正文与判定依据
        task.doc_check = doc_check
        task.meta = meta
        task.full_text = full_text
        task.segments = segments

        # 闸门本次是否判定为"需要拦下"。人工重申只能改变**处置**（放行/拦截），
        # 不能改变闸门的客观结论——所以判定结果先算清楚，再决定谁说了算。
        gate_blocks = (not doc_check.is_contract)
        # 只有"闸门确实拦了 + 有人重申"才算人工放行。
        # 若闸门本次本就放行（例如换了份正常文件重跑），那这次是正常流程，
        # 记一条"人工放行"反而是假留痕——报告里会凭空多出一次并不存在的干预。
        override_note: Optional[str] = None
        if gate_override and gate_blocks:
            operator = str(gate_override.get("operator") or "未知")
            reason = str(gate_override.get("reason") or "")
            task.reaffirms.append(ReaffirmRecord(
                operator=operator,
                reason=reason,
                at=time.strftime("%Y-%m-%d %H:%M:%S"),
                prev_status=prev_status,
                prev_verdict=doc_check.verdict,
                prev_score=doc_check.score,
                prev_reason=doc_check.reject_reason or "",
            ))
            # 报告里也要能看出"这次是人工放行的"——否则闸门形同虚设：
            # 谁都能悄悄放行一份本该被拦下的文件，而报告上看不出任何异常。
            override_note = (
                f"{operator} 于 {task.reaffirms[-1].at} 人工重申放行："
                f"{reason or '未填写理由'}（重申前判定：{doc_check.verdict}，"
                f"得分 {doc_check.score:.1f}）"
            )
        elif gate_blocks:
            # 未重申或重申缺理由：按原有拦截分支处理
            if doc_check.verdict == "undetermined":
                # 自述为合同（标题命中）但正文毫无合同要件——典型是拍照件识别失败、
                # 正文全是噪声。这跟"用户传错了文件"是两回事：路由到 blocked（可重试），
                # 而不是 rejected（会被理解成"这份文件不是合同，别传了"）。
                self._touch(task, status=TaskStatus.BLOCKED, error=doc_check.reject_reason)
                self._notify_failure(task)
                return task

            self._touch(task, status=TaskStatus.REJECTED, error=doc_check.reject_reason)
            self._notify_failure(task)
            return task

        # 放行时清掉上一次的拦截错误文本——留着会让"已放行"的任务在界面上仍显示受阻原因。
        self._touch(task, status=TaskStatus.REVIEWING, error=None)

        # 1) 规则引擎（传入业务类型：领域专用规则按 scope 过滤，启用开关在引擎内生效）
        #    拍照识别来源要额外告知引擎，用于给"字段缺失"类告警加上不确定性说明
        rule_findings = self.rule_engine.review(
            full_text, meta, segments, task.contract_type,
            source_is_ocr=(task.source == "scan"))
        # 2) 大模型语义审查（mock/真实）
        llm_findings = self.llm_review.review(full_text, meta, segments)
        findings: List[RiskFinding] = rule_findings + llm_findings
        # 3) 条款推荐填充
        self.clause_recommend.fill(findings)

        # 4) 装配报告
        overall = self._overall_level(findings)
        summary = self._build_summary(meta, findings, overall)
        report = ReviewReport(
            contract_name=task.filename,
            contract_type=task.contract_type,
            overall_level=overall,
            summary=summary,
            findings=findings,
            writeback_comment="",
            # 人工放行必须在报告本身留痕。只写在 task.reaffirms 里是不够的：
            # 报告是会被导出、打印、回写审批系统的对外文书，
            # 读报告的人看不到任务详情，却必须知道"闸门当初是拦过的"。
            gate_override=override_note,
            # 合同基本信息透传（上传时带入；留空则不展示对应行）
            contract_no=task.contract_no,
            applicant_dept=task.applicant_dept,
            applicant=task.applicant,
        )
        report.writeback_comment = self.writeback.build_comment(report, task.segments)

        # 5) 落库
        task.findings = findings
        task.report = report
        self._touch(task, status=TaskStatus.COMPLETED)
        return task

    def retry(self, task_id: str, file_path: str) -> ReviewTask:
        """阻塞任务重试（更换文件或参数后重新解析审查）。"""
        return self.run(task_id, file_path)

    def reaffirm(self, task_id: str, operator: str, reason: str,
                 file_path: Optional[str] = None) -> ReviewTask:
        """人工重申：把被闸门拦下的任务强制送入条款审查。

        为什么需要它：闸门是**启发式**判定（标题/主体/条款/要素的加权得分），
        实测会把真实合同误判——典型是低分辨率拍照件，OCR 失真后正文全无合同要件，
        被当成"非合同文档"拒掉。这种误判只能由人看出来，因此必须留一条人工通道。

        但"能放行"不等于"随便放行"，三条约束是刻意的：
          ① 只有 blocked / rejected 可重申——已完成的任务重申是换一种说法改结论；
          ② 理由必填——重申记录要能被复盘，无理由的放行等于没有留痕；
          ③ 闸门判定**原样保留**（doc_check 不改），人工结论另存 reaffirms。
             复盘时能同时看到"机器说它是非合同"和"某人放行了"，责任清楚。

        Args:
            file_path: 留空则自动复用该任务首次上传时的原文件。
                重申绝大多数场景是"照片糊了就这样吧，先审着"，
                再让用户传一次同一个文件既繁琐又容易传错版本。
        """
        task = self._get(task_id)
        if task.status not in (TaskStatus.BLOCKED, TaskStatus.REJECTED):
            raise ValueError(
                f"仅「解析受阻」或「非合同被拒」的任务可重申，"
                f"当前状态为「{task.status.value}」"
            )
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("重申必须填写理由（将记入留痕，供后续复盘）")

        path = file_path or self._locate_source(task_id)
        if not path:
            raise ValueError("未找到该任务的原始文件，请重新上传后再重申")

        return self.run(task_id, path,
                        gate_override={"operator": operator, "reason": reason})

    def _locate_source(self, task_id: str) -> Optional[str]:
        """按 task_id 在上传目录里找回原始文件（重申复用同一份，不重新上传）。"""
        upload_dir = Path(cfg.UPLOAD_DIR)
        if not upload_dir.exists():
            return None
        # 上传时以 {task_id}{后缀} 落盘，故按前缀匹配即可拿到唯一那份
        for p in sorted(upload_dir.glob(f"{task_id}.*")):
            if p.is_file() and p.suffix.lower() != ".json":
                return str(p)
        return None

    # ------------------------------------------------------------------ #
    # 回写
    # ------------------------------------------------------------------ #
    def write_back(self, task_id: str,
                   approval_instance_id: Optional[str] = None) -> dict:
        """把审查意见回写到审批系统，并把结果落到任务状态上。

        approval_instance_id: 可选，覆盖任务自带的审批实例号（极少数"换审批单"场景）。

        落状态这一步不是装饰：回写状态是任务的属性，不写回任务就会出现
        「接口返回成功、任务详情里仍是未回写」——刷新页面状态就丢，
        前端只能靠本地 DOM 记住它，换个任务又串味。
        """
        task = self._get(task_id)
        if not task.report:
            raise ValueError("任务尚未生成审查报告，无法回写")
        # 先标 writing：回写是外部调用，中途可能超时，界面要能显示"进行中"
        task.writeback_status = WritebackStatus.WRITING
        task.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        instance_id = approval_instance_id or task.approval_instance_id
        result = self.writeback.write_back(
            task_id, task.report, approval_instance_id=instance_id, segments=task.segments)
        # 回写服务返回的是字符串状态，转成枚举落库；无法识别时按失败处理（宁可保守）
        try:
            task.writeback_status = WritebackStatus(result.get("status"))
        except ValueError:
            task.writeback_status = WritebackStatus.FAILED
        task.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_task(self, task_id: str) -> ReviewTask:
        return self._get(task_id)

    def list_tasks(self) -> List[ReviewTask]:
        return list(self._tasks.values())

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _notify_failure(self, task: ReviewTask) -> None:
        """失败态产生后跑一次外发条件门禁，通过则进入人工审核队列。

        刻意不做成"条件过了就自动发钉钉"：条件只能筛掉明显不该发的，
        对外发送这个动作仍需要责任人在「失败外发审核」页点头确认。

        异常一律吞掉：审查结果才是主产物，通知没发出去最多是少提醒一个人，
        绝不该让审查任务本身变成失败态。
        """
        if not cfg.EXPORT_ON_RUN:
            return
        try:
            export_queue.scan([task])
        except Exception:  # pragma: no cover - 通知链路故障不应影响审查
            pass

    def _get(self, task_id: str) -> ReviewTask:
        if task_id not in self._tasks:
            raise KeyError(f"任务不存在: {task_id}")
        return self._tasks[task_id]

    def _touch(self, task: ReviewTask, status: TaskStatus | None = None,
               error: Optional[str] = None) -> None:
        if status is not None:
            task.status = status
        if error is not None:
            task.error = error
        task.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _overall_level(findings: List[RiskFinding]) -> RiskLevel:
        if any(f.level == RiskLevel.HIGH for f in findings):
            return RiskLevel.HIGH
        if any(f.level == RiskLevel.MEDIUM for f in findings):
            return RiskLevel.MEDIUM
        if findings:
            return RiskLevel.LOW
        return RiskLevel.LOW

    @staticmethod
    def _build_summary(meta: ContractMeta, findings: List[RiskFinding], overall: RiskLevel) -> str:
        high = sum(1 for f in findings if f.level == RiskLevel.HIGH)
        med = sum(1 for f in findings if f.level == RiskLevel.MEDIUM)
        low = sum(1 for f in findings if f.level == RiskLevel.LOW)
        base = (f"综合风险等级为【{overall.value}】；共识别风险 {len(findings)} 项"
                f"（高风险 {high}、中风险 {med}、低风险 {low}）。")
        if meta.party_a or meta.party_b:
            base += f" 签约主体：{meta.party_a or '?'} 与 {meta.party_b or '?'}。"
        if meta.amount:
            base += f" 合同金额：{meta.amount:,.0f} {meta.currency or '元'}。"
        if overall == RiskLevel.HIGH:
            base += " 建议法务重点复核高风险条款，确认无误前暂缓审批。"
        return base
