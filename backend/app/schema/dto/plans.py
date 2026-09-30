from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanCreateRequest(BaseModel):
    """新建方案请求，必须钉住页面预览的场景发布版本。"""

    model_config = ConfigDict(extra="forbid")

    plan_name: str = Field(min_length=1, max_length=255)
    scenario_code: str = Field(min_length=1, max_length=80)
    scenario_version_no: int = Field(ge=1)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool = True


class PlanUpdateRequest(BaseModel):
    plan_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class PlanEnabledRequest(BaseModel):
    enabled: bool


class PlanCopyRequest(BaseModel):
    plan_name: str | None = Field(default=None, max_length=255)


class PlanConfigUpdateRequest(BaseModel):
    """整体提交方案的五份配置集合。"""

    model_config = ConfigDict(extra="forbid")

    materials: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    calculation_rules: list[dict[str, Any]]


class PlanFromTaskRequest(BaseModel):
    task_no: str = Field(min_length=1, max_length=40)
    plan_name: str = Field(min_length=1, max_length=255)


class TaskFromPlanRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)
    expected_scope_hash: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=120)

