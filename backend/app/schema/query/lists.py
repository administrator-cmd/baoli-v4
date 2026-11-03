"""管理端列表的筛选参数；分页单独用 PaginationQueryParam。

每个模型对应一个列表接口，字段名与查询字符串一致，前端无需改动。
"""

from pydantic import BaseModel

from app.schema.query.timestamps import FilterDateTime


class ScenarioListQueryParam(BaseModel):
    name: str | None = None
    category: str | None = None
    enabled: bool | None = None
    published_from: FilterDateTime | None = None
    published_to: FilterDateTime | None = None


class PlanListQueryParam(BaseModel):
    name: str | None = None
    scenario_code: str | None = None
    enabled: bool | None = None
    effective: bool | None = None
    deleted: bool = False


class TaskListQueryParam(BaseModel):
    name: str | None = None
    scenario_code: str | None = None
    plan_code: str | None = None
    status: str | None = None
    current_node: str | None = None
    created_from: FilterDateTime | None = None
    created_to: FilterDateTime | None = None


class ExecutionListQueryParam(BaseModel):
    task_no: str | None = None
    node_code: str | None = None
    status: str | None = None
    started_from: FilterDateTime | None = None
    started_to: FilterDateTime | None = None


class ImportJobListQueryParam(BaseModel):
    scenario_code: str | None = None
    filename: str | None = None
    status: str | None = None
    created_from: FilterDateTime | None = None
    created_to: FilterDateTime | None = None


class DocumentListQueryParam(BaseModel):
    task_no: str | None = None
    task_name: str | None = None
    keyword: str | None = None
    material_name: str | None = None
    has_file: bool | None = None


class ReportListQueryParam(BaseModel):
    name: str | None = None
    task_no: str | None = None
    task_name: str | None = None
    scenario_code: str | None = None
    plan_code: str | None = None
    archived: bool | None = None
    generated_from: FilterDateTime | None = None
    generated_to: FilterDateTime | None = None


class RiskResultListQueryParam(BaseModel):
    task_no: str | None = None
    task_name: str | None = None
    scenario_code: str | None = None
    plan_code: str | None = None
    risk_level: str | None = None
    status: str | None = None


class OperationLogListQueryParam(BaseModel):
    object_type: str | None = None
    operation_type: str | None = None
    result: str | None = None
    keyword: str | None = None
    created_from: FilterDateTime | None = None
    created_to: FilterDateTime | None = None
