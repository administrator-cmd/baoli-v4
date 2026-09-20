"""审核方案 API：方案 CRUD、规则范围选择、从方案建任务。

- 方案是对某个场景版本的可复用审核范围，保存即生效；没有草稿/发布与版本概念。
- 修改方案只影响之后新建的任务：任务创建时把方案当前范围抓成自己的执行快照。
- 任务创建是幂等命令，响应丢失后用同一幂等标识重发返回原任务，不重复创建。
- 旧任务可显式另存为方案；不迁移、不改写历史结果。
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BusinessError
from ..models import AuditTask, ScenarioTemplate, ScenarioVersion
from ..schemas import (
    PagedResponse,
    PlanCopyRequest,
    PlanCreateRequest,
    PlanEnabledRequest,
    PlanFromTaskRequest,
    PlanSaveRequest,
    PlanScopePreviewRequest,
    PlanSelectionResponse,
    PlanUpdateRequest,
    TaskCreatedResponse,
    TaskFromPlanRequest,
)
from ..services import plan_service
from ..domain.identity import public_no
from ..services.operation_log_service import OBJECT_PLAN, record_operation, require_idempotent_replay
from ..services.plan_service import (
    copy_plan,
    create_plan,
    delete_plan,
    list_plan_options,
    list_plans_page,
    plan_or_404,
    restore_plan,
    save_plan,
    save_task_range_as_plan,
    scope_preview,
    set_plan_enabled,
    update_plan,
)
from ..services.task_service import create_task_from_plan
from .export_utils import EXPORT_PAGE_SIZE, csv_response

router = APIRouter(prefix="/plans", tags=["审核方案"])


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size


def _selection_response(request: Request, payload: dict[str, Any]) -> PlanSelectionResponse:
    return PlanSelectionResponse.model_validate({**payload, "request_id": request.state.request_id})


# ---------------------------------------------------------------------------
# 方案列表与基本资料
# ---------------------------------------------------------------------------


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
    page, page_size = _page_params(page, page_size)
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
    plan = create_plan(db, payload.model_dump())
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.post("/scope-preview")
def plan_scope_preview(payload: PlanScopePreviewRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """方案编辑的范围预览：显式选择 + 自动补入 + 未启用 + 材料与字段摘要。"""
    result = scope_preview(
        db,
        scenario_code=payload.scenario_code,
        version_no=payload.version_no,
        rule_keys=payload.rule_keys,
    )
    return {**result, "request_id": request.state.request_id}


@router.post("/from-task", response_model=PlanSelectionResponse, status_code=201)
def plan_from_task(payload: PlanFromTaskRequest, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """把旧任务的执行范围显式另存为方案（用户主动动作，不迁移原任务）。"""
    plan = save_task_range_as_plan(db, payload.model_dump())
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.get("/{plan_code}", response_model=PlanSelectionResponse)
def get_plan(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    plan = plan_or_404(db, plan_code, include_deleted=True)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.put("/{plan_code}", response_model=PlanSelectionResponse)
def update_plan_endpoint(
    plan_code: str, payload: PlanUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    plan = update_plan(db, plan_code, payload.model_dump(exclude_unset=True))
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.put("/{plan_code}/enabled", response_model=PlanSelectionResponse)
def set_plan_enabled_endpoint(
    plan_code: str, payload: PlanEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    plan = set_plan_enabled(db, plan_code, payload.enabled)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.delete("/{plan_code}", response_model=PlanSelectionResponse)
def delete_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    plan = delete_plan(db, plan_code)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.post("/{plan_code}/restore", response_model=PlanSelectionResponse)
def restore_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    plan = restore_plan(db, plan_code)
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.post("/{plan_code}/copy", response_model=PlanSelectionResponse, status_code=201)
def copy_plan_endpoint(
    plan_code: str, payload: PlanCopyRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    plan = copy_plan(db, plan_code, payload.model_dump())
    return _selection_response(request, plan_service._selection_view(db, plan))


@router.put("/{plan_code}/save", response_model=PlanSelectionResponse)
def save_plan_endpoint(
    plan_code: str, payload: PlanSaveRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    """保存方案（场景版本绑定 + 显式规则选择）并立即重算范围；切换版本时返回被移除的旧选择。"""
    view = save_plan(db, plan_code, payload.model_dump())
    return _selection_response(request, view)


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
    """
    # 顺序很重要：历史命令优先于状态校验。成功但响应丢失后，方案/场景可能已被停用或删除，
    # 此时同幂等标识的重发仍必须返回原任务，而不是被门禁挡成 409/404。
    plan = plan_or_404(db, plan_code)
    request_payload = {
        "plan_code": plan_code,
        "task_name": payload.task_name,
        "remark": payload.remark,
    }
    existing, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_PLAN,
        operation_type="TASK_CREATE",
        idempotency_key=payload.idempotency_key,
        request=request_payload,
    )
    scenario = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
    if existing is not None:
        detail = existing.detail_json if isinstance(existing.detail_json, dict) else {}
        task = db.scalar(select(AuditTask).where(AuditTask.task_no == detail.get("task_no")))
        if task is None:
            raise BusinessError(
                "TASK_CREATE_REPLAY_UNAVAILABLE",
                "该幂等标识对应的任务记录不存在，请更换标识后重新创建",
                status_code=409,
                details={"log_no": existing.log_no},
            )
        scope = plan.scope_json if isinstance(plan.scope_json, dict) else {}
        return TaskCreatedResponse(
            request_id=request.state.request_id,
            task_no=task.task_no,
            task_name=task.task_name,
            task_status=task.status,
            scenario_code=plan.scenario_code,
            scenario_name=scenario.scenario_name if scenario else plan.scenario_code,
            scenario_version_no=scenario.version_no if scenario else 0,
            plan_name=plan.plan_name,
            current_node_name=task.current_node,
            material_count=scope.get("material_count", 0),
            field_count=scope.get("field_count", 0),
            reused=True,
        )

    # 到这里才是真正的新命令：此时才校验方案/场景是否可用于新建任务。
    scenario_template = db.scalar(
        select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == plan.scenario_code)
    )
    if scenario_template is None or scenario_template.deleted_at is not None:
        raise BusinessError("SCENARIO_NOT_FOUND", "方案所属场景不存在或已被删除", status_code=409, details={"scenario_code": plan.scenario_code})
    if not plan.enabled:
        raise BusinessError("PLAN_DISABLED", "该方案已停用，不能用于创建任务", status_code=409, details={"plan_code": plan_code})
    if not scenario_template.enabled:
        raise BusinessError("SCENARIO_DISABLED", "该场景已停用，不能用于创建任务", status_code=409, details={"scenario_code": plan.scenario_code})

    # 命令记录与任务在同一次提交中对账：任务编号预生成并写进记录，
    # 等待点建立时的提交会把两者一起落库。
    task_no = public_no("TASK")
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="TASK_CREATE",
        detail={"task_no": task_no, "plan_code": plan_code},
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
    )
    db.flush()
    task = create_task_from_plan(db, plan, task_name=payload.task_name, remark=payload.remark, task_no=task_no)
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no)) or task
    scope = plan.scope_json if isinstance(plan.scope_json, dict) else {}
    return TaskCreatedResponse(
        request_id=request.state.request_id,
        task_no=task.task_no,
        task_name=task.task_name,
        task_status=task.status,
        scenario_code=plan.scenario_code,
        scenario_name=scenario.scenario_name if scenario else plan.scenario_code,
        scenario_version_no=scenario.version_no if scenario else 0,
        plan_name=plan.plan_name,
        current_node_name=task.current_node,
        material_count=scope.get("material_count", 0),
        field_count=scope.get("field_count", 0),
        reused=False,
    )



