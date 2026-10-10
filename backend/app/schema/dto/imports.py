from typing import Any, Literal

from pydantic import BaseModel, Field


class SheetBindingItem(BaseModel):
    sheet_key: str
    excluded: bool = False


class SheetBindingsRequest(BaseModel):
    bindings: list[SheetBindingItem]


class DecisionItem(BaseModel):
    target_type: Literal["ISSUE", "MATERIAL", "FIELD", "RULE", "REPORT_SECTION", "SHEET", "CALC_RULE"]
    target_ref: str
    action: Literal["ACCEPT", "EXCLUDE", "RESOLVE"]
    value: dict[str, Any] = Field(default_factory=dict)


class ImportDecisionsRequest(BaseModel):
    decisions: list[DecisionItem]
    expected_revision: int | None = Field(default=None, ge=0)


class ApplyImportDraftRequest(BaseModel):
    expected_job_revision: int = Field(ge=0)
    expected_scene_revision: int = Field(ge=0)


class PublishRequest(BaseModel):
    """历史发布结果的重复调用请求。"""

    scenario_code: str | None = Field(
        default=None,
        min_length=2,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    scenario_name: str = Field(min_length=2, max_length=255)
    acknowledged_capability_gaps: list[str] = Field(default_factory=list, max_length=500)


class DecisionDraftRequest(BaseModel):
    """集中裁决草稿：按 issue_no 提交当前已作答内容。"""

    answers: dict[str, Any] = Field(default_factory=dict)

