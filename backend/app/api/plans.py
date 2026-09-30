"""审核方案 API：方案 CRUD、配置保存、从方案建任务。

- 方案固定到创建时的已发布场景版本。配置保存会重新编译方案自己的执行范围。
- 修改方案只影响之后新建的任务：任务创建时把方案当前范围抓成自己的执行快照。
- 任务创建是幂等命令。重发返回原任务时，读取该任务自己冻结的版本和范围。
- 不能用任务反向生成方案。
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..dto.plans import (
    PlanConfigUpdateRequest,
    PlanCopyRequest,
    PlanCreateRequest,
    PlanEnabledRequest,
    PlanFromTaskRequest,
    PlanUpdateRequest,
    TaskFromPlanRequest,
)
from ..query import validate_page_params
from ..services import plan_service, plan_task_service
from ..services.plan_service import (
    copy_plan,
    create_plan,
    delete_plan,
    list_plan_options,
    list_plans_page,
    plan_adjustments_view,
    plan_config_view,
    plan_or_404,
    restore_plan,
    save_task_range_as_plan,
    set_plan_enabled,
    update_plan,
    update_plan_config,
)
from ..vo import PagedResponse, PlanSelectionResponse, TaskCreatedResponse
from .export_utils import EXPORT_PAGE_SIZE, csv_response

router = APIRouter(prefix="/plans", tags=["审核方案"])


def _selection_response(request: Request, payload: dict[str, Any]) -> PlanSelectionResponse:
    return PlanSelectionResponse.model_validate({**payload, "request_id": request.state.request_id})


# 方案列表与基本资料


@router.get("", response_model=PagedResponse)
def list_plans(
    request: Request,
    name: str | None = None,
    scenario_code: str | None = None,
    enabled: bool | None = None,
    effective: bool | None = None,
    deleted: bool = False,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核方案分页列表：按名称、场景、启用状态与是否已生效筛选。"""
    page, page_size = validate_page_params(page, page_size)
    result = list_plans_page(
        db,
        name=name,
        scenario_code=scenario_code,
        enabled=enabled,
        effective=effective,
        deleted=deleted,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/options")
def plan_options(request: Request, scenario_code: str | None = None, db: Session = Depends(get_db)) -> dict:
    """方案下拉选项，可按场景编码过滤。"""
    return {"request_id": request.state.request_id, "items": list_plan_options(db, scenario_code=scenario_code)}


@router.get("/export")
def export_plans(
    name: str | None = None,
    scenario_code: str | None = None,
    enabled: bool | None = None,
    effective: bool | None = None,
    deleted: bool = False,
    db: Session = Depends(get_db),
):
    """按当前筛选导出方案列表 CSV。"""
    result = list_plans_page(
        db,
        name=name,
        scenario_code=scenario_code,
        enabled=enabled,
        effective=effective,
        deleted=deleted,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["plan_name"],
            item["scenario_name"],
            item["base_scenario_version_no"],
            item["enabled_rule_count"],
            item["explicit_rule_count"],
            "已生效" if item["effective"] else "待核对",
            item["updated_at"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "审核方案列表.csv",
        ["方案名称", "所属场景", "场景版本", "启用规则数", "显式规则数", "范围状态", "更新时间"],
        rows,
    )


@router.post("", response_model=PlanSelectionResponse, status_code=201)
def create_plan_endpoint(payload: PlanCreateRequest, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """创建方案：按请求携带的**页面预览版本号**固定场景版本，并同事务算一次范围。

    版本号与当前可建方案的已发布版本不一致时返回 409 `PLAN_VERSION_CHANGED`，
    不静默改取「当前最新版本」。
    """
    plan = create_plan(db, payload.model_dump())
    return _selection_response(request, plan_service._selection_view(db, plan))



@router.get("/{plan_code}/config")
def get_plan_config(plan_code: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """方案的可编辑配置：材料 / 字段 / 全部规则 / 报告章节 / 计算规则启停。

    方案页各页签的数据源。节点与选用 Sheet 一并返回，但它们是场景级执行结构，
    方案里只读。
    """
    plan = plan_or_404(db, plan_code)
    return {**plan_config_view(db, plan), "request_id": request.state.request_id}


@router.get("/{plan_code}/adjustments")
def get_plan_adjustments(plan_code: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """旧方案相对其来源场景版本的调整，供重新生成方案时参考。"""
    plan = plan_or_404(db, plan_code)
    return {**plan_adjustments_view(db, plan), "request_id": request.state.request_id}


@router.put("/{plan_code}/config")
def put_plan_config(
    plan_code: str, payload: PlanConfigUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """整体替换方案的配置并立即重新编译。

    提交的是五份完整集合，因此「这次提交之后方案配了什么」不由历史操作顺序决定。
    编译不通过时保存照常成功，原因写在方案的 sync_error 里（页面显示「待核对」）。
    """
    result = update_plan_config(db, plan_code, payload.model_dump())
    return {**result, "request_id": request.state.request_id}


@router.post("/from-task", response_model=PlanSelectionResponse, status_code=201)
def plan_from_task(payload: PlanFromTaskRequest, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """拒绝用任务反向生成方案。"""
    plan = save_task_range_as_plan(payload.model_dump())
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.get("/{plan_code}", response_model=PlanSelectionResponse)
def get_plan(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """方案详情：场景版本来源与方案自己的规则启停。"""
    plan = plan_or_404(db, plan_code, include_deleted=True)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.put("/{plan_code}", response_model=PlanSelectionResponse)
def update_plan_endpoint(
    plan_code: str, payload: PlanUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    """编辑方案基本资料（名称、说明）；不修改规则范围。"""
    plan = update_plan(db, plan_code, payload.model_dump(exclude_unset=True))
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.put("/{plan_code}/enabled", response_model=PlanSelectionResponse)
def set_plan_enabled_endpoint(
    plan_code: str, payload: PlanEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    """启用 / 停用方案；已停用的方案不能用于创建任务。"""
    plan = set_plan_enabled(db, plan_code, payload.enabled)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.delete("/{plan_code}", response_model=PlanSelectionResponse)
def delete_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """逻辑删除方案；历史任务保留。"""
    plan = delete_plan(db, plan_code)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.post("/{plan_code}/restore", response_model=PlanSelectionResponse)
def restore_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """恢复已逻辑删除的方案。"""
    plan = restore_plan(db, plan_code)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.post("/{plan_code}/copy", response_model=PlanSelectionResponse, status_code=201)
def copy_plan_endpoint(
    plan_code: str, payload: PlanCopyRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    """复制方案：场景门禁与新建相同，并继续钉住源方案的场景版本。"""
    plan = copy_plan(db, plan_code, payload.model_dump())
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.post("/{plan_code}/tasks", response_model=TaskCreatedResponse, status_code=201)
def create_task_from_plan_endpoint(
    plan_code: str,
    payload: TaskFromPlanRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TaskCreatedResponse:
    """使用方案创建审核任务。

    幂等：同一幂等标识 + 同一请求内容的重发返回原任务；内容不同则明确冲突。
    方案必须启用、范围已生效、场景必须未删除且启用；不满足时明确说明原因。

    请求携带 `expected_scope_hash`（页面预览看到的范围指纹）：与锁定后的实际指纹
    不一致时返回 `PLAN_SCOPE_CHANGED`，任务必须冻结用户刚刚确认的那一版范围。

    命令占位与任务在首次提交时一起落库；等待点确认后追加成功操作日志。
    """
    result = plan_task_service.create_from_plan(db, plan_code, payload.model_dump())
    return TaskCreatedResponse.model_validate({**result, "request_id": request.state.request_id})
