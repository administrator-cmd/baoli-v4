from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool


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


class PlanMaterialItem(BaseModel):
    """方案提交里的一条材料位。"""

    model_config = ConfigDict(extra="allow")

    material_key: str
    name: str


class PlanFieldItem(BaseModel):
    """方案提交里的一个字段。"""

    model_config = ConfigDict(extra="allow")

    field_key: str
    name: str
    value_type: str
    material_key: str


class PlanRuleItem(BaseModel):
    """方案提交里的一条规则；业务定义整体放在 rule_json。"""

    model_config = ConfigDict(extra="allow")

    rule_key: str
    name: str
    node_code: str
    operator_code: str
    rule_json: dict[str, Any]


class PlanSectionItem(BaseModel):
    """方案提交里的一个报告章节。"""

    model_config = ConfigDict(extra="allow")

    section_key: str
    title: str


class PlanCalculationRuleItem(BaseModel):
    """方案提交里的一条计算规则。

    这十五项就是编辑页提交的完整计算定义。缺任何一项都在请求层指出来，
    不能让合法 HTTP 请求在服务里触发 KeyError。
    """

    model_config = ConfigDict(extra="allow")

    rule_code: str
    enabled: StrictBool
    rule_name: str
    formula: Any
    requirement: Any
    result_code: Any
    result_type: Any
    unit: Any
    currency: Any
    period_policy: Any
    statement_scope: Any
    status: Any
    remark: Any
    parameters: list[Any]
    depends_on_result_codes: Any
    business_context: Any
    source_id: Any


class PlanConfigUpdateRequest(BaseModel):
    """整体提交方案的五份配置集合。"""

    model_config = ConfigDict(extra="forbid")

    materials: list[PlanMaterialItem]
    fields: list[PlanFieldItem]
    rules: list[PlanRuleItem]
    sections: list[PlanSectionItem]
    calculation_rules: list[PlanCalculationRuleItem]


class PlanFromTaskRequest(BaseModel):
    task_no: str = Field(min_length=1, max_length=40)
    plan_name: str = Field(min_length=1, max_length=255)


class TaskFromPlanRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)
    expected_scope_hash: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=120)

