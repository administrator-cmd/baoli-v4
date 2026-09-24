from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiResponse(BaseModel):
    request_id: str


class ExternalServiceConfiguration(BaseModel):
    status: Literal["UNCONFIGURED", "CONFIGURATION_INCOMPLETE", "CONFIGURED_UNVERIFIED"]
    missing: list[str] = Field(default_factory=list)


class SystemStatusResponse(ApiResponse):
    service: str
    database: Literal["CONNECTED", "FAILED"]
    external_services: dict[str, ExternalServiceConfiguration]


class UploadKindPolicy(BaseModel):
    """一种上传入口的体量上限与允许扩展名。"""

    max_bytes: int
    allowed_extensions: list[str]


class UploadPolicyResponse(ApiResponse):
    """上传策略的唯一真相源。

    前端只读该接口，不得自行复制大小常量或扩展名清单；服务端 storage_service.save_upload
    仍是最终校验者，超限仍显式抛 FILE_TOO_LARGE。
    """

    rulebook: UploadKindPolicy
    document: UploadKindPolicy
    # 明确声明该上限只覆盖本系统上传入口，不构成对外部服务与部署入口的承诺。
    scope_note: str


class MaterialTypeItem(BaseModel):
    """8018 支持目录中的一项材料识别类型。"""

    id: int
    name: str


class MaterialTypeCatalogResponse(ApiResponse):
    """8018 材料识别类型目录。原样转发，不内置默认清单、不做名称映射。"""

    items: list[MaterialTypeItem]


class SheetBindingItem(BaseModel):
    sheet_key: str
    role: str | None = None
    # 绑定的**节点实例码**（来自当前场景的节点配置）。一张 Sheet 只能绑定一个节点，
    # 一个节点可以绑定多张 Sheet。未绑定或已排除的 Sheet 不进入执行范围。
    node_code: str | None = Field(default=None, max_length=80)
    header_row: int | None = Field(default=None, ge=1)
    excluded: bool = False


class SheetBindingsRequest(BaseModel):
    bindings: list[SheetBindingItem]


class DecisionItem(BaseModel):
    target_type: Literal["ISSUE", "MATERIAL", "FIELD", "RULE", "REPORT_SECTION", "SHEET"]
    target_ref: str
    action: Literal["ACCEPT", "EXCLUDE", "RESOLVE"]
    value: dict[str, Any] = Field(default_factory=dict)


class ImportDecisionsRequest(BaseModel):
    decisions: list[DecisionItem]
    # 提交者看到的草稿修订号。与作业当前修订不一致时明确冲突，不接受过期提交。
    expected_revision: int | None = Field(default=None, ge=0)


class ApplyImportDraftRequest(BaseModel):
    expected_job_revision: int = Field(ge=0)
    expected_scene_revision: int = Field(ge=0)


class PublishRequest(BaseModel):
    """历史发布结果的重复调用请求。

    导入页不再有发布前检查，也没有「新建模板 / 更新模板」的目标意图：写入场景草稿之后，
    可执行的场景版本只由管理员在场景页显式发布。本请求只用于**只读地**重放已经产生过
    场景版本的历史作业，因此没有 `template_mode` 一类会改写目标的字段。
    """

    scenario_code: str | None = Field(default=None, min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    scenario_name: str = Field(min_length=2, max_length=255)
    # 已逐条确认「这条检查本次确实无法执行」的规则键。
    #
    # 能力缺口默认阻断发布：规则引用的能力当前没有判定实现时，发布检查不通过。
    # 要让发布继续，必须把缺口规则的 rule_key **逐条**列在这里——这表示使用者已经看到
    # 并接受「该检查在运行时会明确报不可判定、不会给出结论」。
    # 它不是「忽略全部缺口」的开关：未列出的缺口仍然阻断；列出的缺口会随已发布版本
    # 冻结，报告与任务范围里继续可见，历史任务不受后续能力实现的影响。
    acknowledged_capability_gaps: list[str] = Field(default_factory=list, max_length=500)


class OperationAcceptedResponse(ApiResponse):
    """长操作的受理状态。它只表示操作已被受理，绝不表示业务已经成功。"""

    operation_no: str
    object_no: str
    object_type: str
    operation_type: str
    target_node: str | None = None
    input_version: str | None = None
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"]
    message: str
    reused: bool = False
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    # 命令当前停在哪个阶段，用于区分“已保存未执行 / 外部已受理 / 结果未知 /
    # 业务已提交但图未推进 / 等待 interrupt / 检查点暂不可达 / 检查点确实缺失”。
    phase: str | None = None
    external_job_ref: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CapabilityItem(BaseModel):
    """能力目录中的一项能力。

    页面据此渲染检查方式下拉、规则输入形态与参数提示，不再本地维护一份名单——
    本地名单与后端漂移过一次（后端 12 个算子，页面只有 7 个），
    结果是页面上打开一条已导入的规则，下拉里找不到它当前的检查方式。
    """

    capability_id: str
    label: str
    kind: str
    parent: str | None = None
    nodes: list[str] = Field(default_factory=list)
    input_kind: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    output_keys: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    supports_evidence: bool = False
    supports_comparison: bool = False
    implemented: bool = False
    unimplemented_reason: str | None = None
    execution: str = ""
    evidence: str | None = None
    judge: str | None = None
    extension_namespace: str | None = None
    version: str = ""
    note: str | None = None


class ToolCatalogItem(BaseModel):
    """工具目录中的一项工具。

    与 `CapabilityItem` 的分工：能力项回答「这条规则由哪项能力判定」，
    工具项回答「模型可以调用什么、参数长什么样、当前能不能真跑」。
    两者都由 `domain/tool_catalog.py` 从能力目录派生，不各自维护一份名单。
    """

    tool_id: str
    version: str
    label: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    handler_ref: dict[str, str] | None = None
    allowed_nodes: list[str] = Field(default_factory=list)
    allowed_scenes: list[str] | None = None
    required_evidence: list[str] = Field(default_factory=list)
    side_effect: bool = False
    idempotent: bool = True
    supports_query: bool = False
    # 四个口径分开返回，页面据此区分「已注册 / 节点级可调用 / Agent 可选择 / 已实现」。
    # 只有一个笼统的「可调用」会让「注册了但模型调不到」显示成模型能用。
    registered: bool = True
    implemented: bool = False
    agent_selectable: bool = False
    node_level_only: bool = False
    agent_unselectable_reason: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    unimplemented_reason: str | None = None
    input_count: str | None = None
    runtime_keys: list[str] = Field(default_factory=list)


class CapabilityCatalogResponse(ApiResponse):
    """审核能力目录。

    这是后端能力的单一事实来源：前端从这里取算子选项、节点约束与参数口径，
    版本随能力语义一起变化。目录里 implemented=false 的能力可以配置，
    但发布前必须逐条确认，运行时明确返回不可判定——不会伪装成通过。

    `tools` 是同一份目录的工具视图：模型可调用的工具及其参数 Schema。
    只有 `implemented=true` 的工具才会出现在模型请求的 `tools` 里，
    不可调用的工具仍如实列出并写明原因，页面据此不把它们显示成可执行选项。
    """

    capability_version: str
    rule_statuses: list[str] = Field(default_factory=list)
    items: list[CapabilityItem] = Field(default_factory=list)
    tool_catalog_version: str = ""
    # 工具目录的内容指纹：随已发布配置冻结，用于对账「模型当时看到的是哪一版工具」。
    tool_catalog_fingerprint: str = ""
    tools: list[ToolCatalogItem] = Field(default_factory=list)


class RuleCapabilityStatusResponse(BaseModel):
    """一个规则结果状态的标准解释。

    页面显示状态时用它区分「已判定 / 尚未判定 / 不适用 / 未识别」，
    而不是把不认识的码显示成「未开始」。
    """

    status: str | None = None
    known: bool = False
    label: str | None = None
    category: str = "UNKNOWN"
    category_label: str | None = None
    note: str | None = None


class RuleStatusCatalogResponse(ApiResponse):
    """全部审核结论状态的标准口径与分档。"""

    statuses: list[RuleCapabilityStatusResponse] = Field(default_factory=list)
    categories: list[dict[str, str]] = Field(default_factory=list)


class ImportJobResponse(ApiResponse):
    job_no: str
    source_filename: str
    status: str
    # 统一模板协议的版本标识：系统只有一份协议，这里记录按哪一版解释这份文件。
    profile_version: str
    # 作业所属场景 + 统一模板协议摘要（Sheet 数量、名称与可绑定的节点类型）。
    # 页面据此显示真实的表数量与名称，不写死任何场景的表清单。
    profile: dict[str, Any] = Field(default_factory=dict)
    sheets: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    proposals: dict[str, Any] | None = None
    manifest_draft: dict[str, Any] | None = None
    # 每条来源业务记录的处置：已形成候选 / 明确排除（含原因）/ 待处理问题。
    coverage: dict[str, Any] | None = None
    # 草稿修订号：绑定、排除、裁决、候选原地修正都会递增；页面提交时带上它。
    draft_revision: int = 0
    failure: dict[str, str] | None = None
    # 该作业上最近一次后台操作的受理状态；有活跃操作时页面据此继续查询。
    latest_operation: dict[str, Any] | None = None
    created_at: datetime


class ImportJobSummaryResponse(ApiResponse):
    job_no: str
    source_filename: str
    status: str
    created_at: datetime


class ScenarioSummaryResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    latest_version_no: int
    status: str
    published_at: datetime


class ScenarioVersionResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    manifest: dict[str, Any]
    published_at: datetime


class ScenarioVersionSummaryResponse(ApiResponse):
    """模板版本列表项；用于页面选择具体版本，不假定版本号连续。"""

    version_no: int
    status: str
    manifest_hash: str
    published_at: datetime


class CreateTaskRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=255)
    enabled_rule_keys: list[str] | None = None


class TaskSchemaPreviewRequest(BaseModel):
    """建任务前预览实际执行范围；允许为空表示“全部启用”。"""

    enabled_rule_keys: list[str] | None = None


class TaskSchemaPreviewResponse(ApiResponse):
    """方案范围预览。**范围完全等于勾选的规则**——系统不补入任何规则，
    因此只有「已启用」与「未启用」两类，没有「自动补入」。"""

    enabled_rule_keys: list[str]
    unenabled_rule_keys: list[str] = Field(default_factory=list)
    # 未配置的检查类别：`..._types` 是能力类型汇总（「这类检查有没有配」），
    # `..._codes` 是这些类型下真实存在的节点实例码（「这个节点是不是未配置的类别」）。
    # 两者口径不同，页面不得交叉比较。
    unconfigured_check_node_types: list[str] = Field(default_factory=list)
    unconfigured_check_node_codes: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    field_count: int = 0


class CreateTaskResponse(ApiResponse):
    task_no: str
    task_name: str
    status: str
    current_node_name: str
    materials: list[dict[str, Any]]
    field_count: int
    unenabled_rule_keys: list[str] = Field(default_factory=list)
    # 任务实际执行范围：未配置 / 未启用 / 已启用的区分依据。
    scope: dict[str, Any] = Field(default_factory=dict)


class AuditTaskResponse(ApiResponse):
    task_no: str
    task_name: str
    # 任务备注是可编辑基本资料；执行快照不随重命名或备注修改变化。
    remark: str | None = None
    # 基本资料的时间：创建=任务建立时刻，更新=名称/备注/流程推进等任何写入。
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 方案绑定：为 None 表示旧任务没有方案关联（页面显示“历史任务配置”），
    # 不伪造关联、不改写历史结果。
    plan: dict[str, Any] | None = None
    is_legacy_config: bool = False
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    deleted: bool = False
    current_node: dict[str, Any]
    nodes: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    # **当前复核批次**：唯一允许提交的集合。复核分批判决（字段事实先于规则结论），
    # 页面只能提交这一批；不属于本批的待办留在服务端，提交它们会被明确拒绝。
    pending_reviews: list[dict[str, Any]]
    # 全部待办（含不属于本批的批次）：仅用于展示「还有多少项在排队」。
    all_pending_reviews: list[dict[str, Any]] = Field(default_factory=list)
    # 本批的身份：`review_nos` 是本批全部待办编号，`hash` 与服务端提交校验、
    # 检查点校验用的是同一个哈希；`stale=True` 表示待办集合在等待期间被改动过，
    # 页面必须刷新后再提交。`all_pending_count` 是排队总数。
    pending_review_batch: dict[str, Any] = Field(default_factory=dict)
    pending_review_hash: str | None = None
    pending_review_count: int = 0
    # 本模板未配置的业务校验类别与对应的节点实例；这些节点会显式跳过，不代表检查通过。
    # 未配置的检查类别：`..._types` 是能力类型汇总（「这类检查有没有配」），
    # `..._codes` 是这些类型下真实存在的节点实例码（「这个节点是不是未配置的类别」）。
    # 两者口径不同，页面不得交叉比较。
    unconfigured_check_node_types: list[str] = Field(default_factory=list)
    unconfigured_check_node_codes: list[str] = Field(default_factory=list)
    # 任务输入修订号：首次补传、替换材料、人工有效值变化都会递增。
    # 提交执行/复核命令时带上它，过期修订的命令会被明确拒绝。
    input_revision: int = 0
    # 本任务实际执行范围：未配置 / 未启用 / 不适用 的区分依据。
    scope: dict[str, Any] = Field(default_factory=dict)
    # 该任务上最近一次后台操作的受理状态；有活跃操作时页面据此继续查询。
    latest_operation: dict[str, Any] | None = None


class AuditTaskSummaryResponse(ApiResponse):
    task_no: str
    task_name: str
    scenario_name: str
    status: str
    current_node_name: str
    updated_at: datetime


class DocumentUploadResponse(ApiResponse):
    document_no: str
    material_name: str
    filename: str
    size_bytes: int
    status: str


class ExecuteNodeRequest(BaseModel):
    node_code: str
    # 幂等标识与请求内容绑定：同一节点重复提交才复用；换节点必须换标识。
    idempotency_key: str | None = Field(default=None, max_length=120)
    # 提交者看到的任务输入修订号；与任务当前修订不一致时明确冲突，不执行旧输入。
    input_revision: int | None = Field(default=None, ge=0)


class OperationRequest(BaseModel):
    """可能触发长处理的操作入口共用请求体。"""

    idempotency_key: str | None = Field(default=None, max_length=120)


class ExecuteNodeResponse(ApiResponse):
    task_no: str
    node_name: str
    execution_status: str
    task_status: str
    next_node_name: str | None
    result_summary: dict[str, Any]


class ManualReviewItem(BaseModel):
    review_no: str
    decision: Literal["CONFIRM_ABNORMAL", "MARK_NORMAL", "EVIDENCE_INSUFFICIENT", "CONFIRM_FIELD", "MODIFY_FIELD"]
    business_note: str = Field(min_length=1, max_length=2000)
    field_value: str | None = None
    # 这条人工结论的依据类型。默认人工业务判断；不得把旧引文当成新值的文档证明。
    basis_type: Literal["HUMAN_STATEMENT", "DOCUMENT_EVIDENCE", "UPLOAD_RECORD"] = "HUMAN_STATEMENT"


class ManualReviewRequest(BaseModel):
    reviews: list[ManualReviewItem] = Field(min_length=1)
    # 与本次复核集合绑定；重复提交同一集合时复用同一条后台操作。
    idempotency_key: str | None = Field(default=None, max_length=120)
    # 提交者看到的任务输入修订号；与任务当前修订不一致时明确冲突。
    input_revision: int | None = Field(default=None, ge=0)


class ManualReviewResponse(ApiResponse):
    task_no: str
    resolved_count: int
    status: str
    next_node_name: str | None
    # 本次提交真实改动了哪些字段事实（字段键、修改前后值）；不是只返回数量。
    field_changes: list[dict[str, Any]] = Field(default_factory=list)
    invalidated_reviews: int = 0
    # 材料类型确认后是否就地重算了文件质量结论（含重算批次的序号与结果分布）；
    # 未触发重算时为 None，页面据此区分“已重算”与“本次无需重算”。
    file_quality_recompute: dict[str, Any] | None = None
    # 本批之后仍未处理的有效待办条数：大于 0 表示任务仍停在人工复核等待点等下一批，
    # 不等于已完成。页面据此继续等待，而不是把“本批已提交”当成复核结束。
    remaining_review_count: int = 0


class FieldCorrectionRequest(BaseModel):
    """对 PRESENT 错值 / NOT_PRESENT 漏值的主动纠错。

    纠错走与复核相同的类型校验、版本记录和下游重算通道：
    生成新的字段事实版本并把旧的置为非当前，旧复核与旧证据保留为历史。
    """

    field_key: str
    material_key: str
    decision: Literal["MODIFY_FIELD", "MARK_NOT_PRESENT"] = "MODIFY_FIELD"
    # MODIFY_FIELD 必须给出新值；MARK_NOT_PRESENT 表示该字段确实不存在于文档。
    field_value: str | None = None
    business_note: str = Field(min_length=1, max_length=2000)
    basis_type: Literal["HUMAN_STATEMENT", "DOCUMENT_EVIDENCE", "UPLOAD_RECORD"] = "HUMAN_STATEMENT"
    idempotency_key: str | None = Field(default=None, max_length=120)
    input_revision: int | None = Field(default=None, ge=0)


class FieldCorrectionResponse(ApiResponse):
    task_no: str
    field_key: str
    material_key: str
    review_no: str
    fact_revision_no: int
    previous_revision_no: int | None = None
    before_value: str | None = None
    after_value: str | None = None
    normalized_value: Any | None = None
    status: str
    invalidated_reviews: int = 0
    invalidated_rule_results: int = 0
    recomputed_rules: list[str] = Field(default_factory=list)


class OcrReconcileRequest(BaseModel):
    """OCR 提交结果未知时的人工对账动作。

    系统没有真实的供应商幂等/查询能力时既不自动重发也不标成功：
    ADOPT_JOB=人工提供已确认的供应商作业号并据此续查；
    RETRY_SUBMIT=人工明确放弃未知状态并重新提交（会记录新的提交尝试）。
    """

    action: Literal["ADOPT_JOB", "RETRY_SUBMIT"]
    job_id: str | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=120)


class OcrReconcileResponse(ApiResponse):
    document_no: str
    artifact_no: str
    status: str
    job_id: str | None = None
    deadline_at: datetime | None = None
    message: str


class RiskListResponse(ApiResponse):
    task_no: str
    risk_configured: bool = False
    risk_executed: bool = False
    # 风险是否形成可用结论；与“命中 0 条”不是一回事。
    risk_conclusion_available: bool = False
    # 只统计 ABNORMAL，不能用结果总数冒充命中数。
    hit_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)


class ReportSnapshotSignal(BaseModel):
    """报告生成时刻的真实范围快照，用于把报告与当时的输入修订对账。"""

    task_input_revision: int = 0
    upload_ids: list[int] = Field(default_factory=list)
    field_value_ids: list[int] = Field(default_factory=list)
    review_ids: list[int] = Field(default_factory=list)
    rule_result_ids: list[int] = Field(default_factory=list)


class ReportFormatSupport(BaseModel):
    """一种声明输出格式的真实支持状态。

    声明（业务对报告形态的期望）与实现（系统能否真的产出该格式文件）必须分开表达：
    节点版模板在 07 声明 PDF / Word / Excel，当前只生成 Markdown 正文。
    只记录 formats、不记录支持状态，会让页面把未实现的格式显示成「可执行的输出格式」。
    """

    format: str
    supported: bool
    message: str = ""
    report_code: str | None = None
    report_name: str | None = None


class ReportResponse(ApiResponse):
    task_no: str
    status: Literal["COMPLETE", "INCOMPLETE", "NOT_GENERATED"]
    content_type: Literal["text/markdown"]
    content: str | None
    reasons: list[str]
    # 三个信号必须分开表达：报告是否生成、已配置审核范围是否执行完整、风险是否可判定。
    # 不能用单独一个 status 表示“全部通过”，也不能因为未配置风险评级就判定执行故障。
    report_generated: bool = False
    scope_execution_complete: bool = False
    risk_conclusion_available: bool = False
    # 报告结果范围：只对应当前有效事实与执行结果，历史结果不再进入当前结论。
    results_scope: Literal["CURRENT_RUN"] = "CURRENT_RUN"
    # 当前入口返回的报告是否仍对应当前输入修订；为 false 时页面必须标明“已失效”。
    current: bool = True
    input_revision: int = 0
    snapshot: ReportSnapshotSignal | None = None
    # 本节报告声明的输出格式与各自的真实支持状态；缺少该字段会让页面无从判断
    # 「模板要求 PDF、系统只产 Markdown」这件事。
    format_support: list[ReportFormatSupport] = Field(default_factory=list)
    # 本次报告正文里，系统固定事实章节之外的业务附加章节数量。零业务章节不等于零报告，
    # 也不等于审核范围完整，因此单独表达。
    business_section_count: int = 0
    # 报告结论的结构化形式（报告是否生成 / 范围是否完整 / 风险是否可判定 / 未实现的格式 / 能力缺口）。
    # 页面优先读它；报告正文里的中文行仍是给人读的原文，页面不再靠匹配行首文案提取结论。
    conclusion: dict[str, Any] | None = None


class ReplacementImpact(BaseModel):
    """替换上传文件的影响范围：历史事实仍可追溯，但不再进入当前报告。"""

    affected_artifacts: int = 0
    stale_field_values: int = 0
    invalidated_rule_results: int = 0
    invalidated_node_executions: int = 0
    invalidated_reviews: int = 0
    reset_to_node: str | None = None
    description: str = ""


class DocumentUploadResponse(ApiResponse):
    document_no: str
    material_name: str
    filename: str
    size_bytes: int
    status: str
    impact: ReplacementImpact | None = None


class EvidenceResponse(ApiResponse):
    evidence_no: str
    document_name: str | None
    page_no: int | None
    quote_text: str
    bbox: list[float] | None
    # 定位核对结论与所属上传版本：证据抽屉必须能显示「未核实/无法定位」与「当前/历史上传版本」，
    # 这些字段由 evidence_view 计算，声明缺失会在响应模型校验时被静默丢弃。
    locator_status: str
    locator_note: str | None = None
    # 内容核对独立于页码坐标；声明服务层返回值，避免响应序列化时丢失。
    quote_check: str
    quote_check_label: str
    document_upload_no: str | None = None
    artifact_type: str | None = None
    is_current_upload: bool = False
    historical: bool = False


# 管理端通用分页


class PagedResponse(ApiResponse):
    """后端分页结构：筛选、排序、当前页、页大小与同一作用域下的 total。

    细节条目保持结构化字典（各页面字段不同），但 total 永远来自服务端计数，
    绝不用当前页数组长度冒充。
    """

    total: int
    page: int
    page_size: int
    items: list[dict[str, Any]] = Field(default_factory=list)


class OperationLogPageResponse(PagedResponse):
    pass


class RiskResultPageResponse(PagedResponse):
    """风险结果分页：命中数只统计 ABNORMAL，与 total（当前筛选总行数）不是一回事。"""

    hit_count: int = 0
    # 当前筛选作用域内实际出现过的风险等级。页面筛选下拉从这里取，
    # 不再硬编码旧四级——两套等级标签（高/中/低/待确认 与 最高/一级/二级/三级）并存，
    # 硬编码一份会让另一套一条都筛不出来，而页面看起来正常。
    risk_levels: list[str] = Field(default_factory=list)


# 场景管理（页面配置）


class ScenarioCreateRequest(BaseModel):
    scenario_name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool = True


class ScenarioUpdateRequest(BaseModel):
    scenario_name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class ScenarioEnabledRequest(BaseModel):
    enabled: bool


class ScenarioCopyRequest(BaseModel):
    scenario_name: str | None = Field(default=None, max_length=255)


class ScenarioPublishRequest(BaseModel):
    # 发布必须携带页面看到的草稿修订号；不一致时明确冲突，要求刷新后重发。
    expected_revision: int = Field(ge=0)
    change_summary: str | None = Field(default=None, max_length=500)
    # 能力缺口默认阻断发布。要继续发布，必须把缺口规则键逐条列在这里。
    # 未列出的缺口仍然阻断；这不是「忽略全部缺口」的开关。
    acknowledged_capability_gaps: list[str] = Field(default_factory=list, max_length=500)


class DraftNodeRequest(BaseModel):
    """新增节点实例。`node_code` 是**实例码**（同一场景可以有两个同类节点）。"""

    node_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    # 能力类型来自统一模板协议的 node_types，决定谁执行这个节点。
    node_type: str = Field(min_length=1, max_length=40)
    name: str = Field(default="", max_length=120)
    sequence_no: int | None = Field(default=None, ge=1)
    # 上游节点**实例码**列表。缺失依赖与自依赖在此报出，循环依赖由发布期 DAG 校验兜底。
    depends_on: list[str] = Field(default_factory=list)
    config_json: dict[str, Any] | None = None
    enabled: bool = True


class DraftNodeUpdateRequest(BaseModel):
    node_type: str | None = Field(default=None, min_length=1, max_length=40)
    name: str | None = Field(default=None, max_length=120)
    sequence_no: int | None = Field(default=None, ge=1)
    depends_on: list[str] | None = None
    config_json: dict[str, Any] | None = None
    enabled: bool | None = None


class DraftSheetSelection(BaseModel):
    """场景选用的一张 Sheet 及它归属的节点实例。

    场景负责配置「使用哪些 Sheet、Sheet 对应哪些节点及其流程关系」，
    统一模板协议负责定义「系统支持哪些 Sheet 以及每张 Sheet 的结构」。
    这份选择是**用户配置**，不由系统按草稿内容反推。
    """

    sheet_role: str = Field(min_length=1, max_length=80)
    # 该 Sheet 归属的节点实例码。为空表示本场景选用这张表但尚未指定节点。
    node_code: str | None = Field(default=None, max_length=80)


class DraftSheetSelectionRequest(BaseModel):
    """整体替换本场景选用的 Sheet 清单。

    一次提交完整清单（而不是逐条增删）：Sheet 选择是**一份集合**，
    逐条增删会让「这次提交之后到底选了哪些表」取决于历史操作顺序。
    """

    sheets: list[DraftSheetSelection] = Field(default_factory=list)


class DraftDisabledSourceRowUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    values: dict[str, str]


class DraftDisabledSourceRowActivateRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class DraftDisabledSourceRowActivationPreviewItem(BaseModel):
    row_key: str
    sheet_role: str
    sheet_name: str
    row_no: int
    source_id: str | None = None


class DraftDisabledSourceRowActivationPreviewResponse(ApiResponse):
    revision_no: int
    rows: list[DraftDisabledSourceRowActivationPreviewItem]
    updated_risk_codes: list[str]


class DraftMaterialRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    required: bool = True
    description: str = Field(default="", max_length=4000)
    # 业务编号（Excel 的「材料编号」列）：导出规则库时写回该列，导入时它是必填项。
    source_ref: str | None = Field(default=None, max_length=80)


class DraftMaterialUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    required: bool | None = None
    description: str | None = Field(default=None, max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftFieldRequest(BaseModel):
    material_key: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=255)
    value_type: Literal["STRING", "DECIMAL", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"] = "STRING"
    unit: str | None = Field(default=None, max_length=40)
    # 币种是金额比较与加总的口径之一；留空表示未声明，不表示人民币。
    currency: str | None = Field(default=None, max_length=40)
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别 SEAL）。
    artifact_source: Literal["SEAL"] | None = None
    # 取数期间与报表口径：**由字段自己声明**，计算参数按这两项定位字段事实
    # （参数表不再声明它们）。合同场景没有这些概念，因此**允许留空**，落成显式的
    # 「不适用」——不是 NULL（统一事实表的当前版本唯一键由它拼成），也不是默认本期。
    # 被计算参数引用的字段不能用「不适用」，发布期会明确拦下。
    period_scope: Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    statement_scope: Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    required: bool = False
    instruction: str = Field(default="", max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftFieldUpdateRequest(BaseModel):
    material_key: str | None = Field(default=None, min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    value_type: Literal["STRING", "DECIMAL", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"] | None = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    artifact_source: Literal["SEAL"] | None = None
    period_scope: Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"] | None = None
    statement_scope: Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"] | None = None
    required: bool | None = None
    instruction: str | None = Field(default=None, max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftRuleInput(BaseModel):
    field_key: str = Field(min_length=1, max_length=40)
    material_key: str = Field(min_length=1, max_length=40)


class DraftRuleApplicability(BaseModel):
    kind: Literal["ALWAYS", "MATERIALS_UPLOADED"] = "ALWAYS"


class DraftRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    node: str = Field(min_length=1, max_length=40)
    operator: str = Field(min_length=1, max_length=40)
    enabled: bool = True
    inputs: list[DraftRuleInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    requirement: str = Field(default="", max_length=4000)
    # 「检查等级」是列协议里的必填业务口径（导入期缺失直接报 RULE_CHECK_LEVEL_REQUIRED）。
    # 页面表单必须能提交它，否则页面新建的规则没有等级，导出后重新导入必然被拒——
    # 「导出再导入完整保留」在页面路径上就永远不成立。
    check_level: str | None = Field(default=None, max_length=40)
    # 风险的成立条件（任一 / 全部关联检查不合格）：列协议里是必填枚举。
    # 与检查等级同理，页面必须能提交它，否则页面新建的风险导出后成立条件为空，
    # 重新导入被枚举校验拒掉。
    risk_condition: str | None = Field(default=None, max_length=40)
    applicability: DraftRuleApplicability = Field(default_factory=DraftRuleApplicability)
    source_node: str | None = Field(default=None, max_length=40)
    source_rule_key: str | None = Field(default=None, max_length=40)
    # 普通检查规则键与计算结果编号分开保存，不能混用两种身份。
    source_rule_keys: list[str] = Field(default_factory=list)
    source_rule_codes: list[str] = Field(default_factory=list)
    # 业务编号（Excel 的「规则编号」列）：导出时写回，导入时必填。
    source_ref: str | None = Field(default=None, max_length=80)


class DraftRuleEnabledRequest(BaseModel):
    enabled: bool


class DraftSectionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content_instruction: str = Field(default="", max_length=4000)
    field_keys: list[str] = Field(default_factory=list)
    # 节点版（contract_review_v3）的报告分组属性：报告编号、报告名称、材料范围、
    # 输出格式、章节顺序、内容类型、结果排序与业务配置状态。旧格式章节全部留空。
    report_code: str | None = Field(default=None, max_length=80)
    report_name: str | None = Field(default=None, max_length=255)
    material_keys: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    section_order: int | None = Field(default=None, ge=1)
    content_type: Literal["FIELD_DETAIL", "AUDIT_RESULT", "FIELD_AND_RESULT", "RISK_LIST", "CONCLUSION"] | None = None
    result_sort: Literal["RISK_LEVEL_DESC", "MATERIAL_ORDER", "MATERIAL_ORDER_RISK_LEVEL_DESC"] | None = None
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] | None = None


class DraftSectionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content_instruction: str | None = Field(default=None, max_length=4000)
    field_keys: list[str] | None = None
    report_code: str | None = Field(default=None, max_length=80)
    report_name: str | None = Field(default=None, max_length=255)
    material_keys: list[str] | None = None
    formats: list[str] | None = None
    section_order: int | None = Field(default=None, ge=1)
    content_type: Literal["FIELD_DETAIL", "AUDIT_RESULT", "FIELD_AND_RESULT", "RISK_LIST", "CONCLUSION"] | None = None
    result_sort: Literal["RISK_LEVEL_DESC", "MATERIAL_ORDER", "MATERIAL_ORDER_RISK_LEVEL_DESC"] | None = None
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] | None = None


class ScenarioDraftViewResponse(ApiResponse):
    scenario_code: str
    revision_no: int
    source: str
    source_label: str
    base_version_no: int | None = None
    # 草稿的来源格式版本：由导入发布 / 版本修订 / 复制继承；页面新建时为 null。
    profile_version: str | None = None
    # 当前配置是否已生效：null 表示已生效；非空时为 {code, message, details}，
    # 页面据此显示“尚未生效 + 具体原因”。编译未通过不阻断保存（建配置过程中必然
    # 不完整），但配置必须在页面上明确标为未生效，不能被当成已生效。
    sync_error: dict[str, Any] | None = None
    updated_at: datetime
    # 用户配置的**节点实例**：流程编排与 Sheet 绑定的唯一来源。
    # 少了这一列，服务层算好的节点清单会被响应模型整体丢掉——页面拿到的草稿里
    # 一个节点都没有，而节点本身已经真实落库。
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    # 本场景**选用**的 Sheet：模板下载与绑定步骤按它取值，不从草稿内容反推。
    sheets: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    disabled_source_rows: list[dict[str, Any]] = Field(default_factory=list)
    # 草稿里的计算配置（定义 / 参数 / 判定）。与 `nodes` 同一类问题：服务层算好的清单
    # 若不在响应模型里声明，会被整体丢掉，页面看到「没有计算配置」而库里其实有。
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    business_rule_count: int = 0
    system_rule_count: int = 0


class ScenarioVersionConfigResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    version_no: int
    source: str
    source_label: str
    change_summary: str | None = None
    published_at: datetime
    manifest_hash: str
    # 未配置的检查类别：`..._types` 是能力类型汇总（「这类检查有没有配」），
    # `..._codes` 是这些类型下真实存在的节点实例码（「这个节点是不是未配置的类别」）。
    # 两者口径不同，页面不得交叉比较。
    unconfigured_check_node_types: list[str] = Field(default_factory=list)
    unconfigured_check_node_codes: list[str] = Field(default_factory=list)
    capability_gaps: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    disabled_source_rows: list[dict[str, Any]] = Field(default_factory=list)
    # 草稿里的计算配置（定义 / 参数 / 判定）。与 `nodes` 同一类问题：服务层算好的清单
    # 若不在响应模型里声明，会被整体丢掉，页面看到「没有计算配置」而库里其实有。
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    business_rule_count: int = 0
    system_rule_count: int = 0


class ScenarioPublishResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    source: str
    change_summary: str | None = None
    manifest_hash: str
    material_count: int
    field_count: int
    rule_count: int
    published_at: datetime
    # 内容与最新已发布版本完全一致时复用该版本，不制造重复版本。
    reused: bool = False


class ScenarioDetailResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    category: str | None = None
    description: str | None = None
    enabled: bool
    deleted: bool
    latest_version_no: int
    created_at: datetime
    updated_at: datetime
    plan_count: int = 0
    draft: dict[str, Any] | None = None
    versions: list[dict[str, Any]] = Field(default_factory=list)


class VersionCompareResponse(ApiResponse):
    scenario_code: str
    from_version_no: int
    to_version_no: int
    entries: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


# 方案管理


class PlanCreateRequest(BaseModel):
    """新建方案请求。

    `scenario_version_no` 是页面预览时看到的那一版：它必须与创建时该场景当前允许创建
    方案的已发布版本一致。缺省或版本变化都明确拒绝，不在创建时静默改取「当前最新版本」——
    那会让用户看着场景 A/v1 的预览，实际建出场景 B 或 v2 的方案。
    """

    model_config = ConfigDict(extra="forbid")

    plan_name: str = Field(min_length=1, max_length=255)
    scenario_code: str = Field(min_length=1, max_length=80)
    scenario_version_no: int = Field(ge=1)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool = True


class PlanUpdateRequest(BaseModel):
    plan_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class PlanEnabledRequest(BaseModel):
    enabled: bool


class PlanCopyRequest(BaseModel):
    plan_name: str | None = Field(default=None, max_length=255)


class PlanConfigUpdateRequest(BaseModel):
    """整体提交方案的配置和计算规则启停选择。

    五个列表各自是一份**完整集合**：这次提交之后这个方案配了哪些就由它决定，
    不取决于历史操作顺序。逐项的必填键与取值校验在 `plan_service.update_plan_config`：
    那里要给出「第几条缺了哪个键」这类可定位的错误，写在契约里只能报一句「校验失败」。

    节点与选用 Sheet 不在这里 —— 它们是场景级执行结构，方案里只读。
    """

    model_config = ConfigDict(extra="forbid")

    materials: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    calculation_rules: list[dict[str, Any]]


class PlanFromTaskRequest(BaseModel):
    task_no: str = Field(min_length=1, max_length=40)
    plan_name: str = Field(min_length=1, max_length=255)


class PlanSelectionResponse(ApiResponse):
    plan_code: str
    plan_name: str
    scenario_code: str
    description: str | None = None
    enabled: bool
    deleted: bool
    # 范围是否已生效：sync_error 非空表示最近一次保存没有算出可用范围，
    # 页面据此显示「待核对 + 原因」，且该方案不能被任务引用。
    effective: bool = False
    sync_error: dict[str, Any] | None = None
    scope_hash: str | None = None
    base_scenario_version: dict[str, Any] | None = None
    # 方案自己的全部规则与启停：这就是「这个方案跑哪些规则」的唯一来源。
    # 不再有 selected_rule_keys / auto_included_* —— 系统不替用户补规则，勾了什么就是什么。
    rules: list[dict[str, Any]] = Field(default_factory=list)
    calculation_rules: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    field_count: int = 0
    updated_at: datetime


class TaskFromPlanRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)
    # 页面预览看到的方案范围指纹。后端锁定方案后比对，不一致时返回 PLAN_SCOPE_CHANGED：
    # 任务必须冻结用户刚刚确认的那一版范围，不在创建时静默取一个已经改过的范围。
    expected_scope_hash: str = Field(min_length=1, max_length=128)
    # 创建任务是一次真实写操作：响应丢失时用同一幂等标识重发，返回原任务，不重复创建。
    idempotency_key: str = Field(min_length=1, max_length=120)


class TaskCreatedResponse(ApiResponse):
    task_no: str
    task_name: str
    task_status: str
    scenario_code: str
    scenario_name: str
    scenario_version_no: int
    plan_name: str
    current_node_name: str
    material_count: int
    field_count: int
    reused: bool = False


class TaskUpdateRequest(BaseModel):
    task_name: str | None = Field(default=None, min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)


class TaskCopyRequest(BaseModel):
    """复制任务：按源任务所属方案的**当前范围**重新创建一个新任务。

    它**不是**「复制原任务的执行快照」：新任务冻结的是方案此刻的范围，
    源任务的冻结快照与已发生的执行事实一概不带过来。
    """

    task_name: str = Field(min_length=1, max_length=255)
    # 复制同样是一次真实写操作：响应丢失时用同一幂等标识重发，返回原任务。
    idempotency_key: str | None = Field(default=None, max_length=120)


class DocumentRemoveResponse(ApiResponse):
    document_no: str
    material_name: str
    status: str
    input_revision: int
    impact: ReplacementImpact | None = None


class SystemServiceItem(BaseModel):
    name: str
    capability: str
    configuration_status: str
    connection_status: str
    last_checked_at: datetime | None = None
    missing: list[str] = Field(default_factory=list)
    note: str | None = None


class SystemServicesResponse(ApiResponse):
    database: Literal["CONNECTED", "FAILED"]
    services: list[SystemServiceItem] = Field(default_factory=list)


class ReportRemarkRequest(BaseModel):
    remark: str | None = Field(default=None, max_length=4000)


class ReviewDraftItem(BaseModel):
    review_no: str = Field(min_length=1, max_length=40)
    decision: str | None = Field(default=None, max_length=40)
    business_note: str | None = Field(default=None, max_length=2000)
    field_value: str | None = Field(default=None, max_length=2000)
    basis_type: str | None = Field(default=None, max_length=30)


class ReviewDraftRequest(BaseModel):
    items: list[ReviewDraftItem] = Field(default_factory=list)


class ReviewDraftResponse(ApiResponse):
    task_no: str
    pending_hash: str | None = None
    saved_review_nos: list[str] = Field(default_factory=list)
    drafts: dict[str, dict[str, Any]] = Field(default_factory=dict)


# 审核任务检索取证（RAG）
#
# 检索记录只用于取证：相似度仅作候选排序，不构成主体一致或审核通过的证明；
# 检索不到内容不等于合同不存在该条款。这些结论必须如实呈现在响应里。


class RetrievalRequest(BaseModel):
    """一次任务内检索：查询内容与材料范围都必须显式给出。"""

    query_text: str = Field(min_length=1, max_length=4000)
    # 材料范围：只能引用本任务已发布的材料 key；为空表示未限定范围，服务端会明确拒绝。
    material_keys: list[str] = Field(min_length=1)
    rule_key: str | None = Field(default=None, max_length=40)
    top_k: int | None = Field(default=None, ge=1, le=64)


class RetrievalCandidateResponse(BaseModel):
    """一条印章主体候选。

    主体名称就是检索文本本身（向量只由它生成）。页码等定位信息不在主体确认界面展示，
    也不参与相似度；候选身份用于人工选择与提交校验。
    """

    chunk_id: int
    chunk_no: int
    index_no: str | None = None
    # 印章条目在原产物中的条目序号；用于回到原始条目。
    source_entry_no: int | None = None
    subject_name: str
    material_key: str | None = None
    material_name: str | None = None
    filename: str | None = None
    upload_no: str | None = None
    # 相似度只用于排序，不构成主体一致的结论。
    similarity: str


class RetrievalResponse(ApiResponse):
    task_no: str
    retrieval_no: str
    status: str
    message: str
    model_id: str | None = None
    model_revision: str | None = None
    index_version: str | None = None
    index_versions: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    candidates: list[RetrievalCandidateResponse] = Field(default_factory=list)
    # 本次实际送入后续步骤的正文条目；它是上下文预算，不是人工结论。
    adopted: list[RetrievalCandidateResponse] = Field(default_factory=list)
    # 有上传但没有可用主体索引的材料及原因（尚未识别主体 / 尚未建索引 / 索引失效）。
    uncovered_materials: list[dict[str, Any]] = Field(default_factory=list)
    excluded_indexes: list[dict[str, Any]] = Field(default_factory=list)


class RetrievalDecisionRequest(BaseModel):
    """工作人员对一次主体检索候选的结论。

    只接受一致 / 不一致 / 证据不足；相似度不能代替审核结论，
    也不存在“自动采纳前几条候选”的取值。
    """

    decision: str = Field(pattern="^(CONSISTENT|INCONSISTENT|EVIDENCE_INSUFFICIENT)$")
    note: str | None = Field(default=None, max_length=2000)
    # 确认一致或不一致时必须指明所依据的候选（本次检索返回的候选身份）。
    selected: dict[str, Any] | None = None
    # 提交时的任务输入修订：与当前修订不一致说明候选已过期。
    expected_input_revision: int | None = Field(default=None, ge=0)


class RetrievalDecisionResponse(ApiResponse):
    """人工对主体候选的结论。

    selected 只在该次检索真实返回的候选内取值；decision_input_revision 记录提交时
    的任务输入修订，修订变化后旧结论不得套用到新的候选。
    """

    task_no: str
    retrieval_no: str
    decision: str
    note: str | None = None
    selected: dict[str, Any] | None = None
    decided_at: datetime | None = None
    decision_input_revision: int | None = None


class EvidenceIndexItem(BaseModel):
    """一个上传版本某个印章产物的索引状态。"""

    index_no: str
    artifact_type: str
    status: str
    # 索引是否仍适用于当前产物内容：产物重新识别后旧索引只是历史记录。
    current_for_artifact: bool = False
    chunk_count: int
    model_id: str
    model_revision: str
    embedding_dim: int | None = None
    chunk_version: str | None = None
    index_version: str
    document_upload_no: str | None = None
    filename: str | None = None
    material_name: str | None = None
    built_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class TaskEvidenceIndexResponse(ApiResponse):
    task_no: str
    # 有索引记录时为真实索引版本；没有任何索引时为 None（不编造一个“当前版本”）。
    index_version: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    chunk_version: str | None = None
    indexed: bool = False
    items: list[EvidenceIndexItem] = Field(default_factory=list)
