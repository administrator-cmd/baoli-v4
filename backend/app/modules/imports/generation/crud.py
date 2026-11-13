"""生成作业、冻结导出与导入关联的 SQL 层。"""

from datetime import datetime

from sqlalchemy import case, func, select

from app.constants.status import ACTIVE_OPERATION_STATUSES
from app.core.base_crud import CRUDBase
from app.domain.generation.document import (
    GENERATION_DOCUMENT_SCHEMA_VERSION,
    GENERATION_STATUS_EDITABLE,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_INTERRUPTED,
    GENERATION_STATUS_NO_DEFINITIONS,
    GENERATION_STATUS_NOT_GENERATED,
    GENERATION_STATUS_PENDING,
    GENERATION_STATUS_RUNNING,
    GENERATION_STATUS_UNKNOWN,
)
from app.modules.imports.generation.model import (
    RulebookGenerationExport,
    RulebookGenerationImport,
    RulebookGenerationJob,
    RulebookGenerationMessage,
    RulebookGenerationRevision,
)
from app.modules.imports.job.model import ImportJob
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION

_CONTENT_PATHS = ("sheets", "issues")


def _latest_operation_status():
    """该生成作业最近一次后台操作状态；按 id 取最新一行。"""
    return (
        select(BackgroundOperation.status)
        .where(
            BackgroundOperation.object_type == OBJECT_RULEBOOK_GENERATION,
            BackgroundOperation.object_no == RulebookGenerationJob.job_no,
        )
        .order_by(BackgroundOperation.id.desc())
        .limit(1)
        .correlate(RulebookGenerationJob)
        .scalar_subquery()
    )


def _content_count():
    """当前文档统计业务表和问题；旧格式只标记已有内容，不解析或转换旧协议。"""
    current_count = sum(
        func.coalesce(func.json_length(RulebookGenerationJob.document_json, f"$.{path}"), 0)
        for path in _CONTENT_PATHS
    )
    return case(
        (RulebookGenerationJob.document_schema_version == GENERATION_DOCUMENT_SCHEMA_VERSION, current_count),
        else_=1,
    )


def _has_document():
    # 兼容此前以 JSON null 保存的未生成作业，避免列表与详情状态不一致。
    return func.coalesce(func.json_type(RulebookGenerationJob.document_json) != "NULL", False)


def _status_condition(status: str):
    latest = _latest_operation_status()
    has_document = _has_document()
    has_content = _content_count() > 0
    active = func.coalesce(latest.in_(ACTIVE_OPERATION_STATUSES), False)
    conditions = {
        GENERATION_STATUS_NOT_GENERATED: latest.is_(None) & ~has_document,
        GENERATION_STATUS_PENDING: latest == "PENDING",
        GENERATION_STATUS_RUNNING: latest == "RUNNING",
        GENERATION_STATUS_FAILED: (latest == "FAILED") & ~has_document,
        GENERATION_STATUS_INTERRUPTED: (latest == "INTERRUPTED") & ~has_document,
        GENERATION_STATUS_EDITABLE: has_document & has_content & ~active,
        GENERATION_STATUS_NO_DEFINITIONS: has_document & ~has_content & ~active,
        # 受理成功或其他非活动、非失败终态却没有文档：如实显示未知，不归入可编辑。
        GENERATION_STATUS_UNKNOWN: (
            latest.isnot(None) & ~has_document
            & ~latest.in_((*ACTIVE_OPERATION_STATUSES, "FAILED", "INTERRUPTED"))
        ),
    }
    return conditions[status]


class GenerationRevisionCRUD(CRUDBase):
    model = RulebookGenerationRevision

    def by_revision(self, job_id: int, revision: int) -> RulebookGenerationRevision | None:
        return self.db.scalar(select(self.model).where(
            self.model.generation_job_id == job_id, self.model.revision == revision,
        ))

    def summaries(self, job_id: int):
        return self.db.execute(select(
            self.model.revision, self.model.summary, self.model.origin, self.model.created_at,
        ).where(self.model.generation_job_id == job_id).order_by(self.model.revision.desc())).all()


class GenerationMessageCRUD(CRUDBase):
    model = RulebookGenerationMessage

    def page(self, job_id: int, *, before_id: int | None = None, limit: int = 40):
        statement = select(self.model, BackgroundOperation).join(
            BackgroundOperation, BackgroundOperation.id == self.model.operation_id,
        ).where(self.model.generation_job_id == job_id)
        if before_id is not None:
            statement = statement.where(self.model.id < before_id)
        return self.db.execute(statement.order_by(self.model.id.desc()).limit(limit)).all()

    def for_operation(self, operation_id: int, role: str) -> RulebookGenerationMessage | None:
        return self.db.scalar(select(self.model).where(
            self.model.operation_id == operation_id, self.model.role == role,
        ))


class RulebookGenerationCRUD(CRUDBase):
    model = RulebookGenerationJob

    def by_no(self, job_no: str, *, lock: bool = False) -> RulebookGenerationJob | None:
        statement = select(RulebookGenerationJob).where(RulebookGenerationJob.job_no == job_no)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def by_idempotency_key(self, key: str, *, lock: bool = False) -> RulebookGenerationJob | None:
        statement = select(RulebookGenerationJob).where(RulebookGenerationJob.idempotency_key == key)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def page_rows(
        self, *,
        filename: str | None,
        status: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ):
        """列表只取展示列；文档 JSON 与阶段状态按表达式读取，不整行加载。"""
        conditions = []
        if filename:
            conditions.append(RulebookGenerationJob.source_filename.like(f"%{filename.strip()}%"))
        if status:
            conditions.append(_status_condition(status))
        if created_from:
            conditions.append(RulebookGenerationJob.created_at >= created_from)
        if created_to:
            conditions.append(RulebookGenerationJob.created_at <= created_to)

        count_statement = select(func.count()).select_from(RulebookGenerationJob)
        rows_statement = select(
            RulebookGenerationJob.id,
            RulebookGenerationJob.job_no,
            RulebookGenerationJob.source_filename,
            RulebookGenerationJob.source_size,
            RulebookGenerationJob.revision,
            RulebookGenerationJob.document_schema_version,
            RulebookGenerationJob.prompt_version,
            RulebookGenerationJob.created_at,
            RulebookGenerationJob.updated_at,
            _has_document().label("has_document"),
            _content_count().label("content_count"),
            _latest_operation_status().label("latest_operation_status"),
        )
        if conditions:
            count_statement = count_statement.where(*conditions)
            rows_statement = rows_statement.where(*conditions)
        total = self.db.scalar(count_statement) or 0
        rows = self.db.execute(
            rows_statement.order_by(
                RulebookGenerationJob.created_at.desc(), RulebookGenerationJob.id.desc(),
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return list(rows), int(total)

    def latest_operations(self, job_nos: list[str]) -> dict[str, BackgroundOperation]:
        if not job_nos:
            return {}
        rows = self.db.scalars(
            select(BackgroundOperation)
            .where(
                BackgroundOperation.object_type == OBJECT_RULEBOOK_GENERATION,
                BackgroundOperation.object_no.in_(job_nos),
            )
            .order_by(BackgroundOperation.id.desc())
        ).all()
        latest: dict[str, BackgroundOperation] = {}
        for operation in rows:
            latest.setdefault(operation.object_no, operation)
        return latest


class RulebookGenerationExportCRUD(CRUDBase):
    model = RulebookGenerationExport

    def by_no(self, job_id: int, export_no: str, *, lock: bool = False) -> RulebookGenerationExport | None:
        statement = select(RulebookGenerationExport).where(
            RulebookGenerationExport.generation_job_id == job_id,
            RulebookGenerationExport.export_no == export_no,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def by_revision(self, job_id: int, revision: int, *, lock: bool = False) -> RulebookGenerationExport | None:
        statement = select(RulebookGenerationExport).where(
            RulebookGenerationExport.generation_job_id == job_id,
            RulebookGenerationExport.revision == revision,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def for_job(self, job_id: int) -> list[RulebookGenerationExport]:
        return list(
            self.db.scalars(
                select(RulebookGenerationExport)
                .where(RulebookGenerationExport.generation_job_id == job_id)
                .order_by(RulebookGenerationExport.revision.desc())
            ).all()
        )

    def latest_exports(self, job_ids: list[int]) -> dict[int, RulebookGenerationExport]:
        """列表行的最新导出；只回一行，不把全部修订塞进列表。"""
        if not job_ids:
            return {}
        rows = self.db.scalars(
            select(RulebookGenerationExport)
            .where(RulebookGenerationExport.generation_job_id.in_(job_ids))
            .order_by(RulebookGenerationExport.generation_job_id, RulebookGenerationExport.revision.desc())
        ).all()
        latest: dict[int, RulebookGenerationExport] = {}
        for row in rows:
            latest.setdefault(int(row.generation_job_id), row)
        return latest


class RulebookGenerationImportCRUD(CRUDBase):
    model = RulebookGenerationImport

    def by_request_key(self, export_id: int, idempotency_key: str) -> RulebookGenerationImport | None:
        return self.db.scalar(
            select(RulebookGenerationImport).where(
                RulebookGenerationImport.generation_export_id == export_id,
                RulebookGenerationImport.idempotency_key == idempotency_key,
            )
        )

    def for_exports(self, export_ids: list[int]) -> dict[int, list[RulebookGenerationImport]]:
        if not export_ids:
            return {}
        rows = self.db.scalars(
            select(RulebookGenerationImport)
            .where(RulebookGenerationImport.generation_export_id.in_(export_ids))
            .order_by(RulebookGenerationImport.id.desc())
        ).all()
        grouped: dict[int, list[RulebookGenerationImport]] = {}
        for row in rows:
            grouped.setdefault(row.generation_export_id, []).append(row)
        return grouped

    def import_jobs(self, job_ids: list[int]) -> dict[int, ImportJob]:
        if not job_ids:
            return {}
        rows = self.db.scalars(
            select(ImportJob).where(ImportJob.id.in_(job_ids))
        ).all()
        return {int(job.id): job for job in rows}
