
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.configuration.execution_mode import ExecutionMode

ImportProposalGroup = Literal["materials", "fields", "rules", "calculations", "report_sections"]


class ImportConversionRequest(BaseModel):
    """提交规则库转换；可选择复用第一轮结果只重跑第二轮。"""

    idempotency_key: str | None = Field(default=None, max_length=120)
    mode: Literal["FULL", "RESUME_REFINEMENT"] = "FULL"


class ImportProposalChanges(BaseModel):
    """解析结果编辑器提交的业务属性；未提交的属性保持原值。"""

    name: str = Field(default="", min_length=1)
    description: str = ""
    required: bool = False
    instruction: str = ""
    requirement: str = ""
    execution_mode: ExecutionMode = "UNKNOWN"
    rule_role: Literal["CHECK", "RISK"] = "CHECK"
    rule_name: str = Field(default="", min_length=1)
    formula: str = ""
    section_name: str = Field(default="", min_length=1)
    content_instruction: str = ""
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = "CLEAR"
    status: Literal["CLEAR", "PENDING", "DISABLED"] = "CLEAR"


class UpdateImportProposalRequest(BaseModel):
    expected_job_revision: int = Field(ge=0)
    changes: ImportProposalChanges


class ApplyImportDraftRequest(BaseModel):
    expected_job_revision: int = Field(ge=0)
    expected_scene_revision: int | None = Field(ge=0)
