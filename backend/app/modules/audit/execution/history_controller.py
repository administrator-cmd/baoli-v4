"""执行记录 API（跨任务的管理列表）。"""


from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import PagedResponse
from app.core.database import get_db
from app.modules.audit.execution.service import executions as audit_executions
from app.schema.query.lists import ExecutionListQueryParam
from app.schema.query.pagination import PaginationQueryParam

executions_router = APIRouter(prefix="/executions", tags=["执行记录"])


@executions_router.get("", response_model=ResponseSchema[PagedResponse])
def list_executions(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[ExecutionListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse]:
    """节点执行记录分页列表（跨任务）。"""
    result = audit_executions.AuditExecutionExecutionsService(db).list_executions_page(

        task_no=search.task_no,
        node_code=search.node_code,
        status=search.status,
        started_from=search.started_from,
        started_to=search.started_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse.model_validate(result), request)
