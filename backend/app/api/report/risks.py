"""风险结果管理列表接口。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.query.pagination import validate_page_params
from ...schema.vo.common import RiskResultPageResponse
from ...services.report import risks as report_risks

risks_router = APIRouter(prefix="/risk-results", tags=["风险结果"])


@risks_router.get("", response_model=RiskResultPageResponse)
def list_risk_results(
    request: Request,
    task_no: str | None = None,
    task_name: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    risk_level: str | None = None,
    status: str | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> RiskResultPageResponse:
    """跨任务的风险结果分页列表。"""
    page, page_size = validate_page_params(page, page_size)
    result = report_risks.list_risk_results_page(
        db,
        task_no=task_no,
        task_name=task_name,
        scenario_code=scenario_code,
        plan_code=plan_code,
        risk_level=risk_level,
        status=status,
        page=page,
        page_size=page_size,
    )
    return RiskResultPageResponse.model_validate({**result, "request_id": request.state.request_id})
