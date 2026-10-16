
from typing import Literal

from pydantic import BaseModel, Field


ImportProposalGroup = Literal["materials", "fields", "rules", "calculations", "report_sections"]


class ImportProposalChanges(BaseModel):
    """解析结果编辑器提交的业务属性；未提交的属性保持原值。"""

    name: str = Field(default="", min_length=1)
    description: str = ""
    required: bool = False
    instruction: str = ""
    requirement: str = ""
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
