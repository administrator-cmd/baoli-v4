"""场景列表、详情与生命周期接口。"""


from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.scenarios import ScenarioCreateRequest, ScenarioEnabledRequest, ScenarioUpdateRequest
from ...schema.query.pagination import validate_page_params
from ...schema.query.timestamps import FilterDateTime
from ...schema.vo.common import PagedResponse
from ...schema.vo.scenarios import ScenarioDetailResponse
from ...services.scenario.lifecycle import create_scenario, delete_scenario, set_scenario_enabled, update_scenario
from ...services.scenario.query import (
    list_admin_scenario_options,
    list_scenario_options,
    list_scenarios_page,
    scenario_detail,
)

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.get("", response_model=PagedResponse)
def list_scenarios(
    request: Request,
    name: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
    published_from: FilterDateTime | None = None,
    published_to: FilterDateTime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """场景分页列表：按名称、业务类型、启用状态与发布时间筛选。"""
    page, page_size = validate_page_params(page, page_size)
    result = list_scenarios_page(
        db,
        name=name,
        category=category,
        enabled=enabled,
        published_from=published_from,
        published_to=published_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/options")
def scenario_options(request: Request, db: Session = Depends(get_db)) -> dict:
    """业务用户可用的场景下拉：只含已启用且已有已发布版本的场景。

    方案与任务的场景选择器用它。草稿、未发布、停用或已删除的场景不出现——
    业务用户只能从已发布且启用的场景创建方案。
    """
    return {"request_id": request.state.request_id, "items": list_scenario_options(db)}


@router.get("/admin-options")
def admin_scenario_options(request: Request, db: Session = Depends(get_db)) -> dict:
    """管理员用的场景下拉：未删除的全部场景及其实时状态。

    导入页与场景管理页在场景发布之前就要选中它（先建场景、再上传规则库），
    因此不能按「已发布」过滤。`available` 如实标出该场景当前能否被业务用户使用。
    """
    return {"request_id": request.state.request_id, "items": list_admin_scenario_options(db)}


@router.post("", response_model=ScenarioDetailResponse, status_code=201)
def create_scenario_endpoint(payload: ScenarioCreateRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    """新建场景（页面配置）：没有 Excel 作业与工作表行号，不伪造导入来源。"""
    template = create_scenario(db, payload.model_dump(by_alias=True))
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, template.scenario_code), "request_id": request.state.request_id})


@router.get("/{scenario_code}", response_model=ScenarioDetailResponse)
def get_scenario(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    """场景详情：当前发布版本与配置概览。"""
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.put("/{scenario_code}", response_model=ScenarioDetailResponse)
def update_scenario_endpoint(
    scenario_code: str, payload: ScenarioUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    """编辑场景基本资料（名称、业务类型、说明）。"""
    update_scenario(db, scenario_code, payload.model_dump(exclude_unset=True, by_alias=True))
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.put("/{scenario_code}/enabled", response_model=ScenarioDetailResponse)
def set_scenario_enabled_endpoint(
    scenario_code: str, payload: ScenarioEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    """启用 / 停用场景；已停用的场景不能用于创建任务。"""
    set_scenario_enabled(db, scenario_code, payload.enabled)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.delete("/{scenario_code}", status_code=204)
def delete_scenario_endpoint(scenario_code: str, db: Session = Depends(get_db)) -> Response:
    """实际删除场景和草稿，成功后无响应正文。"""
    delete_scenario(db, scenario_code)
    return Response(status_code=204)
