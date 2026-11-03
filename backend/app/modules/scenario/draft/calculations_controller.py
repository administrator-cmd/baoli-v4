"""场景草稿计算配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.scenario.draft.dto import DraftCalculationRequest
from app.modules.scenario.draft.schema import ScenarioCalculationMutationResponse
from app.modules.scenario.draft.service.calculations import ScenarioDraftCalculationsService

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/calculations", response_model=ResponseSchema[ScenarioCalculationMutationResponse], status_code=201)
def create_calculation(
    scenario_code: str, payload: DraftCalculationRequest, request: Request, db: Session = Depends(get_db),
) -> ResponseSchema[ScenarioCalculationMutationResponse]:
    draft = ScenarioDraftCalculationsService(db).save_draft_calculation( scenario_code, payload.model_dump())
    return ok(ScenarioCalculationMutationResponse(
        scenario_code=draft.scenario_code, revision_no=draft.revision_no,
    ), request)


@router.put("/{scenario_code}/draft/calculations/{rule_code}", response_model=ResponseSchema[ScenarioCalculationMutationResponse])
def update_calculation(
    scenario_code: str, rule_code: str, payload: DraftCalculationRequest, request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[ScenarioCalculationMutationResponse]:
    draft = ScenarioDraftCalculationsService(db).save_draft_calculation( scenario_code, payload.model_dump(), rule_code=rule_code)
    return ok(ScenarioCalculationMutationResponse(
        scenario_code=draft.scenario_code, revision_no=draft.revision_no,
    ), request)


@router.delete("/{scenario_code}/draft/calculations/{rule_code}", response_model=ResponseSchema[ScenarioCalculationMutationResponse])
def delete_calculation(
    scenario_code: str, rule_code: str, request: Request, db: Session = Depends(get_db),
) -> ResponseSchema[ScenarioCalculationMutationResponse]:
    draft = ScenarioDraftCalculationsService(db).delete_draft_calculation( scenario_code, rule_code)
    return ok(ScenarioCalculationMutationResponse(
        scenario_code=draft.scenario_code, revision_no=draft.revision_no,
    ), request)
