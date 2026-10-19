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

class OperationRequest(BaseModel):
    """可能触发长处理的操作入口共用请求体。"""

    idempotency_key: str | None = Field(default=None, max_length=120)

class DocumentOcrRetryRequest(BaseModel):
    """只续查或重试当前材料的 OCR，绑定页面看到的输入和作业身份。"""

    idempotency_key: str = Field(min_length=1, max_length=120)
    input_revision: int = Field(ge=0)
    artifact_no: str | None = Field(default=None, min_length=1, max_length=40)
    resubmit_unknown: bool = Field(default=False, description="用户确认重新提交结果未知的 OCR，可能产生重复的外部作业")


class DocumentArtifactRetryRequest(BaseModel):
    """单项材料处理重试；未知结果必须由用户明确确认。"""
    idempotency_key: str = Field(min_length=1, max_length=120)
    input_revision: int = Field(ge=0)
    artifact_no: str = Field(min_length=1, max_length=40)
    resubmit_unknown: bool = False

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


class TaskUpdateRequest(BaseModel):
    task_name: str | None = Field(default=None, min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)

class TaskCopyRequest(BaseModel):
    """复制任务：按源任务所属方案的当前范围重新创建一个新任务。

    它不是「复制原任务的执行快照」：新任务冻结的是方案此刻的范围，
    源任务的冻结快照与已发生的执行事实一概不带过来。
    """

    task_name: str = Field(min_length=1, max_length=255)
    # 复制同样是一次真实写操作：响应丢失时用同一幂等标识重发，返回原任务。
    idempotency_key: str = Field(min_length=1, max_length=120)

__all__ = [
    "TaskSchemaPreviewRequest",
    "ExecuteNodeRequest",
    "OperationRequest",
    "DocumentOcrRetryRequest",
    "DocumentArtifactRetryRequest",
    "ManualReviewItem",
    "ManualReviewRequest",
    "FieldCorrectionRequest",
    "TaskUpdateRequest",
    "TaskCopyRequest",
]

