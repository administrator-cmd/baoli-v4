"""方案请求模型。"""


from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanCreateRequest(BaseModel):
    """新建方案请求，必须钉住页面预览的场景发布版本。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=120)
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


class PlanFromTaskRequest(BaseModel):
    task_no: str = Field(min_length=1, max_length=40)
    plan_name: str = Field(min_length=1, max_length=255)


class TaskFromPlanRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)
    expected_scope_hash: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=120)
    report_generation_mode: Literal["RULES", "TEMPLATE"] = "RULES"
    report_template_no: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def validate_report_template(self):
        if self.report_generation_mode == "TEMPLATE" and not self.report_template_no:
            raise ValueError("仿照模板生成时必须选择报告模板")
        if self.report_generation_mode == "RULES" and self.report_template_no:
            raise ValueError("按规则生成时不能同时指定报告模板")
        return self
