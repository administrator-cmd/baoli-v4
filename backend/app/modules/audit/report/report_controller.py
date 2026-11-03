"""报告列表、详情、备注、归档与下载接口。"""


from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.response import ResponseSchema, ok
from app.core.base_schema import PagedResponse
from app.core.database import get_db
from app.modules.audit.report.dto import ReportRemarkRequest
from app.modules.audit.report.schema import ReportDetailResponse
from app.modules.audit.report.service import lifecycle as report_lifecycle
from app.modules.audit.report.service import query as report_query
from app.modules.audit.report.service.downloads import report_download_response
from app.schema.query.lists import ReportListQueryParam
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/reports", tags=["审核报告"])


@router.get("", response_model=ResponseSchema[PagedResponse])
def list_reports(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[ReportListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse]:
    """审核报告分页列表：按名称、任务、场景、方案与归档状态筛选。"""
    result = report_query.AuditReportQueryService(db).list_reports_page(
        
        name=search.name,
        task_no=search.task_no,
        task_name=search.task_name,
        scenario_code=search.scenario_code,
        plan_code=search.plan_code,
        archived=search.archived,
        generated_from=search.generated_from,
        generated_to=search.generated_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse.model_validate(result), request)


@router.get("/{report_no}", response_model=ResponseSchema[ReportDetailResponse])
def get_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ReportDetailResponse]:
    """报告详情：正文、三信号、范围快照与结构化结论。"""
    return ok(ReportDetailResponse.model_validate(report_query.AuditReportQueryService(db).report_detail( report_no)), request)


@router.patch("/{report_no}", response_model=ResponseSchema[ReportDetailResponse])
def update_report(report_no: str, payload: ReportRemarkRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ReportDetailResponse]:
    """编辑报告备注：只改备注，不改正文、三信号与快照。"""
    report_lifecycle.AuditReportLifecycleService(db).update_report_remark( report_no, payload.remark)
    return ok(ReportDetailResponse.model_validate(report_query.AuditReportQueryService(db).report_detail( report_no)), request)


@router.post("/{report_no}/archive", response_model=ResponseSchema[ReportDetailResponse])
def archive_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ReportDetailResponse]:
    """归档报告快照；只改归档状态，不删除任何历史快照。"""
    report_lifecycle.AuditReportLifecycleService(db).set_report_archived( report_no, True)
    return ok(ReportDetailResponse.model_validate(report_query.AuditReportQueryService(db).report_detail( report_no)), request)


@router.post("/{report_no}/unarchive", response_model=ResponseSchema[ReportDetailResponse])
def unarchive_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ReportDetailResponse]:
    """取消归档。"""
    report_lifecycle.AuditReportLifecycleService(db).set_report_archived( report_no, False)
    return ok(ReportDetailResponse.model_validate(report_query.AuditReportQueryService(db).report_detail( report_no)), request)


@router.get("/{report_no}/download")
def download_report(report_no: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；快照没有正文时明确 409。"""
    report = report_query.AuditReportQueryService(db).report_or_404( report_no)
    content = report.content_md
    if not isinstance(content, str) or not content:
        raise BusinessError(
            "REPORT_CONTENT_MISSING",
            "该报告快照没有已保存的可下载正文",
            status_code=409,
            details={"report_no": report_no},
        )
    return report_download_response(content, report.content_type, report.report_name)
