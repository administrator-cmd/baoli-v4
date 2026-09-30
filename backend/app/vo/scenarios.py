from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..common.api import ApiResponse


class DraftDisabledSourceRowActivationPreviewItem(BaseModel):
    row_key: str
    sheet_role: str
    sheet_name: str
    row_no: int
    source_id: str | None = None


class DraftDisabledSourceRowActivationPreviewResponse(ApiResponse):
    revision_no: int
    rows: list[DraftDisabledSourceRowActivationPreviewItem]
    updated_risk_codes: list[str]


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
    sheets: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    disabled_source_rows: list[dict[str, Any]] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    business_rule_count: int = 0
    system_rule_count: int = 0


class ScenarioPublishResponse(ApiResponse):
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    source: str
    change_summary: str | None = None
    manifest_hash: str
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
    deleted: bool
    latest_version_no: int
    created_at: datetime
    updated_at: datetime
    plan_count: int = 0
    draft: dict[str, Any] | None = None


__all__ = [name for name in globals() if name.endswith("Response") or name.endswith("Item")]
