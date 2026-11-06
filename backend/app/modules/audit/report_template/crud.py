"""报告模板 SQL 查询。"""

from sqlalchemy import func, select

from app.core.base_crud import CRUDBase
from app.modules.audit.report_template.model import ReportTemplate
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.operation_types import OBJECT_REPORT_TEMPLATE


class ReportTemplateCRUD(CRUDBase):
    model = ReportTemplate

    def by_no(self, template_no: str) -> ReportTemplate | None:
        return self.db.scalar(select(ReportTemplate).where(ReportTemplate.template_no == template_no))

    def by_upload_idempotency_key(self, key: str, *, lock: bool = False) -> ReportTemplate | None:
        statement = select(ReportTemplate).where(ReportTemplate.upload_idempotency_key == key)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def lock_by_no(self, template_no: str) -> ReportTemplate | None:
        return self.db.scalar(
            select(ReportTemplate).where(ReportTemplate.template_no == template_no)
            .with_for_update().execution_options(populate_existing=True)
        )

    def latest_operations(self, template_nos: list[str]) -> list[BackgroundOperation]:
        if not template_nos:
            return []
        latest_ids = (
            select(func.max(BackgroundOperation.id).label("id"))
            .where(
                BackgroundOperation.object_type == OBJECT_REPORT_TEMPLATE,
                BackgroundOperation.object_no.in_(template_nos),
            )
            .group_by(BackgroundOperation.object_no)
            .subquery()
        )
        return list(self.db.scalars(
            select(BackgroundOperation).join(latest_ids, BackgroundOperation.id == latest_ids.c.id)
        ).all())

    def list_all(self) -> list[ReportTemplate]:
        return list(self.db.scalars(select(ReportTemplate).order_by(ReportTemplate.updated_at.desc(), ReportTemplate.id.desc())).all())
