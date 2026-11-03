"""系统域查询的 SQL 层：只取行，不组装视图。"""

from datetime import datetime

from sqlalchemy import and_, case, func, literal, or_, select, text, union_all

from app.core.base_crud import CRUDBase
from app.modules.audit.task.model import AuditTask
from app.modules.imports.job.model import ImportJob
from app.modules.system.operation.model import BackgroundOperation, OperationLog


class SystemOperationCRUD(CRUDBase):
    model = OperationLog

    def lock_by_id(self, operation_id: int) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation).where(BackgroundOperation.id == operation_id)
            .with_for_update().execution_options(populate_existing=True)
        )

    def lock_for_run(self, operation_id: int) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation).where(BackgroundOperation.id == operation_id).with_for_update()
        )

    def deletion_log_id(self, *, object_type: str, object_no: str, operation_type: str, lock: bool = False) -> int | None:
        statement = select(OperationLog.id).where(
            OperationLog.object_type == object_type,
            OperationLog.object_no == object_no,
            OperationLog.operation_type == operation_type,
            OperationLog.permanent.is_(True),
        ).limit(1)
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def running_for_object(self, *, object_type: str, object_no: str) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation)
            .where(
                BackgroundOperation.object_type == object_type,
                BackgroundOperation.object_no == object_no,
                BackgroundOperation.status == "RUNNING",
            )
            .order_by(BackgroundOperation.id.desc())
        )

    def active_for_object(self, *, object_type: str, object_no: str, statuses, lock: bool = False) -> BackgroundOperation | None:
        statement = (
            select(BackgroundOperation)
            .where(
                BackgroundOperation.object_type == object_type,
                BackgroundOperation.object_no == object_no,
                BackgroundOperation.status.in_(statuses),
            )
            .order_by(BackgroundOperation.id.desc())
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def lock_by_operation_no(self, operation_no: str, *, object_type: str, object_no: str) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation).where(
                BackgroundOperation.operation_no == operation_no,
                BackgroundOperation.object_type == object_type,
                BackgroundOperation.object_no == object_no,
            ).with_for_update().execution_options(populate_existing=True)
        )

    def by_operation_no(self, operation_no: str) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation).where(BackgroundOperation.operation_no == operation_no)
        )

    def latest_for_object(self, *, object_type: str, object_no: str) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation)
            .where(BackgroundOperation.object_type == object_type, BackgroundOperation.object_no == object_no)
            .order_by(BackgroundOperation.id.desc())
        )

    def active_all(self, statuses) -> list[BackgroundOperation]:
        return list(self.db.scalars(
            select(BackgroundOperation).where(BackgroundOperation.status.in_(statuses))
        ).all())

    def idempotency_record(self, *, object_type: str, operation_type: str, idempotency_key: str, lock: bool = False) -> OperationLog | None:
        statement = select(OperationLog).where(
            OperationLog.object_type == object_type,
            OperationLog.operation_type == operation_type,
            OperationLog.idempotency_key == idempotency_key,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def operations_for_jobs(self, object_type: str, job_nos: list[str], *, lock: bool = False) -> list[BackgroundOperation]:
        statement = select(BackgroundOperation).where(
            BackgroundOperation.object_type == object_type,
            BackgroundOperation.object_no.in_(job_nos),
        )
        if lock:
            statement = statement.with_for_update()
        return list(self.db.scalars(statement).all())

    def _business_entry_select(self):
        return select(
            OperationLog.log_no.label("entry_no"),
            literal("BUSINESS").label("source"),
            OperationLog.object_type.label("object_type"),
            OperationLog.object_no.label("object_no"),
            OperationLog.object_name.label("object_name"),
            OperationLog.operation_type.label("operation_type"),
            OperationLog.result.label("result"),
            # 列位置与命令查询对齐：第 8 列统一是耗时（业务为毫秒，命令为微秒）。
            OperationLog.duration_ms.label("duration_value"),
            OperationLog.command_no.label("command_no"),
            OperationLog.failure_code.label("failure_code"),
            OperationLog.failure_message.label("failure_message"),
            OperationLog.created_at.label("created_at"),
            # 业务日志是追加写入的：写入时间即完成时间，没有单独的 finished_at 列。
            OperationLog.updated_at.label("finished_at"),
        )


    def _command_entry_select(self):
        object_name = func.coalesce(
            select(AuditTask.task_name).where(AuditTask.task_no == BackgroundOperation.object_no).limit(1).scalar_subquery(),
            select(ImportJob.source_filename).where(ImportJob.job_no == BackgroundOperation.object_no).limit(1).scalar_subquery(),
            BackgroundOperation.object_no,
        )
        # 命令状态映射成统一结果口径；PENDING/RUNNING 是“执行中”，不是成功。
        result = case(
            (BackgroundOperation.status == "SUCCEEDED", literal("SUCCEEDED")),
            (BackgroundOperation.status.in_(("FAILED", "INTERRUPTED")), literal("FAILED")),
            else_=literal("ACCEPTED"),
        )
        return select(
            BackgroundOperation.operation_no.label("entry_no"),
            literal("COMMAND").label("source"),
            BackgroundOperation.object_type.label("object_type"),
            BackgroundOperation.object_no.label("object_no"),
            object_name.label("object_name"),
            BackgroundOperation.operation_type.label("operation_type"),
            result.label("result"),
            # 与业务日志同一列位置：命令的耗时按开始/结束时间计算，缺时间时为 NULL。
            func.timestampdiff(
                text("MICROSECOND"),
                BackgroundOperation.started_at,
                BackgroundOperation.finished_at,
            ).label("duration_value"),
            BackgroundOperation.operation_no.label("command_no"),
            BackgroundOperation.failure_code.label("failure_code"),
            BackgroundOperation.failure_message.label("failure_message"),
            BackgroundOperation.created_at.label("created_at"),
            BackgroundOperation.finished_at.label("finished_at"),
        )


    def operation_log_page_rows(self, *,
        object_type: str | None,
        operation_type: str | None,
        result: str | None,
        keyword: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ):
        """业务写操作与后台命令的并集分页行。"""
        business = self._business_entry_select().subquery()
        command = self._command_entry_select().subquery()
        combined = union_all(
            select(
                business.c.entry_no,
                business.c.source,
                business.c.object_type,
                business.c.object_no,
                business.c.object_name,
                business.c.operation_type,
                business.c.result,
                business.c.duration_value,
                business.c.command_no,
                business.c.failure_code,
                business.c.failure_message,
                business.c.created_at,
                business.c.finished_at,
            ),
            select(
                command.c.entry_no,
                command.c.source,
                command.c.object_type,
                command.c.object_no,
                command.c.object_name,
                command.c.operation_type,
                command.c.result,
                command.c.duration_value,
                command.c.command_no,
                command.c.failure_code,
                command.c.failure_message,
                command.c.created_at,
                command.c.finished_at,
            ),
        ).subquery("operation_entries")

        conditions = []
        if object_type:
            conditions.append(combined.c.object_type == object_type)
        if operation_type:
            conditions.append(combined.c.operation_type == operation_type)
        if result:
            conditions.append(combined.c.result == result)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(or_(
                combined.c.object_name.like(like),
                combined.c.object_no.like(like),
                combined.c.entry_no.like(like),
            ))
        if created_from:
            conditions.append(combined.c.created_at >= created_from)
        if created_to:
            conditions.append(combined.c.created_at <= created_to)

        where_clause = and_(*conditions) if conditions else None
        count_statement = select(func.count()).select_from(combined)
        rows_statement = select(combined)
        if where_clause is not None:
            count_statement = count_statement.where(where_clause)
            rows_statement = rows_statement.where(where_clause)
        total = self.db.scalar(count_statement) or 0
        rows = self.db.execute(
            rows_statement.order_by(combined.c.created_at.desc(), combined.c.entry_no.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return list(rows), int(total)

    def background_by_operation_key(
        self, *, object_type: str, object_no: str, operation_type: str, idempotency_key: str,
        lock: bool = False,
    ) -> BackgroundOperation | None:
        statement = select(BackgroundOperation).where(
            BackgroundOperation.object_type == object_type,
            BackgroundOperation.object_no == object_no,
            BackgroundOperation.operation_type == operation_type,
            BackgroundOperation.idempotency_key == idempotency_key,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(statement)
