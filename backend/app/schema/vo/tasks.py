from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ...common.api import ApiResponse


class TaskSchemaPreviewResponse(ApiResponse):
    """方案范围预览。范围完全等于勾选的规则——系统不补入任何规则，
    因此只有「已启用」与「未启用」两类，没有「自动补入」。"""

    enabled_rule_keys: list[str]
    unenabled_rule_keys: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    field_count: int = 0

class AuditTaskResponse(ApiResponse):
    intermediate_csv_available: bool = False
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
    field_correction_candidates: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]]
    # 当前复核批次：唯一允许提交的集合。复核分批判决（字段事实先于规则结论），
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
    # 任务输入修订号：首次补传、替换材料、人工有效值变化都会递增。
    # 提交执行/复核命令时带上它，过期修订的命令会被明确拒绝。
    input_revision: int = 0
    # 本任务实际执行范围：未配置 / 未启用 / 不适用 的区分依据。
    scope: dict[str, Any] = Field(default_factory=dict)
    # 该任务上最近一次后台操作的受理状态；有活跃操作时页面据此继续查询。
    latest_operation: dict[str, Any] | None = None
    can_recover_failed_node: bool = False

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

class ExecuteNodeResponse(ApiResponse):
    task_no: str
    node_name: str
    execution_status: str
    task_status: str
    next_node_name: str | None
    result_summary: dict[str, Any]

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

class ExternalArtifactStatusResponse(ApiResponse):
    document_no: str
    artifact_type: str
    artifact_no: str | None = None
    status: str
    message: str

class ExternalArtifactReconcileResponse(ApiResponse):
    document_no: str
    artifact_type: str
    artifact_no: str
    status: str
    message: str

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

class EvidenceResponse(ApiResponse):
    evidence_no: str
    document_name: str | None
    field_path: str | None
    quote_text: str
    # 引用原文在所属处理产物中的核对结论。
    quote_check: str
    quote_check_label: str
    document_upload_no: str | None = None
    artifact_type: str | None = None
    is_current_upload: bool = False
    historical: bool = False

class DocumentRemoveResponse(ApiResponse):
    document_no: str
    material_name: str
    status: str
    input_revision: int
    impact: ReplacementImpact | None = None

__all__ = [
    "TaskSchemaPreviewResponse",
    "AuditTaskResponse",
    "DocumentUploadResponse",
    "ExecuteNodeResponse",
    "FieldCorrectionResponse",
    "ExternalArtifactStatusResponse",
    "ExternalArtifactReconcileResponse",
    "OcrReconcileResponse",
    "RiskListResponse",
    "ReplacementImpact",
    "EvidenceResponse",
    "DocumentRemoveResponse",
]

