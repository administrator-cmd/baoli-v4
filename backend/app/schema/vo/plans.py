from datetime import datetime
from typing import Any

from pydantic import Field

from ...common.api import ApiResponse


class PlanSelectionResponse(ApiResponse):
    plan_code: str
    plan_name: str
    scenario_code: str
    description: str | None = None
    enabled: bool
    deleted: bool
    effective: bool = False
    sync_error: dict[str, Any] | None = None
    scope_hash: str | None = None
    base_scenario_version: dict[str, Any] | None = None
    rules: list[dict[str, Any]] = Field(default_factory=list)
    calculation_rules: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    field_count: int = 0
    updated_at: datetime


class TaskCreatedResponse(ApiResponse):
    task_no: str
    task_name: str
    task_status: str
    scenario_code: str
    scenario_name: str
    scenario_version_no: int
    plan_name: str
    current_node_name: str
    material_count: int
    field_count: int
    reused: bool = False

