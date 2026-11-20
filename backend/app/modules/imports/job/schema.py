from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.modules.system.operation.schema import OperationAcceptedResponse


class ImportJobRowResponse(BaseModel):
    job_no: str
    source_filename: str
    status: str
    profile_version: str
    scenario_code: str | None
    scenario_name: str | None
    draft_revision: int
    source_size: int
    published_version_no: int | None
    active_operation_count: int
    latest_operation: OperationAcceptedResponse | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class ImportJobResponse(BaseModel):
    job_no: str
    source_filename: str
    status: str
    profile_version: str
    schema_key: str | None = Field(default=None, description="上传时冻结的业务场景 Schema 标识")
    schema_version: str | None = Field(default=None, description="业务场景 Schema 版本")
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")
    profile: dict[str, Any] = Field(default_factory=dict)
    proposals: dict[str, Any] | None = None
    draft_revision: int = 0
    # 第一轮目录可复用：为 True 时可以只重跑第二轮。
    can_resume_refinement: bool = False
    failure: dict[str, str] | None = None
    latest_operation: dict[str, Any] | None = None
    created_at: datetime


class ImportJobSummaryResponse(BaseModel):
    job_no: str
    source_filename: str
    status: str
    created_at: datetime

