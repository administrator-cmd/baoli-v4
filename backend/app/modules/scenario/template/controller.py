"""场景列表、详情与生命周期接口。"""


from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import ItemsResponse, PagedResponse
from app.core.database import get_db
from app.modules.scenario.template.dto import ScenarioCreateRequest, ScenarioEnabledRequest, ScenarioUpdateRequest
from app.modules.scenario.template.schema import (
    AdminScenarioOptionItem,
    ScenarioDetailResponse,
    ScenarioOptionItem,
    ScenarioRowResponse,
)
from app.modules.scenario.template.service.lifecycle import ScenarioTemplateLifecycleService
from app.modules.scenario.template.service.query import ScenarioTemplateQueryService
from app.schema.query.lists import ScenarioListQueryParam
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.get("", response_model=ResponseSchema[PagedResponse[ScenarioRowResponse]])
def list_scenarios(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[ScenarioListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse[ScenarioRowResponse]]:
    """场景分页列表：按名称、业务类型、启用状态与发布时间筛选。"""
    result = ScenarioTemplateQueryService(db).list_scenarios_page(
        
        name=search.name,
        category=search.category,
        enabled=search.enabled,
        published_from=search.published_from,
        published_to=search.published_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse[ScenarioRowResponse].model_validate(result), request)


@router.get("/options", response_model=ResponseSchema[ItemsResponse[ScenarioOptionItem]])
def scenario_options(request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ItemsResponse[ScenarioOptionItem]]:
    """业务用户可用的场景下拉：只含已启用且已有已发布版本的场景。

    方案与任务的场景选择器用它。草稿、未发布、停用或已删除的场景不出现——
    业务用户只能从已发布且启用的场景创建方案。
    """
    return ok(ItemsResponse[ScenarioOptionItem].model_validate({"items": ScenarioTemplateQueryService(db).list_scenario_options()}), request)


@router.get("/admin-options", response_model=ResponseSchema[ItemsResponse[AdminScenarioOptionItem]])
def admin_scenario_options(request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ItemsResponse[AdminScenarioOptionItem]]:
    """管理员用的场景下拉：未删除的全部场景及其实时状态。

    导入页与场景管理页在场景发布之前就要选中它（先建场景、再上传规则库），
    因此不能按「已发布」过滤。`available` 如实标出该场景当前能否被业务用户使用。
    """
    return ok(ItemsResponse[AdminScenarioOptionItem].model_validate({"items": ScenarioTemplateQueryService(db).list_admin_scenario_options()}), request)


@router.post("", response_model=ResponseSchema[ScenarioDetailResponse], status_code=201)
def create_scenario_endpoint(payload: ScenarioCreateRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDetailResponse]:
    """新建场景（页面配置）：没有 Excel 作业与工作表行号，不伪造导入来源。"""
    template = ScenarioTemplateLifecycleService(db).create_scenario( payload.model_dump(by_alias=True))
    return ok(ScenarioDetailResponse.model_validate(ScenarioTemplateQueryService(db).scenario_detail( template.scenario_code)), request)


@router.get("/{scenario_code}", response_model=ResponseSchema[ScenarioDetailResponse])
def get_scenario(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDetailResponse]:
    """场景详情：当前发布版本与配置概览。"""
    return ok(ScenarioDetailResponse.model_validate(ScenarioTemplateQueryService(db).scenario_detail( scenario_code)), request)


@router.put("/{scenario_code}", response_model=ResponseSchema[ScenarioDetailResponse])
def update_scenario_endpoint(
    scenario_code: str, payload: ScenarioUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDetailResponse]:
    """编辑场景基本资料（名称、业务类型、说明）。"""
    ScenarioTemplateLifecycleService(db).update_scenario( scenario_code, payload.model_dump(exclude_unset=True, by_alias=True))
    return ok(ScenarioDetailResponse.model_validate(ScenarioTemplateQueryService(db).scenario_detail( scenario_code)), request)


@router.put("/{scenario_code}/enabled", response_model=ResponseSchema[ScenarioDetailResponse])
def set_scenario_enabled_endpoint(
    scenario_code: str, payload: ScenarioEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDetailResponse]:
    """启用 / 停用场景；已停用的场景不能用于创建任务。"""
    ScenarioTemplateLifecycleService(db).set_scenario_enabled( scenario_code, payload.enabled)
    return ok(ScenarioDetailResponse.model_validate(ScenarioTemplateQueryService(db).scenario_detail( scenario_code)), request)


@router.delete("/{scenario_code}", status_code=204)
def delete_scenario_endpoint(scenario_code: str, db: Session = Depends(get_db)) -> Response:
    """实际删除场景和草稿，成功后无响应正文。"""
    ScenarioTemplateLifecycleService(db).delete_scenario( scenario_code)
    return Response(status_code=204)
