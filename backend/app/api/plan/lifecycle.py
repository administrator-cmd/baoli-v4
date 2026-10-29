"""方案列表与生命周期接口。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.plans import PlanCreateRequest, PlanEnabledRequest, PlanUpdateRequest
from ...schema.query.pagination import validate_page_params
from ...schema.vo.common import PagedResponse
from ...schema.vo.plans import PlanSelectionResponse
from ...services.plan.lifecycle import create_plan, delete_plan, restore_plan, set_plan_enabled, update_plan
from ...services.plan.query import list_plan_options, list_plans_page, plan_selection_view
from .configuration import selection_response

router = APIRouter(prefix="/plans", tags=["审核方案"])


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


@router.post("", response_model=PlanSelectionResponse, status_code=201)
def create_plan_endpoint(payload: PlanCreateRequest, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """创建方案：按请求携带的页面预览版本号固定场景版本，并同事务算一次范围。

    版本号与当前可建方案的已发布版本不一致时返回 409 `PLAN_VERSION_CHANGED`，
    不静默改取「当前最新版本」。
    """
    plan = create_plan(db, payload.model_dump())
    return selection_response(request, plan_selection_view(db, plan))


@router.put("/{plan_code}", response_model=PlanSelectionResponse)
def update_plan_endpoint(
    plan_code: str, payload: PlanUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    """编辑方案基本资料（名称、说明）；不修改规则范围。"""
    plan = update_plan(db, plan_code, payload.model_dump(exclude_unset=True))
    return selection_response(request, plan_selection_view(db, plan))


@router.put("/{plan_code}/enabled", response_model=PlanSelectionResponse)
def set_plan_enabled_endpoint(
    plan_code: str, payload: PlanEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> PlanSelectionResponse:
    """启用 / 停用方案；已停用的方案不能用于创建任务。"""
    plan = set_plan_enabled(db, plan_code, payload.enabled)
    return selection_response(request, plan_selection_view(db, plan))


@router.delete("/{plan_code}", response_model=PlanSelectionResponse)
def delete_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """逻辑删除方案；历史任务保留。"""
    plan = delete_plan(db, plan_code)
    return selection_response(request, plan_selection_view(db, plan))


@router.post("/{plan_code}/restore", response_model=PlanSelectionResponse)
def restore_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """恢复已逻辑删除的方案。"""
    plan = restore_plan(db, plan_code)
    return selection_response(request, plan_selection_view(db, plan))
