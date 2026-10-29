"""场景当前配置与草稿查询接口。"""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.vo.scenarios import ScenarioConfigurationViewResponse, ScenarioDraftViewResponse
from ...services.scenario import configuration as scenario_configuration
from ...services.scenario.configuration import draft_config_view
from ...services.scenario.lookup import template_or_404
from .responses import draft_response

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.get("/{scenario_code}/configuration", response_model=ScenarioConfigurationViewResponse)
def get_scenario_configuration(
    scenario_code: str, request: Request, view: Literal["DRAFT", "PUBLISHED"] | None = None,
    db: Session = Depends(get_db),
) -> ScenarioConfigurationViewResponse:
    """只读当前配置：有草稿就展示草稿，否则展示发布内容。"""
    payload = scenario_configuration.configuration_view(db, template_or_404(db, scenario_code), view)
    return ScenarioConfigurationViewResponse.model_validate({**payload, "request_id": request.state.request_id})


@router.get("/{scenario_code}/draft", response_model=ScenarioDraftViewResponse)
def get_scenario_draft(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """场景配置草稿：材料、字段、规则、报告章节与草稿修订号。"""
    template = template_or_404(db, scenario_code)
    return draft_response(request, draft_config_view(db, template))
