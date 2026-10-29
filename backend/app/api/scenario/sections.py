"""场景草稿报告章节配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.scenarios import DraftSectionRequest, DraftSectionUpdateRequest
from ...schema.vo.scenarios import ScenarioDraftViewResponse
from ...services.scenario import configuration as scenario_configuration
from ...services.scenario.sections import create_draft_section, delete_draft_section, update_draft_section
from .responses import draft_response

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/sections", response_model=ScenarioDraftViewResponse, status_code=201)
def create_section(scenario_code: str, payload: DraftSectionRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增报告章节。"""
    draft = create_draft_section(db, scenario_code, payload.model_dump())
    return draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/sections/{section_key}", response_model=ScenarioDraftViewResponse)
def update_section(
    scenario_code: str, section_key: str, payload: DraftSectionUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的报告章节。"""
    draft = update_draft_section(db, scenario_code, section_key, payload.model_dump(exclude_unset=True))
    return draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/sections/{section_key}", response_model=ScenarioDraftViewResponse)
def delete_section(scenario_code: str, section_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的报告章节。"""
    draft = delete_draft_section(db, scenario_code, section_key)
    return draft_response(request, scenario_configuration.draft_config_payload(db, draft))
