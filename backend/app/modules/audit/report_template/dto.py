"""报告模板写入请求。"""

from typing import Any

from pydantic import BaseModel, Field


class ReportTemplateUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # 缺省或 null 只更新名称，不清除现有档案或分析诊断。
    style_profile: dict[str, Any] | None = None


class ReportTemplateAnalyzeRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
