"""场景当前配置与草稿查询接口。"""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.scenario.draft.schema import ScenarioDraftViewResponse
from app.modules.scenario.draft.service import configuration as scenario_configuration
from app.modules.scenario.draft.service.configuration import ScenarioDraftConfigurationService
from app.modules.scenario.draft.service.responses import ScenarioDraftResponsesService
from app.modules.scenario.template.schema import ScenarioConfigurationViewResponse
from app.modules.scenario.template.service.lookup import ScenarioTemplateLookupService

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.get("/{scenario_code}/configuration", response_model=ResponseSchema[ScenarioConfigurationViewResponse])
def get_scenario_configuration(
    scenario_code: str, request: Request, view: Literal["DRAFT", "PUBLISHED"] | None = None,
    db: Session = Depends(get_db),
) -> ResponseSchema[ScenarioConfigurationViewResponse]:
    """只读当前配置：有草稿就展示草稿，否则展示发布内容。"""
    payload = scenario_configuration.ScenarioDraftConfigurationService(db).configuration_view( ScenarioTemplateLookupService(db).template_or_404( scenario_code), view)
    return ok(ScenarioConfigurationViewResponse.model_validate(payload), request)


@router.get("/{scenario_code}/draft", response_model=ResponseSchema[ScenarioDraftViewResponse])
def get_scenario_draft(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """场景配置草稿：材料、字段、规则、报告章节与草稿修订号。"""
    template = ScenarioTemplateLookupService(db).template_or_404( scenario_code)
    return ScenarioDraftResponsesService().draft_response(request, ScenarioDraftConfigurationService(db).draft_config_view( template))
