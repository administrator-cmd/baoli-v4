"""场景草稿计算配置接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.scenarios import DraftCalculationRequest
from ...schema.vo.scenarios import ScenarioCalculationMutationResponse
from ...services.scenario.calculations import delete_draft_calculation, save_draft_calculation

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/calculations", response_model=ScenarioCalculationMutationResponse, status_code=201)
def create_calculation(
    scenario_code: str, payload: DraftCalculationRequest, request: Request, db: Session = Depends(get_db),
) -> ScenarioCalculationMutationResponse:
    draft = save_draft_calculation(db, scenario_code, payload.model_dump())
    return ScenarioCalculationMutationResponse(
        request_id=request.state.request_id, scenario_code=draft.scenario_code, revision_no=draft.revision_no,
    )


@router.put("/{scenario_code}/draft/calculations/{rule_code}", response_model=ScenarioCalculationMutationResponse)
def update_calculation(
    scenario_code: str, rule_code: str, payload: DraftCalculationRequest, request: Request,
    db: Session = Depends(get_db),
) -> ScenarioCalculationMutationResponse:
    draft = save_draft_calculation(db, scenario_code, payload.model_dump(), rule_code=rule_code)
    return ScenarioCalculationMutationResponse(
        request_id=request.state.request_id, scenario_code=draft.scenario_code, revision_no=draft.revision_no,
    )


@router.delete("/{scenario_code}/draft/calculations/{rule_code}", response_model=ScenarioCalculationMutationResponse)
def delete_calculation(
    scenario_code: str, rule_code: str, request: Request, db: Session = Depends(get_db),
) -> ScenarioCalculationMutationResponse:
    draft = delete_draft_calculation(db, scenario_code, rule_code)
    return ScenarioCalculationMutationResponse(
        request_id=request.state.request_id, scenario_code=draft.scenario_code, revision_no=draft.revision_no,
    )
