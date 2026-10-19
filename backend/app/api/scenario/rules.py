"""场景草稿规则配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from ...database import get_db
from ...schema.dto.scenarios import DraftRuleEnabledRequest, DraftRuleRequest
from ...schema.vo.scenarios import ScenarioDraftViewResponse
from ...services.scenario import configuration as scenario_configuration
from ...services.scenario.rules import create_draft_rule, delete_draft_rule, set_draft_rule_enabled, update_draft_rule
from .responses import _draft_response


router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/rules", response_model=ScenarioDraftViewResponse, status_code=201)
def create_rule(scenario_code: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增规则，返回最新草稿视图。"""
    draft = create_draft_rule(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}", response_model=ScenarioDraftViewResponse)
def update_rule(
    scenario_code: str, rule_key: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """整体替换草稿中这条规则的定义；引用了它的下游规则名记录在操作明细里。"""
    draft = update_draft_rule(db, scenario_code, rule_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}/enabled", response_model=ScenarioDraftViewResponse)
def set_rule_enabled(
    scenario_code: str, rule_key: str, payload: DraftRuleEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """启用 / 停用草稿中的规则；停用规则不进入发布后的执行范围。"""
    draft = set_draft_rule_enabled(db, scenario_code, rule_key, payload.enabled)
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/rules/{rule_key}", response_model=ScenarioDraftViewResponse)
def delete_rule(scenario_code: str, rule_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的规则。"""
    draft = delete_draft_rule(db, scenario_code, rule_key)
    return _draft_response(request, scenario_configuration.draft_config_payload(db, draft))
