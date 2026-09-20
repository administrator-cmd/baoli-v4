"""人工复核与执行记录 API（跨任务的管理列表）。

- 复核列表显示系统原判断、人工结论与复核后的有效状态；进入具体任务复核阶段
  提交裁决（本页面不跨任务批量裁决）。
- 执行记录来自 node_execution；重试是否可用由服务端状态决定（只允许对
  当前节点、READY 状态且无活跃操作的任务重试），页面据 eligibility 显示。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BusinessError
from ..services import admin_query_service
from ..schemas import PagedResponse
from .export_utils import EXPORT_PAGE_SIZE, csv_response

router = APIRouter(prefix="/reviews", tags=["人工复核"])
executions_router = APIRouter(prefix="/executions", tags=["执行记录"])


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size


@router.get("", response_model=PagedResponse)
def list_reviews(
    request: Request,
    task_no: str | None = None,
    review_type: str | None = None,
    status: str | None = None,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    page, page_size = _page_params(page, page_size)
    result = admin_query_service.list_reviews_page(
        db,
        task_no=task_no,
        review_type=review_type,
        status=status,
        updated_from=updated_from,
        updated_to=updated_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/export")
def export_reviews(
    task_no: str | None = None,
    review_type: str | None = None,
    status: str | None = None,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    db: Session = Depends(get_db),
):
    result = admin_query_service.list_reviews_page(
        db,
        task_no=task_no,
        review_type=review_type,
        status=status,
        updated_from=updated_from,
        updated_to=updated_to,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["task_name"],
            item["object_name"],
            item["review_type"],
            item["system_judgment_status"],
            item["system_judgment_message"],
            item["decision"],
            item["effective_status"],
            item["basis_type"],
            item["status"],
            item["updated_at"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "人工复核列表.csv",
        ["任务", "复核对象", "复核类型", "系统原判断", "原判断说明", "人工结论", "复核后有效状态", "依据类型", "状态", "更新时间"],
        rows,
    )


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
    page, page_size = _page_params(page, page_size)
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


@executions_router.get("/export")
def export_executions(
    task_no: str | None = None,
    node_code: str | None = None,
    status: str | None = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    db: Session = Depends(get_db),
):
    result = admin_query_service.list_executions_page(
        db,
        task_no=task_no,
        node_code=node_code,
        status=status,
        started_from=started_from,
        started_to=started_to,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["task_name"],
            item["node_name"],
            item["status"],
            item["started_at"],
            item["finished_at"],
            item["duration_ms"],
            item["failure_code"],
            item["failure_message"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "执行记录列表.csv",
        ["任务", "执行节点", "执行状态", "开始时间", "结束时间", "耗时（毫秒）", "失败码", "失败原因"],
        rows,
    )


@executions_router.get("/{execution_id}")
def get_execution(execution_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    """节点执行详情：输入快照来源、结果、错误与关联的后台命令编号。

    列表页不含这个接口的 eligibility 判断；重试入口由任务详情提供，
    只有任务确实 READY 且停在当前节点、无活跃操作时才出现。
    """
    return {**admin_query_service.execution_detail(db, execution_id), "request_id": request.state.request_id}
