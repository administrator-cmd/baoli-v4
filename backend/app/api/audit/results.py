"""审核任务的风险、报告、字段纠错与字段核对接口。"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...core.database import get_db
from ...schema.dto.tasks import (
    FieldCorrectionRequest,
    FieldReviewConfirmRequest,
    FieldReviewSaveRequest,
)
from ...schema.vo.operations import OperationAcceptedResponse
from ...schema.vo.reports import ReportResponse
from ...schema.vo.tasks import FieldCorrectionResponse, FieldReviewSaveResponse, RiskListResponse
from ...services.audit.commands import correct_field_value, save_field_review
from ...services.audit.operations import submit_field_review_confirm
from ...services.audit.tasks.lookup import task_or_404
from ...services.audit.views import report_view, risks_view
from ...services.system.background import operation_view
from ..report.downloads import report_download_response

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("/{task_no}/risks", response_model=RiskListResponse)
def get_risks(task_no: str, request: Request, db: Session = Depends(get_db)) -> RiskListResponse:
    """该任务的风险结果：命中只统计 ABNORMAL，未配置 / 未执行 / 无法判断分别表达。"""
    return RiskListResponse.model_validate({**risks_view(db, task_no), "request_id": request.state.request_id})


@router.get("/{task_no}/reports", response_model=ReportResponse)
def get_report(task_no: str, request: Request, report_no: str | None = None, db: Session = Depends(get_db)) -> ReportResponse:
    """该任务的审核报告视图：正文、三信号与生成时的范围快照。"""
    return ReportResponse.model_validate({**report_view(db, task_no, report_no), "request_id": request.state.request_id})


@router.get("/{task_no}/reports/download")
def download_report(task_no: str, report_no: str | None = None, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；报告尚未生成时明确 409，不返回空文件。"""
    report = report_view(db, task_no, report_no)
    content = report["content"]
    if report["status"] == "NOT_GENERATED" or not isinstance(content, str) or not content:
        raise BusinessError("REPORT_NOT_GENERATED", "审核报告尚未生成，无法下载", status_code=409)
    if not report["current"]:
        raise BusinessError("REPORT_STALE", "报告对应的输入或执行批次已失效，不能下载为当前报告", status_code=409)
    safe_name = "".join(char for char in str(report.get("report_name") or "审核报告") if char.isalnum() or char in "-_（）() ").strip() or "审核报告"
    return report_download_response(content, report["content_type"], f"{task_no}-{safe_name}")


@router.post("/{task_no}/field-corrections", response_model=FieldCorrectionResponse)
def correct_field(
    task_no: str,
    payload: FieldCorrectionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> FieldCorrectionResponse:
    """对错值/漏值字段发起主动纠错；生成新的字段事实版本并失效下游结果。"""
    result = correct_field_value(db, task_no, payload)
    return FieldCorrectionResponse.model_validate({**result, "request_id": request.state.request_id})


@router.post("/{task_no}/field-review/save", response_model=FieldReviewSaveResponse)
def save_field_review_endpoint(
    task_no: str,
    payload: FieldReviewSaveRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> FieldReviewSaveResponse:
    """只保存等待核对期间的字段修改；任务仍停在等待核对，不触发后续规则。"""
    result = save_field_review(db, task_no, payload)
    return FieldReviewSaveResponse.model_validate({**result, "request_id": request.state.request_id})


@router.post("/{task_no}/field-review/confirm", response_model=OperationAcceptedResponse, status_code=202)
def confirm_field_review_endpoint(
    task_no: str,
    payload: FieldReviewConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OperationAcceptedResponse:
    """保存本次剩余修改并确认当前字段版本，随后在后台继续审核。

    返回 202 只代表操作已受理：字段修改与确认会在后台命令里先提交再做模型调用，
    真实结果用 operation_no 查询，或刷新任务详情查看。
    """
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_field_review_confirm(db, task, payload)
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )
