"""场景草稿规则配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema
from app.core.database import get_db
from app.modules.scenario.draft.dto import DraftRuleEnabledRequest, DraftRuleRequest
from app.modules.scenario.draft.schema import ScenarioDraftViewResponse
from app.modules.scenario.draft.service import configuration as scenario_configuration
from app.modules.scenario.draft.service.responses import ScenarioDraftResponsesService
from app.modules.scenario.draft.service.rules import ScenarioDraftRulesService

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/rules", response_model=ResponseSchema[ScenarioDraftViewResponse], status_code=201)
def create_rule(scenario_code: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """在草稿中新增规则，返回最新草稿视图。"""
    draft = ScenarioDraftRulesService(db).create_draft_rule( scenario_code, payload.model_dump())
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def update_rule(
    scenario_code: str, rule_key: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDraftViewResponse]:
    """整体替换草稿中这条规则的定义；引用了它的下游规则名记录在操作明细里。"""
    draft = ScenarioDraftRulesService(db).update_draft_rule( scenario_code, rule_key, payload.model_dump(exclude_unset=True))
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}/enabled", response_model=ResponseSchema[ScenarioDraftViewResponse])
def set_rule_enabled(
    scenario_code: str, rule_key: str, payload: DraftRuleEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ScenarioDraftViewResponse]:
    """启用 / 停用草稿中的规则；停用规则不进入发布后的执行范围。"""
    draft = ScenarioDraftRulesService(db).set_draft_rule_enabled( scenario_code, rule_key, payload.enabled)
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))


@router.delete("/{scenario_code}/draft/rules/{rule_key}", response_model=ResponseSchema[ScenarioDraftViewResponse])
def delete_rule(scenario_code: str, rule_key: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ScenarioDraftViewResponse]:
    """删除草稿中的规则。"""
    draft = ScenarioDraftRulesService(db).delete_draft_rule( scenario_code, rule_key)
    return ScenarioDraftResponsesService().draft_response(request, scenario_configuration.ScenarioDraftConfigurationService(db).draft_config_payload( draft))
