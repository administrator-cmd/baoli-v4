from typing import Any, Literal

from pydantic import BaseModel, Field

from ...domain.parameter_contract import ReportContentType, ReportResultSort


class ScenarioCreateRequest(BaseModel):
    scenario_name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool = True
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")


class ScenarioUpdateRequest(BaseModel):
    scenario_name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")


class ScenarioEnabledRequest(BaseModel):
    enabled: bool


class ScenarioPublishRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    change_summary: str | None = Field(default=None, max_length=500)
    acknowledged_capability_gaps: list[str] = Field(default_factory=list, max_length=500)


class DraftNodeRequest(BaseModel):
    node_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    node_type: str = Field(min_length=1, max_length=40)
    name: str = Field(default="", max_length=120)
    sequence_no: int | None = Field(default=None, ge=1)
    depends_on: list[str] = Field(default_factory=list)
    config_json: dict[str, Any] | None = None
    enabled: bool = True


class DraftNodeUpdateRequest(BaseModel):
    node_type: str | None = Field(default=None, min_length=1, max_length=40)
    name: str | None = Field(default=None, max_length=120)
    sequence_no: int | None = Field(default=None, ge=1)
    depends_on: list[str] | None = None
    config_json: dict[str, Any] | None = None
    enabled: bool | None = None


class DraftSheetSelection(BaseModel):
    sheet_role: str = Field(min_length=1, max_length=80)
    node_code: str | None = Field(default=None, max_length=80)


class DraftSheetSelectionRequest(BaseModel):
    sheets: list[DraftSheetSelection] = Field(default_factory=list)


class DraftDisabledSourceRowUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    values: dict[str, str]


class DraftSourceActivationPreviewRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    row_keys: list[str] = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=120)


class DraftSourceActivationConfirmRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class DraftMaterialRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    required: bool = True
    description: str = Field(default="", max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftMaterialUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    required: bool | None = None
    description: str | None = Field(default=None, max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftFieldRequest(BaseModel):
    material_key: str = Field(min_length=1, max_length=40)
    node_code: str | None = Field(default=None, min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=255)
    value_type: Literal["STRING", "DECIMAL", "AMOUNT", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"] = "STRING"
    extraction_strategy: Literal["RAW_TEXT", "DETERMINISTIC", "STRUCTURED"] | None = None
    structure_schema: dict[str, Any] | None = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    artifact_source: Literal["SEAL"] | None = None
    period_scope: Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    statement_scope: Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    required: bool = False
    instruction: str = Field(default="", max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftFieldUpdateRequest(BaseModel):
    material_key: str | None = Field(default=None, min_length=1, max_length=40)
    node_code: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    value_type: Literal["STRING", "DECIMAL", "AMOUNT", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"] | None = None
    extraction_strategy: Literal["RAW_TEXT", "DETERMINISTIC", "STRUCTURED"] | None = None
    structure_schema: dict[str, Any] | None = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    artifact_source: Literal["SEAL"] | None = None
    period_scope: Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"] | None = None
    statement_scope: Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"] | None = None
    required: bool | None = None
    instruction: str | None = Field(default=None, max_length=4000)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftRuleInput(BaseModel):
    field_key: str = Field(min_length=1, max_length=40)
    material_key: str = Field(min_length=1, max_length=40)


class DraftRuleApplicability(BaseModel):
    kind: Literal["ALWAYS", "MATERIALS_UPLOADED"] = "ALWAYS"


class DraftRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    node: str = Field(min_length=1, max_length=80)
    operator: str = Field(min_length=1, max_length=40)
    enabled: bool = True
    inputs: list[DraftRuleInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    requirement: str = Field(default="", max_length=4000)
    check_level: str | None = Field(default=None, max_length=40)
    risk_condition: str | None = None
    applicability: DraftRuleApplicability = Field(default_factory=DraftRuleApplicability)
    source_node: str | None = Field(default=None, max_length=40)
    source_rule_key: str | None = Field(default=None, max_length=40)
    source_rule_keys: list[str] = Field(default_factory=list)
    source_rule_codes: list[str] = Field(default_factory=list)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftRuleEnabledRequest(BaseModel):
    enabled: bool


class DraftSectionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content_instruction: str = Field(default="", max_length=4000)
    field_keys: list[str] = Field(default_factory=list)
    report_code: str | None = Field(default=None, max_length=80)
    report_name: str | None = Field(default=None, max_length=255)
    material_keys: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    section_order: int | None = Field(default=None, ge=1)
    content_type: ReportContentType | None = None
    result_sort: ReportResultSort | None = None
    risk_level_order: list[str] = Field(default_factory=list)
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] | None = None


class DraftSectionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content_instruction: str | None = Field(default=None, max_length=4000)
    field_keys: list[str] | None = None
    report_code: str | None = Field(default=None, max_length=80)
    report_name: str | None = Field(default=None, max_length=255)
    material_keys: list[str] | None = None
    formats: list[str] | None = None
    section_order: int | None = Field(default=None, ge=1)
    content_type: ReportContentType | None = None
    result_sort: ReportResultSort | None = None
    risk_level_order: list[str] | None = None
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] | None = None


__all__ = [name for name in globals() if name.startswith("Scenario") or name.startswith("Draft")]
