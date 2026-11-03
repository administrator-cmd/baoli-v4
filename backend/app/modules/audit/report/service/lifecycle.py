"""审核报告备注维护与归档状态变更。"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.audit.report.crud import AuditReportCRUD
from app.modules.audit.report.model import AuditReport
from app.modules.audit.report.service.query import AuditReportQueryService
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_REPORT


class AuditReportLifecycleService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def update_report_remark(self, report_no: str, remark: str | None) -> AuditReport:
        """只更新报告备注，不动正文与快照。"""
        report = AuditReportQueryService(self.db).report_or_404( report_no)
        report.remark = (str(remark).strip() or None) if remark is not None else None
        task = AuditTaskCRUD(self.db).get_of(AuditTask, report.audit_task_id)
        OperationLogService(self.db).record_operation(

            object_type=OBJECT_REPORT,
            object_no=report.report_no,
            object_name=report.report_name,
            operation_type="REPORT_REMARK",
            detail={"report_no": report.report_no, "task_no": task.task_no if task else None},
        )
        AuditReportCRUD(self.db).flush()
        AuditReportCRUD(self.db).refresh(report)
        return report


    def set_report_archived(self, report_no: str, archived: bool) -> AuditReport:
        """归档 / 取消归档报告快照；不删除任何历史快照。"""
        report = AuditReportQueryService(self.db).report_or_404( report_no)
        if archived and report.archived_at is not None:
            raise BusinessError("REPORT_ALREADY_ARCHIVED", "报告已归档", status_code=409, details={"report_no": report_no})
        if not archived and report.archived_at is None:
            raise BusinessError("REPORT_NOT_ARCHIVED", "报告未归档，无需取消归档", status_code=409, details={"report_no": report_no})
        report.archived_at = datetime.now(UTC) if archived else None
        task = AuditTaskCRUD(self.db).get_of(AuditTask, report.audit_task_id)
        OperationLogService(self.db).record_operation(

            object_type=OBJECT_REPORT,
            object_no=report.report_no,
            object_name=report.report_name,
            operation_type="REPORT_ARCHIVE" if archived else "REPORT_UNARCHIVE",
            detail={"report_no": report.report_no, "task_no": task.task_no if task else None},
        )
        AuditReportCRUD(self.db).flush()
        AuditReportCRUD(self.db).refresh(report)
        return report
