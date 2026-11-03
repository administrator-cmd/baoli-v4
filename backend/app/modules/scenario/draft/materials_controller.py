"""场景草稿材料配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema
from app.core.database import get_db
from app.modules.scenario.draft.dto import DraftMaterialRequest, DraftMaterialUpdateRequest
from app.modules.scenario.draft.schema import ScenarioDraftViewResponse
from app.modules.scenario.draft.service import configuration as scenario_configuration
from app.modules.scenario.draft.service.materials import ScenarioDraftMaterialsService
from app.modules.scenario.draft.service.responses import ScenarioDraftResponsesService

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/materials", response_model=ResponseSchema[ScenarioDraftViewResponse], status_code=201)
def create_material(scenario_code: str, payload: DraftMaterialRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """在草稿中新增材料位，返回最新草稿视图。"""
    draft = ScenarioDraftMaterialsService(db).create_draft_material( scenario_code, payload.model_dump())
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.put("/{scenario_code}/draft/materials/{material_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def update_material(
    scenario_code: str, material_key: str, payload: DraftMaterialUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDraftViewResponse]:
    """修改草稿中的材料位（只覆盖请求里出现的字段）。"""
    draft = ScenarioDraftMaterialsService(db).update_draft_material( scenario_code, material_key, payload.model_dump(exclude_unset=True))
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.delete("/{scenario_code}/draft/materials/{material_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def delete_material(scenario_code: str, material_key: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """删除草稿中的材料位；仍被字段或规则引用时明确 409，不级联删除。"""
    draft = ScenarioDraftMaterialsService(db).delete_draft_material( scenario_code, material_key)
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))
