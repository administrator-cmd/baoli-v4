from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ...common.api import ApiResponse


class ScenarioVersionResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    schema_key: str | None = None
    schema_version: str | None = None
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")
    manifest: dict[str, Any]
    published_at: datetime


class ScenarioDraftViewResponse(ApiResponse):
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


class ScenarioConfigurationViewResponse(ScenarioDraftViewResponse):
    revision_no: int | None


class ScenarioPublishResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    source: str
    change_summary: str | None = None
    material_count: int
    field_count: int
    rule_count: int
    published_at: datetime
    reused: bool = False


class ScenarioDetailResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    schema_key: str | None = None
    schema_version: str | None = None
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")
    category: str | None = None
    description: str | None = None
    enabled: bool
    latest_version_no: int
    created_at: datetime
    updated_at: datetime
    plan_count: int = 0
    draft: dict[str, Any] | None = None


__all__ = [name for name in globals() if name.endswith("Response") or name.endswith("Item")]
