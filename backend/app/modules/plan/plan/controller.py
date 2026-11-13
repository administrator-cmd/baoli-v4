"""方案列表与生命周期接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import ItemsResponse, PagedResponse
from app.core.database import get_db
from app.modules.plan.plan.dto import PlanCreateRequest, PlanEnabledRequest, PlanUpdateRequest
from app.modules.plan.plan.schema import PlanOptionItem, PlanRowResponse, PlanSelectionResponse
from app.modules.plan.plan.service.lifecycle import PlanLifecycleService
from app.modules.plan.plan.service.query import PlanQueryService
from app.schema.query.lists import PlanListQueryParam
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/plans", tags=["审核方案"])


@router.get("", response_model=ResponseSchema[PagedResponse[PlanRowResponse]])
def list_plans(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[PlanListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse[PlanRowResponse]]:
    """审核方案分页列表：按名称、场景、启用状态与是否已生效筛选。"""
    result = PlanQueryService(db).list_plans_page(
        
        name=search.name,
        scenario_code=search.scenario_code,
        enabled=search.enabled,
        effective=search.effective,
        deleted=search.deleted,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse[PlanRowResponse].model_validate(result), request)


@router.get("/options", response_model=ResponseSchema[ItemsResponse[PlanOptionItem]])
def plan_options(request: Request, scenario_code: str | None = None, db: Session = Depends(get_db)) -> ResponseSchema[ItemsResponse[PlanOptionItem]]:
    """方案下拉选项，可按场景编码过滤。"""
    return ok(ItemsResponse[PlanOptionItem].model_validate({"items": PlanQueryService(db).list_plan_options( scenario_code=scenario_code)}), request)


@router.post("", response_model=ResponseSchema[PlanSelectionResponse], status_code=201)
def create_plan_endpoint(payload: PlanCreateRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[PlanSelectionResponse]:
    """创建方案：按请求携带的页面预览版本号固定场景版本，并同事务算一次范围。

    版本号与当前可建方案的已发布版本不一致时返回 409 `PLAN_VERSION_CHANGED`，
    不静默改取「当前最新版本」。
    """
    plan = PlanLifecycleService(db).create_plan( payload.model_dump())
    return ok(PlanSelectionResponse.model_validate(PlanQueryService(db).plan_selection_view(plan)), request)


@router.put("/{plan_code}", response_model=ResponseSchema[PlanSelectionResponse])
def update_plan_endpoint(
    plan_code: str, payload: PlanUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[PlanSelectionResponse]:
    """编辑方案基本资料（名称、说明）；不修改规则范围。"""
    plan = PlanLifecycleService(db).update_plan( plan_code, payload.model_dump(exclude_unset=True))
    return ok(PlanSelectionResponse.model_validate(PlanQueryService(db).plan_selection_view(plan)), request)


@router.put("/{plan_code}/enabled", response_model=ResponseSchema[PlanSelectionResponse])
def set_plan_enabled_endpoint(
    plan_code: str, payload: PlanEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[PlanSelectionResponse]:
    """启用 / 停用方案；已停用的方案不能用于创建任务。"""
    plan = PlanLifecycleService(db).set_plan_enabled( plan_code, payload.enabled)
    return ok(PlanSelectionResponse.model_validate(PlanQueryService(db).plan_selection_view(plan)), request)


@router.delete("/{plan_code}", response_model=ResponseSchema[PlanSelectionResponse])
def delete_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[PlanSelectionResponse]:
    """逻辑删除方案；历史任务保留。"""
    plan = PlanLifecycleService(db).delete_plan( plan_code)
    return ok(PlanSelectionResponse.model_validate(PlanQueryService(db).plan_selection_view(plan)), request)


@router.post("/{plan_code}/restore", response_model=ResponseSchema[PlanSelectionResponse])
def restore_plan_endpoint(plan_code: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[PlanSelectionResponse]:
    """恢复已逻辑删除的方案。"""
    plan = PlanLifecycleService(db).restore_plan( plan_code)
    return ok(PlanSelectionResponse.model_validate(PlanQueryService(db).plan_selection_view(plan)), request)
