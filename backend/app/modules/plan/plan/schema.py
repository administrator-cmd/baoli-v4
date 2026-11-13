"""方案响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PlanRowResponse(BaseModel):
    plan_code: str
    plan_name: str
    scenario_code: str
    scenario_name: str
    description: str | None
    enabled: bool
    deleted: bool
    base_scenario_version_no: int | None
    effective: bool
    sync_error: dict[str, Any] | None
    enabled_rule_count: int
    explicit_rule_count: int
    updated_at: datetime


class PlanSelectionResponse(BaseModel):
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


class TaskCreatedResponse(BaseModel):
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


class PlanOptionItem(BaseModel):
    """方案下拉项；scope_hash 供创建任务时做并发保护。"""

    plan_code: str
    plan_name: str
    scenario_code: str
    scenario_version_no: int | None = None
    scope_hash: str | None = None
