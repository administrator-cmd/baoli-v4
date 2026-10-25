"""场景草稿材料配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.scenarios import DraftMaterialRequest, DraftMaterialUpdateRequest
from ...schema.vo.scenarios import ScenarioDraftViewResponse
from ...services.scenario import configuration as scenario_configuration
from ...services.scenario.materials import create_draft_material, delete_draft_material, update_draft_material
from .responses import _draft_response

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/materials", response_model=ScenarioDraftViewResponse, status_code=201)
def create_material(scenario_code: str, payload: DraftMaterialRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增材料位，返回最新草稿视图。"""
    draft = create_draft_material(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/materials/{material_key}", response_model=ScenarioDraftViewResponse)
def update_material(
    scenario_code: str, material_key: str, payload: DraftMaterialUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的材料位（只覆盖请求里出现的字段）。"""
    draft = update_draft_material(db, scenario_code, material_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/materials/{material_key}", response_model=ScenarioDraftViewResponse)
def delete_material(scenario_code: str, material_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的材料位；仍被字段或规则引用时明确 409，不级联删除。"""
    draft = delete_draft_material(db, scenario_code, material_key)
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))
