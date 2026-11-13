"""审核响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.system.operation.schema import OperationAcceptedResponse


class TaskRowResponse(BaseModel):
    task_no: str
    task_name: str
    remark: str | None
    scenario_code: str
    scenario_name: str
    scenario_version_no: int
    plan_code: str | None
    plan_name: str | None
    current_node: str
    current_node_name: str
    status: str
    created_at: datetime
    updated_at: datetime


class TaskPlanResponse(BaseModel):
    plan_code: str
    plan_name: str
    scenario_version_no: int


class TaskNodeResponse(BaseModel):
    """固定节点身份与运行状态；保留冻结配置中的扩展属性。"""

    model_config = ConfigDict(extra="allow")

    node_code: str
    name: str
    sequence_no: int
    enabled: bool
    status: str
    failure_code: str | None
    failure_message: str | None


class TaskDocumentResponse(BaseModel):
    """材料位的固定身份；质量与处理详情保留原有业务结构。"""

    model_config = ConfigDict(extra="allow")

    document_no: str
    material_key: str
    material_name: str
    required: bool
    status: str
    filename: str | None


class TaskFieldResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    field_key: str
    material_key: str
    name: str
    material_name: str
    status: str
    raw_value: str | None
    normalized_value: Any
    evidence: list[dict[str, Any]]


class TaskRuleResponse(BaseModel):
    rule_key: str
    name: str
    source_ref: str | None
    kind: str | None
    execution_mode: str | None
    is_risk_rule: bool
    source_sheet: str | None
    material_names: list[str]
    status: str
    message: str | None
    turn_count: int
    result: dict[str, Any]
    evidence: list[dict[str, Any]]
    configured_requirement: str | None
    input_gaps: list[dict[str, Any]]


class FieldGapGroupResponse(BaseModel):
    material_name: str
    period: str
    unit: str
    field_names: list[str]
    affected_rules: list[str]


class TaskFieldSummaryResponse(BaseModel):
    total: int
    valid: int
    missing: int
    not_executed: int
    gap_groups: list[FieldGapGroupResponse]


class FieldCorrectionCandidateResponse(BaseModel):
    field_key: str
    material_key: str
    field_name: str
    material_name: str
    status: str
    raw_value: str | None
    normalized_value: Any


class TaskSchemaPreviewResponse(BaseModel):
    """方案范围预览。范围完全等于勾选的规则——系统不补入任何规则，
    因此只有「已启用」与「未启用」两类，没有「自动补入」。"""

    enabled_rule_keys: list[str]
    unenabled_rule_keys: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    field_count: int = 0


class AuditTaskResponse(BaseModel):
    task_no: str
    task_name: str
    # 任务备注是可编辑基本资料；执行快照不随重命名或备注修改变化。
    remark: str | None = None
    # 基本资料的时间：创建=任务建立时刻，更新=名称/备注/流程推进等任何写入。
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 任务创建时冻结的方案身份。
    plan: TaskPlanResponse | None = None
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    current_node: TaskNodeResponse
    nodes: list[TaskNodeResponse]
    documents: list[TaskDocumentResponse]
    fields: list[TaskFieldResponse]
    # 字段结果统计与按材料/期间/单位汇总的缺项；页面统计与筛选用同一口径。
    field_summary: TaskFieldSummaryResponse | None = None
    field_correction_candidates: list[FieldCorrectionCandidateResponse] = Field(default_factory=list)
    rules: list[TaskRuleResponse]
    # 任务输入修订号：首次补传、替换材料、人工有效值变化都会递增。
    # 提交执行/复核命令时带上它，过期修订的命令会被明确拒绝。
    input_revision: int = 0
    # 本任务实际执行范围：未配置 / 未启用 / 不适用 的区分依据。
    scope: dict[str, Any] = Field(default_factory=dict)
    # 任务冻结的技术容器、字段与展示结构；页面按它通用渲染。
    display: dict[str, Any] = Field(default_factory=dict)
    # 该任务上最近一次后台操作的受理状态；有活跃操作时页面据此继续查询。
    latest_operation: OperationAcceptedResponse | None = None


class TaskOptionItem(BaseModel):
    """任务下拉项：按名称或编号模糊匹配，最多 50 条。"""

    task_no: str
    task_name: str
    scenario_name: str
    status: str
