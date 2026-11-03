"""审核请求模型。"""


from pydantic import BaseModel, Field


class ReportRemarkRequest(BaseModel):
    remark: str | None = Field(default=None, max_length=4000)
