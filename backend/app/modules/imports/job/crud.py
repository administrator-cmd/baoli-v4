"""导入作业查询的 SQL 层：只取行与实体，不组装视图。"""

from datetime import datetime

from sqlalchemy import func, select

from app.constants.status import ACTIVE_OPERATION_STATUSES
from app.core.base_crud import CRUDBase
from app.modules.imports.job.model import ImportJob
from app.modules.scenario.template.model import ScenarioTemplate, ScenarioVersion
from app.modules.system.operation.model import BackgroundOperation


class ImportsJobCRUD(CRUDBase):
    model = ImportJob

    def import_job_page_rows(self, *,
        filename: str | None,
        status: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
        scenario_code: str | None = None,
    ):
        """导入作业分页行；不带 proposals_json 等大 JSON 列。"""
        conditions = []
        if scenario_code:
            conditions.append(ImportJob.scenario_code == scenario_code)
        if filename:
            conditions.append(ImportJob.source_filename.like(f"%{filename.strip()}%"))
        if status:
            conditions.append(ImportJob.status == status)
        if created_from:
            conditions.append(ImportJob.created_at >= created_from)
        if created_to:
            conditions.append(ImportJob.created_at <= created_to)

        count_statement = select(func.count()).select_from(ImportJob)
        # 只取列表真正用到的列：import_job 带 proposals_json 等大 JSON 列，整行 SELECT * 会让
        # ORDER BY 的 filesort 把整行塞进排序缓冲（默认 256KB），两三条作业就报 1038
        # Out of sort memory；列表页从不读这些列。
        rows_statement = select(
            ImportJob.id,
            ImportJob.job_no,
            ImportJob.source_filename,
            ImportJob.status,
            ImportJob.profile_version,
            ImportJob.scenario_code,
            ImportJob.draft_revision,
            ImportJob.source_size,
            ImportJob.failure_code,
            ImportJob.failure_message,
            ImportJob.created_at,
            ImportJob.updated_at,
        )
        if conditions:
            count_statement = count_statement.where(*conditions)
            rows_statement = rows_statement.where(*conditions)
        total = self.db.scalar(count_statement) or 0
        rows = self.db.execute(
            rows_statement.order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return list(rows), int(total)


    def scenario_names(self, scenario_codes: list[str]) -> dict[str, str]:
        if not scenario_codes:
            return {}
        return dict(
            self.db.execute(
                select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name).where(
                    ScenarioTemplate.scenario_code.in_(scenario_codes)
                )
            ).all()
        )


    def scenario_by_code(self, scenario_code: str) -> ScenarioTemplate | None:
        return self.db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))


    def active_operation_counts(self, job_nos: list[str]) -> dict[str, int]:
        """未结束的后台操作数；只回计数，列表不读整个操作对象。"""
        if not job_nos:
            return {}
        return {
            str(object_no): int(count)
            for object_no, count in self.db.execute(
                select(BackgroundOperation.object_no, func.count())
                .where(
                    BackgroundOperation.object_type == "IMPORT_JOB",
                    BackgroundOperation.object_no.in_(job_nos),
                    BackgroundOperation.status.in_(ACTIVE_OPERATION_STATUSES),
                )
                .group_by(BackgroundOperation.object_no)
            ).all()
        }

    def latest_operations(self, job_nos: list[str]) -> dict[str, BackgroundOperation]:
        latest_ids = (
            select(func.max(BackgroundOperation.id))
            .where(BackgroundOperation.object_type == "IMPORT_JOB", BackgroundOperation.object_no.in_(job_nos))
            .group_by(BackgroundOperation.object_no)
        )
        return {operation.object_no: operation for operation in self.db.scalars(
            select(BackgroundOperation).where(BackgroundOperation.id.in_(latest_ids))
        ).all()}


    def published_versions(self, job_ids: list[int]) -> dict[int, int]:
        """作业发布出的场景版本号；import_job_id 有唯一约束，一个作业最多一个版本。"""
        if not job_ids:
            return {}
        return {
            int(version_job_id): int(version_no)
            for version_job_id, version_no in self.db.execute(
                select(ScenarioVersion.import_job_id, ScenarioVersion.version_no).where(
                    ScenarioVersion.import_job_id.in_(job_ids)
                )
            ).all()
        }


    def job_summary_rows(self):
        """摘要列表只取摘要列，避免大 JSON 列撑爆排序缓冲。"""
        return list(
            self.db.execute(
                select(ImportJob.job_no, ImportJob.source_filename, ImportJob.status, ImportJob.created_at)
                .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
            ).all()
        )

    def by_no(self, job_no: str, *, lock: bool = False) -> ImportJob | None:
        statement = select(ImportJob).where(ImportJob.job_no == job_no)
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)
