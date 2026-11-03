"""审核任务的风险与报告接口。"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.audit.report.schema import ReportResponse, RiskListResponse
from app.modules.audit.report.service.downloads import report_download_response
from app.modules.audit.report.service.views import AuditReportViewsService

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("/{task_no}/risks", response_model=ResponseSchema[RiskListResponse])
def get_risks(task_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[RiskListResponse]:
    """该任务的风险结果：命中只统计 ABNORMAL，未配置 / 未执行 / 无法判断分别表达。"""
    return ok(RiskListResponse.model_validate(AuditReportViewsService(db).risks_view( task_no)), request)


@router.get("/{task_no}/reports", response_model=ResponseSchema[ReportResponse])
def get_report(task_no: str, request: Request, report_no: str | None = None, db: Session = Depends(get_db)) -> ResponseSchema[ReportResponse]:
    """该任务的审核报告视图：正文、三信号与生成时的范围快照。"""
    return ok(ReportResponse.model_validate(AuditReportViewsService(db).report_view( task_no, report_no)), request)


@router.get("/{task_no}/reports/download")
def download_report(task_no: str, report_no: str | None = None, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；报告尚未生成时明确 409，不返回空文件。"""
    report = AuditReportViewsService(db).report_view( task_no, report_no)
    content = report["content"]
    if report["status"] == "NOT_GENERATED" or not isinstance(content, str) or not content:
        raise BusinessError("REPORT_NOT_GENERATED", "审核报告尚未生成，无法下载", status_code=409)
    if not report["current"]:
        raise BusinessError("REPORT_STALE", "报告对应的输入或执行批次已失效，不能下载为当前报告", status_code=409)
    safe_name = "".join(char for char in str(report.get("report_name") or "审核报告") if char.isalnum() or char in "-_（）() ").strip() or "审核报告"
    return report_download_response(content, report["content_type"], f"{task_no}-{safe_name}")
