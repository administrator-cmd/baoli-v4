"""场景草稿字段配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema
from app.core.database import get_db
from app.modules.scenario.draft.dto import DraftFieldRequest, DraftFieldUpdateRequest
from app.modules.scenario.draft.schema import ScenarioDraftViewResponse
from app.modules.scenario.draft.service import configuration as scenario_configuration
from app.modules.scenario.draft.service.fields import ScenarioDraftFieldsService
from app.modules.scenario.draft.service.responses import ScenarioDraftResponsesService

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/fields", response_model=ResponseSchema[ScenarioDraftViewResponse], status_code=201)
def create_field(scenario_code: str, payload: DraftFieldRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """在草稿中新增字段，返回最新草稿视图。"""
    draft = ScenarioDraftFieldsService(db).create_draft_field( scenario_code, payload.model_dump())
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.put("/{scenario_code}/draft/fields/{field_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def update_field(
    scenario_code: str, field_key: str, payload: DraftFieldUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDraftViewResponse]:
    """修改草稿中的字段（只覆盖请求里出现的字段）。"""
    draft = ScenarioDraftFieldsService(db).update_draft_field( scenario_code, field_key, payload.model_dump(exclude_unset=True))
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.delete("/{scenario_code}/draft/fields/{field_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def delete_field(scenario_code: str, field_key: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """删除草稿中的字段；仍被规则或报告章节引用时明确 409，不级联删除。"""
    draft = ScenarioDraftFieldsService(db).delete_draft_field( scenario_code, field_key)
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))
