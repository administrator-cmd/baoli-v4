from typing import Literal

from pydantic import BaseModel, Field


class TaskSchemaPreviewRequest(BaseModel):
    """建任务前预览实际执行范围；允许为空表示“全部启用”。"""

    enabled_rule_keys: list[str] | None = None

class ExecuteNodeRequest(BaseModel):
    node_code: str
    # 幂等标识与请求内容绑定：同一节点重复提交才复用；换节点必须换标识。
    idempotency_key: str | None = Field(default=None, max_length=120)
    # 提交者看到的任务输入修订号；与任务当前修订不一致时明确冲突，不执行旧输入。
    input_revision: int | None = Field(default=None, ge=0)

class RecoverFailedNodeRequest(BaseModel):
    node_code: str
    input_revision: int = Field(ge=0)

class OperationRequest(BaseModel):
    """可能触发长处理的操作入口共用请求体。"""

    idempotency_key: str | None = Field(default=None, max_length=120)

class ManualReviewItem(BaseModel):
    review_no: str
    decision: Literal["CONFIRM_ABNORMAL", "MARK_NORMAL", "EVIDENCE_INSUFFICIENT", "CONFIRM_FIELD", "MODIFY_FIELD", "MARK_NOT_PRESENT"]
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

class OcrReconcileRequest(BaseModel):
    """OCR 提交结果未知时的人工对账动作。

    系统没有真实的供应商幂等/查询能力时既不自动重发也不标成功：
    ADOPT_JOB=人工提供已确认的供应商作业号并据此续查；
    RETRY_SUBMIT=人工明确放弃未知状态并重新提交（会记录新的提交尝试）。
    """

    action: Literal["ADOPT_JOB", "RETRY_SUBMIT"]
    artifact_no: str = Field(min_length=1, max_length=40)
    job_id: str | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=120)

class ExternalArtifactReconcileRequest(BaseModel):
    """人工确认同步外部调用结果未知，解除占位后重新执行节点。"""

    artifact_no: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=120)

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
    idempotency_key: str = Field(min_length=1, max_length=120)

__all__ = [
    "TaskSchemaPreviewRequest",
    "ExecuteNodeRequest",
    "RecoverFailedNodeRequest",
    "OperationRequest",
    "ManualReviewItem",
    "ManualReviewRequest",
    "FieldCorrectionRequest",
    "OcrReconcileRequest",
    "ExternalArtifactReconcileRequest",
    "TaskUpdateRequest",
    "TaskCopyRequest",
]

