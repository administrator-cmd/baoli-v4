"""报告列表、详情、备注、归档与下载接口。"""


from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...core.database import get_db
from ...schema.dto.reports import ReportRemarkRequest
from ...schema.query.pagination import validate_page_params
from ...schema.query.timestamps import FilterDateTime
from ...schema.vo.common import PagedResponse
from ...services.report import lifecycle as report_lifecycle
from ...services.report import query as report_query
from .downloads import report_download_response

router = APIRouter(prefix="/reports", tags=["审核报告"])


@router.get("", response_model=PagedResponse)
def list_reports(
    request: Request,
    name: str | None = None,
    task_no: str | None = None,
    task_name: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    archived: bool | None = None,
    generated_from: FilterDateTime | None = None,
    generated_to: FilterDateTime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核报告分页列表：按名称、任务、场景、方案与归档状态筛选。"""
    page, page_size = validate_page_params(page, page_size)
    result = report_query.list_reports_page(
        db,
        name=name,
        task_no=task_no,
        task_name=task_name,
        scenario_code=scenario_code,
        plan_code=plan_code,
        archived=archived,
        generated_from=generated_from,
        generated_to=generated_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/{report_no}")
def get_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """报告详情：正文、三信号、范围快照与结构化结论。"""
    return {**report_query.report_detail(db, report_no), "request_id": request.state.request_id}


@router.patch("/{report_no}")
def update_report(report_no: str, payload: ReportRemarkRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """编辑报告备注：只改备注，不改正文、三信号与快照。"""
    report_lifecycle.update_report_remark(db, report_no, payload.remark)
    return {**report_query.report_detail(db, report_no), "request_id": request.state.request_id}


@router.post("/{report_no}/archive")
def archive_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """归档报告快照；只改归档状态，不删除任何历史快照。"""
    report_lifecycle.set_report_archived(db, report_no, True)
    return {**report_query.report_detail(db, report_no), "request_id": request.state.request_id}


@router.post("/{report_no}/unarchive")
def unarchive_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """取消归档。"""
    report_lifecycle.set_report_archived(db, report_no, False)
    return {**report_query.report_detail(db, report_no), "request_id": request.state.request_id}


@router.get("/{report_no}/download")
def download_report(report_no: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；快照没有正文时明确 409。"""
    report = report_query.report_or_404(db, report_no)
    content = report.content_md
    if not isinstance(content, str) or not content:
        raise BusinessError(
            "REPORT_CONTENT_MISSING",
            "该报告快照没有已保存的可下载正文",
            status_code=409,
            details={"report_no": report_no},
        )
    return report_download_response(content, report.content_type, report.report_name)
