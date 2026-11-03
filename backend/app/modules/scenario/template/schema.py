"""场景响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.modules.scenario.draft.schema import ScenarioDraftViewResponse


class ScenarioConfigurationViewResponse(ScenarioDraftViewResponse):
    revision_no: int | None


class ScenarioPublishResponse(BaseModel):
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


class ScenarioDetailResponse(BaseModel):
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


class ScenarioOptionItem(BaseModel):
    """业务用户可用的场景下拉项；available 恒为 true。"""

    scenario_code: str
    scenario_name: str
    category: str | None = None
    enabled: bool
    latest_version_no: int
    available: bool


class AdminScenarioOptionItem(BaseModel):
    """管理员下拉项；available 如实标明能否被业务用户用于创建方案。"""

    scenario_code: str
    scenario_name: str
    category: str | None = None
    enabled: bool
    latest_version_no: int
    available: bool
