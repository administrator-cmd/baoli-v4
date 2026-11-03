"""方案响应模型。"""

from typing import Any

from pydantic import BaseModel, Field


class PlanConfigResponse(BaseModel):
    """方案可编辑配置；材料/字段/规则/章节与计算定义的结构由配置模型决定。"""

    plan_code: str
    plan_name: str
    scenario_code: str
    description: str | None = None
    enabled: bool
    deleted: bool
    effective: bool
    sync_error: dict[str, Any] | None = None
    scope_hash: str | None = None
    base_scenario_version: dict[str, Any] | None = None
    materials: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    calculation_rules: list[dict[str, Any]] = Field(default_factory=list)
