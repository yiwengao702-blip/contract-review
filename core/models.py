# 文件位置: contract-review/core/models.py
# 功能说明: 审查系统核心数据模型（基于 pydantic）。
#          所有跨模块流转的结构化对象在此统一定义，前端/后端共用同一套字段约定。
from __future__ import annotations

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """审查任务状态机。"""
    PENDING = "pending"        # 待处理
    PARSING = "parsing"        # 解析中
    CHECKING = "checking"      # 文档类型校验中（判定是否为合同）
    REVIEWING = "reviewing"    # 审查中
    BLOCKED = "blocked"        # 解析受阻
    REJECTED = "rejected"      # 非合同文档，已拒绝进入条款审查
    COMPLETED = "completed"    # 审查完成


class WritebackStatus(str, Enum):
    """审批回写状态机。"""
    NOT_WRITTEN = "not_written"  # 未回写
    WRITING = "writing"          # 回写中
    SUCCESS = "success"          # 回写成功
    FAILED = "failed"            # 回写失败


class RiskLevel(str, Enum):
    """风险等级。"""
    HIGH = "high"      # 高风险
    MEDIUM = "medium"  # 中风险
    LOW = "low"       # 低风险


class ClauseCategory(str, Enum):
    """条款类别（用于层级切分与高亮锚定）。"""
    SUBJECT = "标的物"           # 标的物条款
    PAYMENT = "付款进度"         # 付款进度
    ACCEPTANCE = "验收标准"       # 验收标准
    LIABILITY = "违约责任"        # 违约责任
    CONFIDENTIALITY = "保密义务"  # 保密义务
    DATA_SECURITY = "数据安全"    # 数据安全
    PII = "个人信息保护"         # 个人信息保护
    JURISDICTION = "争议解决管辖" # 争议解决管辖
    COMPLIANCE = "廉洁合规"      # 廉洁合规（反商业贿赂）
    TERMINATION = "合同解除与终止" # 合同解除与终止
    OTHER = "其他"               # 其他


class DocumentCheck(BaseModel):
    """文档属性校验结果（判定输入文档是否为合同类文本）。

    各项命中信号全部保留，前端可据此展示"为什么判为/不判为合同"的依据，
    避免出现"系统说不出理由就拒审"的黑盒体验。
    """
    is_contract: bool = Field(description="是否判定为合同类文档")
    # 三态结论，比 is_contract 多一层信息量：
    #   contract      证据充分，是合同
    #   not_contract  证据指向非合同（确无合同特征）
    #   undetermined  证据矛盾：文档自述为合同，但正文读不出任何合同要件
    #                 —— 这不是"不是合同"，而是"没认出来"（多为拍摄/扫描质量差导致 OCR 失败）。
    # 区分后两者的意义：前者该跳过审查，后者该提示人工校对后重试；
    # 混为一谈会把识别失败误报成"您传的不是合同"，用户据此重传一份同样的照片只会再失败一次。
    verdict: str = Field(default="", description="三态结论：contract / not_contract / undetermined")
    score: float = Field(default=0.0, description="合同特征加权得分")
    threshold: float = Field(default=0.0, description="判定阈值")
    title_hit: Optional[str] = Field(default=None, description="命中的合同标题特征词")
    party_hit: Optional[str] = Field(default=None, description="命中的签约主体标识")
    clause_count: int = Field(default=0, description="识别到的条款结构数量")
    element_hits: List[str] = Field(default_factory=list, description="命中的合同要素关键词")
    sign_hit: bool = Field(default=False, description="是否含签署要素")
    negative_hits: List[str] = Field(default_factory=list, description="命中的非合同负向特征词")
    code_block_hits: int = Field(default=0, description="代码块/Markdown 强调痕迹数量")
    reject_reason: str = Field(default="", description="判为非合同时的可读原因")


class ContractMeta(BaseModel):
    """合同核心元数据（自动抽取）。"""
    party_a: Optional[str] = Field(default=None, description="甲方/签约主体名称")
    party_b: Optional[str] = Field(default=None, description="乙方/相对方主体名称")
    credit_code: Optional[str] = Field(default=None, description="统一社会信用代码")
    amount: Optional[float] = Field(default=None, description="合同金额（元）")
    currency: Optional[str] = Field(default=None, description="币种")
    duration: Optional[str] = Field(default=None, description="履行期限")
    effective_condition: Optional[str] = Field(default=None, description="生效条件")


class ClauseSegment(BaseModel):
    """一段被切分出的合同条款，携带可溯源定位信息。"""
    segment_id: str = Field(description="段落唯一 ID")
    category: ClauseCategory = Field(description="条款类别")
    text: str = Field(description="条款原文文本")
    start_offset: int = Field(description="在全文纯文本中的起始字符偏移")
    end_offset: int = Field(description="在全文纯文本中的结束字符偏移")
    page: Optional[int] = Field(default=None, description="页码（PDF/DOCX 物理页）")
    paragraph_index: Optional[int] = Field(default=None, description="段落序号")


class RiskFinding(BaseModel):
    """单条风险发现（审查结论的最小单元）。"""
    finding_id: str = Field(description="风险发现 ID")
    level: RiskLevel = Field(description="风险等级")
    category: str = Field(description="风险维度，如 违约责任/管辖地/保密义务")
    segment_ref: Optional[str] = Field(default=None, description="关联的条款 segment_id")
    quote: str = Field(default="", description="命中原文片段")
    start_offset: Optional[int] = Field(default=None, description="命中原文起始偏移（用于高亮）")
    end_offset: Optional[int] = Field(default=None, description="命中原文结束偏移")
    reason: str = Field(description="违规/风险成因说明")
    legal_basis: str = Field(default="", description="合规/法律依据")
    recommendation: str = Field(default="", description="AI 生成的推荐修改示范条款")
    recommend_key: Optional[str] = Field(default=None, description="关联示范条款库键，供条款推荐引擎查表")
    source: str = Field(default="rule", description="发现来源：rule（规则引擎）/ llm（大模型）")


class ReaffirmRecord(BaseModel):
    """人工重申留痕。

    闸门是自动判定，会出错（典型：拍照件识别失败导致正文全是噪声，
    被误判为"非合同"）。这条记录回答的是审计上最要紧的三个问题：
    **谁**在**什么时候**、以**什么理由**，把一份被拦下的文件放进了条款审查。
    没有它，报告里就会凭空多出一份"没有闸门记录"的审查结论。
    """
    operator: str = Field(description="重申操作人（法务/管理员）")
    reason: str = Field(description="重申理由（必填，留痕用）")
    at: str = Field(description="重申时间")
    prev_status: str = Field(default="", description="重申前的任务状态")
    prev_verdict: str = Field(default="", description="重申前的闸门判定（contract/not_contract/undetermined）")
    prev_score: float = Field(default=0.0, description="重申前的合同特征得分")
    prev_reason: str = Field(default="", description="重申前的拦截原因（原文留档，便于复盘闸门误判）")


class ReviewReport(BaseModel):
    """结构化审查报告。"""
    contract_name: str
    contract_type: str
    overall_level: RiskLevel
    summary: str = Field(description="综合审查结论摘要")
    findings: List[RiskFinding] = Field(default_factory=list)
    writeback_comment: str = Field(default="", description="回写至审批系统的评论文本（Markdown）")
    # 非空即表示：本次审查未经闸门自动放行，而是人工重申后执行的。
    # 报告里必须能看出来，否则"闸门形同虚设"——谁都能悄悄放行一份文件。
    gate_override: Optional[str] = Field(
        default=None, description="人工重申说明（非空表示绕过了合同闸门自动判定）")
    # 以下为报告抬头中的「合同基本信息」，可由上传时带入或留空（留空则不展示对应行）。
    contract_no: Optional[str] = Field(default=None, description="合同编号")
    applicant_dept: Optional[str] = Field(default=None, description="送审部门")
    applicant: Optional[str] = Field(default=None, description="申请人")


class ReviewTask(BaseModel):
    """一个合同审查任务的完整状态快照。"""
    task_id: str
    filename: str
    contract_type: str = Field(default="未分类")
    # 正文来源：upload=上传电子文档（文本层可信）/ scan=拍照识别（可能含识别错字）。
    # 这个字段不是装饰：元数据抽取依赖"甲方/乙方"这类标签文字，
    # 拍照识别一旦没认出（实测 Tesseract 曾把「甲方」认成 FA），
    # 规则引擎就会按"合同确实没写甲方"报出高风险——是假阳性。
    # 有来源标记才能在告警里把"可能没认出来"这层不确定性说清楚。
    source: str = Field(default="upload", description="正文来源：upload / scan")
    # 审批系统回写所需的实例标识：从审批系统待办拉取任务时带下来，
    # 回写评论区时据此定位要写入的审批单（缺省时回退到 task_id）。
    approval_instance_id: Optional[str] = Field(default=None, description="审批系统审批单/实例 ID")
    # 报告抬头所需的合同基本信息（上传时可选带入；留空则不展示对应行）。
    contract_no: Optional[str] = Field(default=None, description="合同编号")
    applicant_dept: Optional[str] = Field(default=None, description="送审部门")
    applicant: Optional[str] = Field(default=None, description="申请人")
    status: TaskStatus = TaskStatus.PENDING
    writeback_status: WritebackStatus = WritebackStatus.NOT_WRITTEN
    created_at: str
    updated_at: str
    error: Optional[str] = None
    doc_check: Optional[DocumentCheck] = Field(default=None, description="文档类型校验结果（是否合同）")
    meta: Optional[ContractMeta] = None
    full_text: str = Field(default="", description="合同全文纯文本（用于前端高亮渲染）")
    segments: List[ClauseSegment] = Field(default_factory=list)
    findings: List[RiskFinding] = Field(default_factory=list)
    report: Optional[ReviewReport] = None
    # 已重试次数：解析受阻的任务常常"换一份文件就好了"，
    # 所以"首次失败就告警"往往过于敏感——把重试次数作为可配条件，
    # 让"重试过了还是失败"这种更值得人介入的情形被单独筛出来。
    retries: int = Field(default=0, description="该任务已重试次数")
    # 人工重申留痕（可多次）。
    # 闸门是自动判定且会误判，典型是"拍照件识别失败 → 正文全是噪声 → 判成非合同"。
    # 重申允许人工放行，但必须留下"谁、何时、为什么"——否则报告里会凭空出现
    # 一份没有闸门依据的审查结论，审计上说不清。列表保留全部历史，不覆盖。
    reaffirms: List[ReaffirmRecord] = Field(
        default_factory=list, description="人工重申记录（按时间先后，可多次）")


# ---------------------------------------------------------------------- #
# 失败任务外发（钉钉）相关模型
# ---------------------------------------------------------------------- #
class FailureKind(str, Enum):
    """可外发的失败类型。

    取值与 TaskStatus 对齐，便于从任务状态直接映射：
    只有这两类才谈得上"审查这一步没走完"。
    """
    BLOCKED = "blocked"      # 解析受阻：文档解不开（加密 / 无文本层 / 格式异常）
    REJECTED = "rejected"    # 非合同被拒：闸门判定输入不是合同


class NotifyCondition(BaseModel):
    """外发触发条件（"加条件审核"里的那层"条件"）。

    每一项都对应 failure_gate 里一条可解释的判定：
    empty 语义统一为"不限"，避免出现"填 0 个就是不选任何一项"这种别扭约定。
    """
    kinds: List[str] = Field(default_factory=lambda: ["blocked"], description="纳入的失败类型")
    min_retries: int = Field(default=0, description="已重试次数下限，0 表示不限")
    extensions: List[str] = Field(default_factory=list, description="文件后缀白名单（空=不限）")
    sources: List[str] = Field(default_factory=list, description="正文来源（空=不限）：upload / scan")
    reason_keywords: List[str] = Field(default_factory=list, description="失败原因关键词（空=不限）")
    dedupe: bool = Field(default=True, description="同一任务已推送过则不再入队")


class GateItem(BaseModel):
    """单条条件判定结果（逐条可解释）。"""
    key: str = Field(description="条件标识")
    label: str = Field(description="条件中文名，如「失败类型」")
    expected: str = Field(description="条件要求（人类可读）")
    actual: str = Field(description="任务实际情况（人类可读）")
    passed: bool = Field(description="该项是否通过")
    skipped: bool = Field(default=False, description="该项未启用（空条件），不参与结论")


class GateCheck(BaseModel):
    """条件门禁的完整判定结论。

    保留逐条明细而不是只回一个布尔值：审核人看到的应当是
    "为什么这条失败任务值得推送到钉钉群"，而不是系统替他做的判断。
    """
    passed: bool = Field(description="是否全部通过（未启用的项不计入）")
    items: List[GateItem] = Field(default_factory=list, description="逐条判定明细")
    summary: str = Field(default="", description="一句话结论")


class ExportStatus(str, Enum):
    """外发记录状态机。

    PENDING 是"条件已通过、等人工确认"——外发钉钉属于对外动作，
    条件只能筛掉噪音，不能替代责任人点头，所以中间必须有这一态。
    """
    FILTERED = "filtered"              # 条件未通过，未入队（留痕用，不需要人工审核）
    PENDING = "pending"                # 待人工审核
    PUSHED = "pushed"                  # 已推送成功
    PUSH_FAILED = "push_failed"        # 推送失败（可重试）
    HUMAN_REJECTED = "human_rejected"  # 人工驳回（不外发）
    # 已失效：任务被重新审查且这次不再失败（例如把加密件换成正常文件后重试成功）。
    # 此时队列里那条"审查失败提醒"若还留着，审核人一点就会把过期信息发进群，
    # 变成假警报，所以新一次审查启动时立即作废该任务待审核的记录。
    SUPERSEDED = "superseded"


class PushResult(BaseModel):
    """一次推送的结果留痕。"""
    ok: bool = Field(description="是否成功")
    mode: str = Field(default="simulated", description="real=真实发出 / simulated=未配置通道的模拟")
    channel: str = Field(default="dingtalk", description="通道标识")
    http_status: Optional[int] = Field(default=None, description="HTTP 状态码")
    errorcode: Optional[int] = Field(default=None, description="钉钉业务错误码（0 为成功）")
    errmsg: str = Field(default="", description="钉钉返回的 errmsg 或异常信息")
    detail: str = Field(default="", description="给人看的说明")
    pushed_at: str = Field(default="", description="推送时间")


class ExportRecord(BaseModel):
    """一条待/已外发记录。"""
    record_id: str
    task_id: str
    filename: str
    contract_type: str = Field(default="未分类")
    failure_kind: str = Field(description="失败类型：blocked / rejected")
    reason: str = Field(default="", description="失败原因原文")
    retries: int = Field(default=0, description="入队时该任务的重试次数")
    source: str = Field(default="upload", description="正文来源")
    gate: GateCheck = Field(description="入队时的条件判定明细")
    status: ExportStatus = ExportStatus.PENDING
    message: str = Field(default="", description="将要推送出去的消息正文（供审核人预览）")
    created_at: str
    updated_at: str
    reviewed_by: Optional[str] = Field(default=None, description="审核人")
    review_note: str = Field(default="", description="审核意见 / 驳回理由")
    push: Optional[PushResult] = Field(default=None, description="最近一次推送结果")
    attempts: int = Field(default=0, description="已尝试推送次数")
