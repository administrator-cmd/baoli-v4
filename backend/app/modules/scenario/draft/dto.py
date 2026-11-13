"""场景请求模型。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.constants.execution import NODE_RULE_EXECUTION, OPERATOR_LLM_EXECUTE
from app.domain.configuration.execution_mode import ExecutionMode
from app.domain.configuration.parameter_contract import ReportContentType, ReportResultSort


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
    period_scope: str = "NOT_APPLICABLE"
    statement_scope: str = "NOT_APPLICABLE"
    entity_scope: str = "SELF"
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
    period_scope: str | None = None
    statement_scope: str | None = None
    entity_scope: str | None = None
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
    node: str = NODE_RULE_EXECUTION
    operator: str = OPERATOR_LLM_EXECUTE
    execution_mode: ExecutionMode = "UNKNOWN"
    rule_role: Literal["CHECK", "RISK"] = "CHECK"
    enabled: bool = True
    inputs: list[DraftRuleInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    requirement: str = Field(default="", max_length=4000)
    check_level: str | None = Field(default=None, max_length=40)
    risk_condition: str | None = None
    applicability: DraftRuleApplicability = Field(default_factory=DraftRuleApplicability)
    source_rule_key: str | None = Field(default=None, max_length=40)
    source_rule_keys: list[str] = Field(default_factory=list)
    source_rule_codes: list[str] = Field(default_factory=list)
    source_ref: str | None = Field(default=None, max_length=80)


class DraftRuleEnabledRequest(BaseModel):
    enabled: bool


class DraftCalculationParameter(BaseModel):
    model_config = ConfigDict(extra="allow")

    parameter_code: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_code: str | None = None
    constant_value: Any = None


class DraftCalculationRequest(BaseModel):
    rule_code: str = Field(min_length=1, max_length=80)
    rule_name: str = Field(min_length=1, max_length=255)
    result_code: str = Field(min_length=1, max_length=80)
    source_id: str | None = None
    formula: str = ""
    requirement: str = ""
    result_type: str
    unit: str | None = None
    currency: str | None = None
    period_policy: str | None = None
    statement_scope: str | None = None
    status: Literal["CLEAR", "PENDING", "DISABLED"]
    parameters: list[DraftCalculationParameter] = Field(default_factory=list)
    depends_on_result_codes: list[str] = Field(default_factory=list)
    business_context: dict[str, Any] = Field(default_factory=dict)
    remark: str = ""


class DraftSectionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content_instruction: str = Field(default="", max_length=4000)
    field_keys: list[str] = Field(default_factory=list)
    data_refs: list[str] = Field(default_factory=list)
    report_code: str | None = Field(default=None, max_length=80)
    report_name: str | None = Field(default=None, max_length=255)
    material_keys: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    section_order: int | None = None
    content_type: ReportContentType | None = None
    result_sort: ReportResultSort | None = None
    risk_level_order: list[str] = Field(default_factory=list)
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] | None = None


class DraftSectionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content_instruction: str | None = Field(default=None, max_length=4000)
    field_keys: list[str] | None = None
    data_refs: list[str] | None = None
    report_code: str | None = Field(default=None, max_length=80)
    report_name: str | None = Field(default=None, max_length=255)
    material_keys: list[str] | None = None
    formats: list[str] | None = None
    section_order: int | None = None
    content_type: ReportContentType | None = None
    result_sort: ReportResultSort | None = None
    risk_level_order: list[str] | None = None
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] | None = None
