"""报告模板风格档案的后台分析操作。"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.transactions import commit_transaction, rollback_transaction
from app.modules.audit.report_template.crud import ReportTemplateCRUD
from app.modules.audit.report_template.model import ReportTemplate
from app.modules.audit.report_template.service.analysis import (
    analyze_report_style,
    extract_pdf_pages,
    has_usable_style_profile,
)
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.background import CommandHandle, OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_REPORT_TEMPLATE
from app.services.shared.commands import _idempotency_key
from app.services.shared.storage import resolve_storage_path

logger = logging.getLogger(__name__)

OPERATION_ANALYZE_REPORT_TEMPLATE = "REPORT_TEMPLATE_ANALYZE"


def _operation_is_running(session: Session, operation_no: str, template_no: str) -> None:
    operation = SystemOperationCRUD(session).lock_by_operation_no(
        operation_no, object_type=OBJECT_REPORT_TEMPLATE, object_no=template_no,
    )
    if operation is None or operation.status != "RUNNING":
        raise BusinessError(
            "OPERATION_NOT_ACTIVE", "报告模板分析操作已结束，拒绝写入过期结果", status_code=409,
            details={"operation_no": operation_no},
        )


def _write_failure(
    session: Session,
    *,
    template_no: str,
    operation_no: str,
    message: str,
    page_count: int | None = None,
) -> None:
    template = ReportTemplateCRUD(session).lock_by_no(template_no)
    if template is None:
        raise BusinessError("REPORT_TEMPLATE_NOT_FOUND", "报告模板不存在", status_code=404)
    _operation_is_running(session, operation_no, template_no)
    # 失败只更新诊断；旧档案（包括手工编辑过的档案）继续保留并可被任务使用。
    template.status = "READY" if has_usable_style_profile(template.style_profile_json) else "FAILED"
    template.analysis_error = message
    if page_count is not None:
        template.page_count = page_count
    commit_transaction(session)


def run_report_template_analysis(session: Session, command: CommandHandle) -> dict[str, Any]:
    """用后台操作自己的会话分析 PDF，外部服务等待期间不持有数据库事务。"""
    template_no = str(command.request["template_no"])
    template = ReportTemplateCRUD(session).lock_by_no(template_no)
    if template is None:
        raise BusinessError("REPORT_TEMPLATE_NOT_FOUND", "报告模板不存在", status_code=404)
    if template.source_sha256 != command.request.get("source_sha256"):
        rollback_transaction(session)
        _write_failure(
            session, operation_no=command.operation_no, template_no=template_no,
            message="模板原文件已变化，拒绝保存过期分析结果",
        )
        raise BusinessError("REPORT_TEMPLATE_SOURCE_CHANGED", "模板原文件已变化，拒绝保存过期分析结果", status_code=409)
    source_path = resolve_storage_path(template.source_path)
    filename = template.source_filename
    rollback_transaction(session)

    pages: list[dict[str, Any]] | None = None
    try:
        pages = extract_pdf_pages(source_path=source_path, filename=filename)
        style_profile = analyze_report_style(pages, template_no)
    except BusinessError as exc:
        _write_failure(
            session, operation_no=command.operation_no, template_no=template_no, message=exc.message,
            page_count=len(pages) if pages is not None else None,
        )
        raise
    except OSError as exc:
        source_unreadable = pages is None
        message = "原始 PDF 文件不存在或无法读取" if source_unreadable else "风格分析失败，请检查模型服务后重试"
        _write_failure(
            session, operation_no=command.operation_no, template_no=template_no,
            message=message,
            page_count=len(pages) if pages is not None else None,
        )
        raise BusinessError(
            "REPORT_TEMPLATE_SOURCE_UNREADABLE" if source_unreadable else "REPORT_TEMPLATE_ANALYSIS_FAILED",
            message, status_code=502,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - operation executor persists the failed state and diagnostic
        logger.exception("报告模板后台分析失败 template_no=%s", template_no)
        _write_failure(
            session, operation_no=command.operation_no, template_no=template_no,
            message="风格分析失败，请检查模型服务后重试",
            page_count=len(pages) if pages is not None else None,
        )
        raise BusinessError(
            "REPORT_TEMPLATE_ANALYSIS_FAILED", "风格分析失败，请检查模型服务后重试", status_code=502,
        ) from exc

    template = ReportTemplateCRUD(session).lock_by_no(template_no)
    if template is None:
        raise BusinessError("REPORT_TEMPLATE_NOT_FOUND", "报告模板不存在", status_code=404)
    _operation_is_running(session, command.operation_no, template_no)
    if template.source_sha256 != command.request.get("source_sha256"):
        rollback_transaction(session)
        _write_failure(
            session, operation_no=command.operation_no, template_no=template_no,
            message="模板原文件已变化，拒绝保存过期分析结果",
        )
        raise BusinessError("REPORT_TEMPLATE_SOURCE_CHANGED", "模板原文件已变化，拒绝保存过期分析结果", status_code=409)
    template.page_count = len(pages)
    template.style_profile_json = style_profile
    template.status = "READY"
    template.analysis_error = None
    commit_transaction(session)
    return {"template_no": template_no, "page_count": len(pages)}


class ReportTemplateAnalysisOperations:
    """提交分析操作；调用方必须已锁定模板行。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def submit(self, template: ReportTemplate, *, idempotency_key: str | None) -> tuple[BackgroundOperation, bool]:
        key = _idempotency_key(idempotency_key)
        source_path = resolve_storage_path(template.source_path)

        def validate() -> None:
            if not source_path.is_file():
                raise BusinessError(
                    "REPORT_TEMPLATE_SOURCE_MISSING", "原始 PDF 文件不存在或无法读取", status_code=404,
                )
            # 已有可用档案时，重新分析只显示更新状态，不下架旧档案。
            template.status = "READY" if has_usable_style_profile(template.style_profile_json) else "PROCESSING"
            template.analysis_error = None

        return OperationBackgroundService(self.db).submit_operation(
            object_type=OBJECT_REPORT_TEMPLATE,
            object_no=template.template_no,
            operation_type=OPERATION_ANALYZE_REPORT_TEMPLATE,
            idempotency_key=key,
            request={"template_no": template.template_no, "source_sha256": template.source_sha256},
            runner=run_report_template_analysis,
            input_version=template.source_sha256,
            validate=validate,
        )
