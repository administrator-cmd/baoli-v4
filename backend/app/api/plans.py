"""审核方案 API：方案 CRUD、配置保存、从方案建任务。

- 方案固定到创建时的已发布场景版本。配置保存会重新编译方案自己的执行范围。
- 修改方案只影响之后新建的任务：任务创建时把方案当前范围抓成自己的执行快照。
- 任务创建是幂等命令。重发返回原任务时，读取该任务自己冻结的版本和范围。
- 不能用任务反向生成方案。
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BusinessError
from ..models import AuditPlan, AuditTask, ScenarioTemplate, ScenarioVersion
from ..schemas import (
    PagedResponse,
    PlanCopyRequest,
    PlanCreateRequest,
    PlanEnabledRequest,
    PlanFromTaskRequest,
    PlanConfigUpdateRequest,
    PlanSelectionResponse,
    PlanUpdateRequest,
    TaskCreatedResponse,
    TaskFromPlanRequest,
)
from ..services import plan_service
from ..domain.identity import public_no
from ..services.audit.interrupt import start_audit_interrupt
from ..services.operation_log_service import OBJECT_PLAN, record_operation, require_idempotent_replay
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
    plan = save_task_range_as_plan(db, payload.model_dump())
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

    成功操作日志只在**任务与审核等待点都成功之后**才写入；等待点初始化失败时写入一条
    `result=FAILED` 的同键记录，重放该标识返回原来的失败原因，不会以 201/reused 冒充成功。
    """
    # 顺序很重要：历史命令优先于一切状态校验。成功创建任务后方案/场景可能已被停用或删除，
    # 此时同幂等标识的重发仍必须返回原任务或原失败，而不是被门禁挡成 409/404。
    request_payload = {
        "plan_code": plan_code,
        "task_name": payload.task_name,
        "remark": payload.remark,
        "expected_scope_hash": payload.expected_scope_hash,
    }
    existing, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_PLAN,
        operation_type="TASK_CREATE",
        idempotency_key=payload.idempotency_key,
        request=request_payload,
    )
    if existing is not None:
        return _replay_task_created(request, db, existing)

    # 到这里才是真正的新命令：此时才校验方案/场景是否可用于新建任务。
    plan = plan_or_404(db, plan_code)
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
    task = create_task_from_plan(
        db,
        plan,
        task_name=payload.task_name,
        remark=payload.remark,
        task_no=task_no,
        expected_scope_hash=payload.expected_scope_hash,
    )
    try:
        # 等待点建立失败会把任务显式标为 FAILED 并提交；此时记一条失败记录，
        # 让同幂等标识的重放返回这次失败，而不是「什么都没发生」地重试成假成功。
        start_audit_interrupt(db, task)
    except BusinessError as failure:
        record_operation(
            db,
            object_type=OBJECT_PLAN,
            object_no=plan_code,
            object_name=plan.plan_name,
            operation_type="TASK_CREATE",
            detail={"task_no": task_no, "plan_code": plan_code},
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            result="FAILED",
            failure_code=failure.code,
            failure_message=failure.message,
        )
        db.commit()
        raise
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="TASK_CREATE",
        detail={
            "task_no": task_no,
            "plan_code": plan_code,
            # 冻结的范围指纹随记录留痕：事后能确认这个任务冻结的是哪一版方案范围。
            "plan_scope_hash": plan.scope_hash,
            "scenario_version_no": task.task_schema_json.get("scenario_version_no"),
        },
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
    )
    db.commit()
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no)) or task
    return _frozen_task_created_response(request, db, task, reused=False)


def _replay_task_created(
    request: Request,
    db: Session,
    log: Any,
) -> TaskCreatedResponse:
    """幂等重放：只读原记录与原任务快照，不读方案当前状态。"""
    if str(log.result or "") != "SUCCEEDED":
        raise BusinessError(
            "TASK_CREATE_PREVIOUSLY_FAILED",
            f"该幂等标识对应的创建任务命令此前执行失败：{log.failure_message or '原因未记录'}。"
            "请更换幂等标识后重新创建",
            status_code=409,
            details={"log_no": log.log_no, "failure_code": log.failure_code},
        )
    detail = log.detail_json if isinstance(log.detail_json, dict) else {}
    task_no = detail.get("task_no")
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no)) if task_no else None
    if task is None:
        raise BusinessError(
            "TASK_CREATE_REPLAY_UNAVAILABLE",
            "该幂等标识对应的任务记录不存在，请更换标识后重新创建",
            status_code=409,
            details={"log_no": log.log_no},
        )
    return _frozen_task_created_response(request, db, task, reused=True)


def _frozen_task_created_response(
    request: Request,
    db: Session,
    task: AuditTask,
    *,
    reused: bool,
) -> TaskCreatedResponse:
    """用任务自己的 task_schema_json 组装响应。

    只读任务冻结的快照：不读方案后来重算过的 scope_json，也不读方案当前的名称与版本。
    历史任务缺少冻结字段时只做**确定性**恢复（场景版本与场景名来自任务所钉的那个版本）；
    连冻结的规则范围都取不到时明确返回 `LEGACY_TASK_SCHEMA_INCOMPLETE`，
    不用「默认全选」或当前方案补一份看起来正常的快照。
    """
    schema = task.task_schema_json if isinstance(task.task_schema_json, dict) else {}
    frozen = db.get(ScenarioVersion, task.scenario_version_id) if task.scenario_version_id else None
    if not isinstance(schema.get("enabled_rule_keys"), list) or not isinstance(schema.get("fields"), list):
        raise BusinessError(
            "LEGACY_TASK_SCHEMA_INCOMPLETE",
            "该历史任务没有冻结的规则范围，无法确认它当时执行的是哪些检查；请从已发布的方案重新创建任务",
            status_code=409,
            details={"task_no": task.task_no},
        )
    version_no = schema.get("scenario_version_no")
    if not isinstance(version_no, int):
        version_no = frozen.version_no if frozen is not None else None
    if not isinstance(version_no, int):
        raise BusinessError(
            "LEGACY_TASK_SCHEMA_INCOMPLETE",
            "该历史任务没有记录冻结的场景版本号，无法确认它的执行依据",
            status_code=409,
            details={"task_no": task.task_no},
        )
    scenario_code = schema.get("scenario_code")
    if not isinstance(scenario_code, str) or not scenario_code:
        scenario_code = frozen.scenario_code if frozen is not None else None
    if not isinstance(scenario_code, str) or not scenario_code:
        raise BusinessError(
            "LEGACY_TASK_SCHEMA_INCOMPLETE",
            "该历史任务没有记录所属场景，无法确认它的执行依据",
            status_code=409,
            details={"task_no": task.task_no},
        )
    scenario_name = schema.get("scenario_name")
    if not isinstance(scenario_name, str) or not scenario_name:
        scenario_name = frozen.scenario_name if frozen is not None else scenario_code
    # 方案名称以任务冻结的快照为准；历史任务没有该键时按它自己关联的方案读取，
    # 都取不到时如实说明为「历史任务配置」，不编一个名字。
    plan_name = schema.get("plan_name")
    if not isinstance(plan_name, str) or not plan_name:
        plan = db.get(AuditPlan, task.plan_id) if task.plan_id else None
        plan_name = plan.plan_name if plan is not None else "历史任务配置"
    material_count = schema.get("material_count")
    if not isinstance(material_count, int):
        materials = schema.get("materials")
        material_count = len(materials) if isinstance(materials, list) else 0
    field_count = schema.get("field_count")
    if not isinstance(field_count, int):
        fields = schema.get("fields")
        field_count = len(fields) if isinstance(fields, list) else 0
    return TaskCreatedResponse(
        request_id=request.state.request_id,
        task_no=task.task_no,
        task_name=task.task_name,
        task_status=task.status,
        scenario_code=scenario_code,
        scenario_name=scenario_name,
        scenario_version_no=version_no,
        plan_name=plan_name,
        current_node_name=task.current_node,
        material_count=material_count,
        field_count=field_count,
        reused=reused,
    )
