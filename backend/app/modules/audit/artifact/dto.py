"""审核请求模型。"""


from pydantic import BaseModel, Field


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
