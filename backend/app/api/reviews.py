"""执行记录 API（跨任务的管理列表）。"""

from datetime import datetime
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..schema.query import validate_page_params
from ..services import admin_query_service
from ..schema.vo import PagedResponse

executions_router = APIRouter(prefix="/executions", tags=["执行记录"])


@executions_router.get("", response_model=PagedResponse)
def list_executions(
    request: Request,
    task_no: str | None = None,
    node_code: str | None = None,
    status: str | None = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """节点执行记录分页列表（跨任务）。"""
    page, page_size = validate_page_params(page, page_size)
    result = admin_query_service.list_executions_page(
        db,
        task_no=task_no,
        node_code=node_code,
        status=status,
        started_from=started_from,
        started_to=started_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@executions_router.get("/{execution_id}")
def get_execution(execution_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    """节点执行详情：输入快照来源、结果、错误与关联的后台命令编号。

    列表页不含这个接口的 eligibility 判断；重试入口由任务详情提供，
    只有任务确实 READY 且停在当前节点、无活跃操作时才出现。
    """
    return {**admin_query_service.execution_detail(db, execution_id), "request_id": request.state.request_id}
