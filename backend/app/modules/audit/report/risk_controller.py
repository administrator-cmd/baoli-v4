"""风险结果管理列表接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import RiskResultPageResponse
from app.core.database import get_db
from app.modules.audit.report.service import risks as report_risks
from app.schema.query.lists import RiskResultListQueryParam
from app.schema.query.pagination import PaginationQueryParam

risks_router = APIRouter(prefix="/risk-results", tags=["风险结果"])


@risks_router.get("", response_model=ResponseSchema[RiskResultPageResponse])
def list_risk_results(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[RiskResultListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[RiskResultPageResponse]:
    """跨任务的风险结果分页列表。"""
    result = report_risks.AuditReportRisksService(db).list_risk_results_page(
        
        task_no=search.task_no,
        task_name=search.task_name,
        scenario_code=search.scenario_code,
        plan_code=search.plan_code,
        risk_level=search.risk_level,
        status=search.status,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(RiskResultPageResponse.model_validate(result), request)
