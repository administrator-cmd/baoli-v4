"""场景草稿报告章节配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema
from app.core.database import get_db
from app.modules.scenario.draft.dto import DraftSectionRequest, DraftSectionUpdateRequest
from app.modules.scenario.draft.schema import ScenarioDraftViewResponse
from app.modules.scenario.draft.service import configuration as scenario_configuration
from app.modules.scenario.draft.service.responses import ScenarioDraftResponsesService
from app.modules.scenario.draft.service.sections import ScenarioDraftSectionsService

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/sections", response_model=ResponseSchema[ScenarioDraftViewResponse], status_code=201)
def create_section(scenario_code: str, payload: DraftSectionRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """在草稿中新增报告章节。"""
    draft = ScenarioDraftSectionsService(db).create_draft_section( scenario_code, payload.model_dump())
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.put("/{scenario_code}/draft/sections/{section_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def update_section(
    scenario_code: str, section_key: str, payload: DraftSectionUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDraftViewResponse]:
    """修改草稿中的报告章节。"""
    draft = ScenarioDraftSectionsService(db).update_draft_section( scenario_code, section_key, payload.model_dump(exclude_unset=True))
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.delete("/{scenario_code}/draft/sections/{section_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def delete_section(scenario_code: str, section_key: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """删除草稿中的报告章节。"""
    draft = ScenarioDraftSectionsService(db).delete_draft_section( scenario_code, section_key)
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))
