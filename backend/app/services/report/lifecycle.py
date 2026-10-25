"""审核报告备注维护与归档状态变更。"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...entity.reports import AuditReport
from ...entity.tasks import AuditTask
from ..system.operation_log import record_operation
from ..system.operation_types import OBJECT_REPORT
from .query import report_or_404


def update_report_remark(db: Session, report_no: str, remark: str | None) -> AuditReport:
    """只更新报告备注，不动正文与快照。"""
    report = report_or_404(db, report_no)
    report.remark = (str(remark).strip() or None) if remark is not None else None
    task = db.get(AuditTask, report.audit_task_id)
    record_operation(
        db,
        object_type=OBJECT_REPORT,
        object_no=report.report_no,
        object_name=report.report_name,
        operation_type="REPORT_REMARK",
        detail={"report_no": report.report_no, "task_no": task.task_no if task else None},
    )
    db.commit()
    db.refresh(report)
    return report


def set_report_archived(db: Session, report_no: str, archived: bool) -> AuditReport:
    """归档 / 取消归档报告快照；不删除任何历史快照。"""
    report = report_or_404(db, report_no)
    if archived and report.archived_at is not None:
        raise BusinessError("REPORT_ALREADY_ARCHIVED", "报告已归档", status_code=409, details={"report_no": report_no})
    if not archived and report.archived_at is None:
        raise BusinessError("REPORT_NOT_ARCHIVED", "报告未归档，无需取消归档", status_code=409, details={"report_no": report_no})
    report.archived_at = datetime.now(UTC) if archived else None
    report.status = "ARCHIVED" if archived else "GENERATED"
    task = db.get(AuditTask, report.audit_task_id)
    record_operation(
        db,
        object_type=OBJECT_REPORT,
        object_no=report.report_no,
        object_name=report.report_name,
        operation_type="REPORT_ARCHIVE" if archived else "REPORT_UNARCHIVE",
        detail={"report_no": report.report_no, "task_no": task.task_no if task else None},
    )
    db.commit()
    db.refresh(report)
    return report
