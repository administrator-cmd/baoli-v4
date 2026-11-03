"""审核请求模型。"""

from typing import Literal

from pydantic import BaseModel, Field


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


class FieldReviewCorrection(BaseModel):
    """核对期间的单个字段修改：改值、补填或标记为材料中不存在。

    `expected_revision_no` 是页面看到的字段事实版本；没有字段事实（漏抽）时为 null。
    服务端按它拒绝用旧页面覆盖新输入。
    """

    field_key: str = Field(min_length=1, max_length=40)
    material_key: str = Field(min_length=1, max_length=40)
    decision: Literal["MODIFY_FIELD", "MARK_NOT_PRESENT"]
    field_value: str | None = Field(default=None, max_length=4000)
    business_note: str = Field(min_length=1, max_length=2000)
    basis_type: Literal["HUMAN_STATEMENT", "DOCUMENT_EVIDENCE", "UPLOAD_RECORD"] = "HUMAN_STATEMENT"
    expected_revision_no: int | None = Field(default=None, ge=1)


class FieldReviewSaveRequest(BaseModel):
    """只保存字段核对修改，任务继续等待核对。"""

    input_revision: int = Field(ge=0)
    corrections: list[FieldReviewCorrection] = Field(min_length=1, max_length=200)


class FieldReviewConfirmRequest(BaseModel):
    """保存本次剩余修改并确认当前字段版本，随后受理后续执行。

    `acknowledge_missing` 只在仍有缺项时为必填确认：它表示用户看过真实缺项后仍允许继续，
    不等于系统补齐了缺失值。
    """

    idempotency_key: str = Field(min_length=1, max_length=120)
    input_revision: int = Field(ge=0)
    corrections: list[FieldReviewCorrection] = Field(default_factory=list, max_length=200)
    acknowledge_missing: bool = False
    note: str | None = Field(default=None, max_length=2000)
