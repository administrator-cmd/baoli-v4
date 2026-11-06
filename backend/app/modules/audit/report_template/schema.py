"""报告模板 API 响应契约。"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.modules.system.operation.schema import OperationAcceptedResponse

MAX_STYLE_PROFILE_BYTES = 8 * 1024


class ReportStyleSection(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    purpose: str = Field(default="", max_length=500)


class ReportStyleProfile(BaseModel):
    version: Literal["1.0"] = "1.0"
    document_overview: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    structure: dict[str, Any] = Field(default_factory=dict)
    sections: list[ReportStyleSection] = Field(min_length=1, max_length=40)
    expression: dict[str, Any] = Field(default_factory=dict)
    formatting_conventions: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def enforce_json_budget(self) -> Self:
        # 与报告提示词使用相同的 JSON 编码，嵌套字典及中文也计入总预算。
        try:
            size = len(json.dumps(
                self.model_dump(mode="python"), ensure_ascii=False, sort_keys=True, allow_nan=False,
            ).encode("utf-8"))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("风格档案必须是可序列化的有限 JSON 数据") from exc
        if size > MAX_STYLE_PROFILE_BYTES:
            raise ValueError("风格档案 JSON 的 UTF-8 编码不得超过 8 KiB（8192 字节）")
        return self


class ReportTemplateResponse(BaseModel):
    template_no: str
    name: str
    source_filename: str
    source_size: int
    page_count: int
    status: Literal["PROCESSING", "READY", "FAILED"]
    style_profile: ReportStyleProfile | None = None
    analysis_error: str | None = None
    latest_operation: OperationAcceptedResponse | None = None
    created_at: datetime
    updated_at: datetime


class ReportTemplateUploadAcceptedResponse(BaseModel):
    template: ReportTemplateResponse
    operation: OperationAcceptedResponse


class ReportTemplateOption(BaseModel):
    template_no: str
    name: str


class ReportTemplateListResponse(BaseModel):
    items: list[ReportTemplateResponse] = Field(default_factory=list)


class ReportTemplateOptionsResponse(BaseModel):
    items: list[ReportTemplateOption] = Field(default_factory=list)
