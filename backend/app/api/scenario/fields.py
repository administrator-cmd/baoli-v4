"""场景草稿字段配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from ...database import get_db
from ...schema.dto.scenarios import DraftFieldRequest, DraftFieldUpdateRequest
from ...schema.vo.scenarios import ScenarioDraftViewResponse
from ...services.scenario import configuration as scenario_configuration
from ...services.scenario.fields import create_draft_field, delete_draft_field, update_draft_field
from .responses import _draft_response


router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/fields", response_model=ScenarioDraftViewResponse, status_code=201)
def create_field(scenario_code: str, payload: DraftFieldRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增字段，返回最新草稿视图。"""
    draft = create_draft_field(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/fields/{field_key}", response_model=ScenarioDraftViewResponse)
def update_field(
    scenario_code: str, field_key: str, payload: DraftFieldUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的字段（只覆盖请求里出现的字段）。"""
    draft = update_draft_field(db, scenario_code, field_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/fields/{field_key}", response_model=ScenarioDraftViewResponse)
def delete_field(scenario_code: str, field_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的字段；仍被规则或报告章节引用时明确 409，不级联删除。"""
    draft = delete_draft_field(db, scenario_code, field_key)
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))
