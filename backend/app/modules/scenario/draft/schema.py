"""场景响应模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ScenarioDraftViewResponse(BaseModel):
    configuration_kind: Literal["DRAFT", "PUBLISHED"] = "DRAFT"
    version_no: int | None = None
    scenario_code: str
    schema_key: str | None = None
    schema_version: str | None = None
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")
    revision_no: int
    source: str
    source_label: str
    base_version_no: int | None = None
    profile_version: str | None = None
    sync_error: dict[str, Any] | None = None
    updated_at: datetime
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    business_rule_count: int = 0
    system_rule_count: int = 0


class ScenarioCalculationMutationResponse(BaseModel):
    scenario_code: str
    revision_no: int
