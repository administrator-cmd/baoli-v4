"""审核报告与风险结果 API（跨任务）。

- 报告来自真实报告输出节点生成的快照；名称与备注可编辑，正文、三信号与
  范围快照不可手改。归档/取消归档只改变归档状态，不删除任何历史快照。
- 风险结果只取当前有效执行批次；命中只统计 ABNORMAL，证据不足、不适用、
  未执行分别展示，不能用结果总数冒充命中数。
"""

from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..common.exceptions import BusinessError
from ..schema.dto import ReportRemarkRequest
from ..schema.query import validate_page_params
from ..services import admin_query_service
from ..schema.vo import PagedResponse, RiskResultPageResponse
from .export_utils import EXPORT_PAGE_SIZE, csv_response

router = APIRouter(prefix="/reports", tags=["审核报告"])
risks_router = APIRouter(prefix="/risk-results", tags=["风险结果"])


@router.get("", response_model=PagedResponse)
def list_reports(
    request: Request,
    name: str | None = None,
    task_no: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    archived: bool | None = None,
    generated_from: datetime | None = None,
    generated_to: datetime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核报告分页列表：按名称、任务、场景、方案与归档状态筛选。"""
    page, page_size = validate_page_params(page, page_size)
    result = admin_query_service.list_reports_page(
        db,
        name=name,
        task_no=task_no,
        scenario_code=scenario_code,
        plan_code=plan_code,
        archived=archived,
        generated_from=generated_from,
        generated_to=generated_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/export")
def export_reports(
    name: str | None = None,
    task_no: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    archived: bool | None = None,
    generated_from: datetime | None = None,
    generated_to: datetime | None = None,
    db: Session = Depends(get_db),
):
    """按当前筛选导出报告列表 CSV。"""
    result = admin_query_service.list_reports_page(
        db,
        name=name,
        task_no=task_no,
        scenario_code=scenario_code,
        plan_code=plan_code,
        archived=archived,
        generated_from=generated_from,
        generated_to=generated_to,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["report_name"],
            item["task_name"],
            item["plan_name"] if not item["is_legacy_config"] else "历史任务配置",
            item["generated_at"],
            item["scope_execution_complete"],
            item["risk_conclusion_available"],
            item["status"],
            item["stale"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "审核报告列表.csv",
        ["报告名称", "任务", "方案", "生成时间", "范围完整性", "风险可判定性", "归档状态", "已失效"],
        rows,
    )


@router.get("/{report_no}")
def get_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """报告详情：正文、三信号、范围快照与结构化结论。"""
    return {**admin_query_service.report_detail(db, report_no), "request_id": request.state.request_id}


@router.patch("/{report_no}")
def update_report(report_no: str, payload: ReportRemarkRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """编辑报告备注：只改备注，不改正文、三信号与快照。"""
    admin_query_service.update_report_remark(db, report_no, payload.remark)
    return {**admin_query_service.report_detail(db, report_no), "request_id": request.state.request_id}


@router.post("/{report_no}/archive")
def archive_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """归档报告快照；只改归档状态，不删除任何历史快照。"""
    admin_query_service.set_report_archived(db, report_no, True)
    return {**admin_query_service.report_detail(db, report_no), "request_id": request.state.request_id}


@router.post("/{report_no}/unarchive")
def unarchive_report(report_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """取消归档。"""
    admin_query_service.set_report_archived(db, report_no, False)
    return {**admin_query_service.report_detail(db, report_no), "request_id": request.state.request_id}


@router.get("/{report_no}/download")
def download_report(report_no: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；快照没有正文时明确 409。"""
    report = admin_query_service.report_or_404(db, report_no)
    content = report.content_md
    if not isinstance(content, str) or not content:
        raise BusinessError(
            "REPORT_CONTENT_MISSING",
            "该报告快照没有可下载的正文（历史回填缺失或生成时未写入）",
            status_code=409,
            details={"report_no": report_no},
        )
    extension = {"text/markdown": "md", "text/plain": "txt", "text/html": "html"}[report.content_type]
    filename = f"{report.report_name}.{extension}"
    return StreamingResponse(
        BytesIO(content.encode("utf-8")),
        media_type=report.content_type + "; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@risks_router.get("", response_model=RiskResultPageResponse)
def list_risk_results(
    request: Request,
    task_no: str | None = None,
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
    result = admin_query_service.list_risk_results_page(
        db,
        task_no=task_no,
        scenario_code=scenario_code,
        plan_code=plan_code,
        risk_level=risk_level,
        status=status,
        page=page,
        page_size=page_size,
    )
    return RiskResultPageResponse.model_validate({**result, "request_id": request.state.request_id})


@risks_router.get("/export")
def export_risk_results(
    task_no: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    risk_level: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """按当前筛选导出风险结果列表 CSV。"""
    result = admin_query_service.list_risk_results_page(
        db,
        task_no=task_no,
        scenario_code=scenario_code,
        plan_code=plan_code,
        risk_level=risk_level,
        status=status,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["task_name"],
            item["name"],
            item["source_rule_name"],
            item["risk_level"],
            item["status"],
            item["suggestion"],
            item["message"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "风险结果列表.csv",
        ["任务", "风险名称", "来源规则", "风险等级", "当前状态", "处理建议", "说明"],
        rows,
    )
