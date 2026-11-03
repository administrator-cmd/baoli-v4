"""规则库导入作业列表、摘要与详情查询：SQL 在 crud，本层负责组装视图。"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.common.pagination import PageResult, page_result
from app.domain import node_profile
from app.modules.imports.job import crud
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.lookup import ImportsJobLookupService
from app.modules.imports.job.service.proposals import ImportsJobProposalsService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_IMPORT_JOB


class ImportsJobQueryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def list_import_jobs_page(self, *,
        filename: str | None,
        status: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
        scenario_code: str | None = None,
    ) -> PageResult[dict[str, Any]]:
        """导入作业分页列表（管理端）。"""
        rows, total = crud.ImportsJobCRUD(self.db).import_job_page_rows(
            filename=filename,
            status=status,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
            scenario_code=scenario_code,
        )
        # 场景名一次查出，逐行查会让列表页变成 N+1 次查询。
        scenario_names = crud.ImportsJobCRUD(self.db).scenario_names( sorted({row.scenario_code for row in rows if row.scenario_code})
        )
        # 转换期间的作业状态不会变（retry_conversion 全程持有作业行锁、结束时才提交），
        # 所以「有没有未结束的后台操作」只能问 background_operation，不能靠作业状态猜。
        active_operation_counts = crud.ImportsJobCRUD(self.db).active_operation_counts( [row.job_no for row in rows])
        published_versions = crud.ImportsJobCRUD(self.db).published_versions( [row.id for row in rows])

        items = []
        for job in rows:
            items.append(
                {
                    "job_no": job.job_no,
                    "source_filename": job.source_filename,
                    "status": job.status,
                    "profile_version": job.profile_version,
                    # 作业所属场景：上传时必须指定，因此列表能直接看出这份规则库属于哪个场景。
                    # 历史作业没有这一列，如实返回 None（页面显示「未记录场景」），
                    # 不按表头或文件名替它补一个。
                    "scenario_code": job.scenario_code,
                    "scenario_name": scenario_names.get(job.scenario_code),
                    "draft_revision": job.draft_revision,
                    "source_size": job.source_size,
                    # 仅当这份 Excel 发布成了场景版本才有编号；未发布的草稿作业为 None。
                    "published_version_no": published_versions.get(int(job.id)),
                    "active_operation_count": active_operation_counts.get(job.job_no, 0),
                    "failure_code": job.failure_code,
                    "failure_message": job.failure_message,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                }
            )
        return page_result(items, total, page, page_size)


    def get_import_job(self, job_no: str) -> ImportJob:
        """按作业编号取导入作业；取不到即 404。"""
        return ImportsJobLookupService(self.db)._job_or_404( job_no)


    def _job_profile_summary(self, job: ImportJob) -> dict[str, Any]:
        """返回执行契约版本与作业所属场景身份。"""
        summary = node_profile.node_summary()
        scenario_name: str | None = None
        if job.scenario_code:
            template = crud.ImportsJobCRUD(self.db).scenario_by_code( job.scenario_code)
            scenario_name = template.scenario_name if template is not None else None
        return {
            **summary,
            "scenario_code": job.scenario_code,
            "scenario_name": scenario_name,
            "scenario_schema_key": job.schema_key,
            "scenario_schema_version": job.schema_version,
        }


    def import_job_view(self, job: ImportJob) -> dict[str, Any]:
        """导入作业的业务视图：二轮整理结果、修订号、失败与可重试阶段。"""
        from app.modules.imports.job.service.llm import ImportsJobLlmService

        return {
            "job_no": job.job_no,
            "source_filename": job.source_filename,
            "status": job.status,
            "profile_version": job.profile_version,
            "schema_key": job.schema_key,
            "schema_version": job.schema_version,
            "schema_json": ImportsJobLookupService()._job_schema(job),
            # 作业所属场景及执行契约摘要。
            "profile": self._job_profile_summary( job),
            "draft_revision": job.draft_revision,
            "proposals": ImportsJobProposalsService()._proposal_business_view(job),
            # 第一轮目录可复用时可只重跑第二轮；检查点缺失或契约变化时为 False。
            "can_resume_refinement": ImportsJobLlmService().can_resume_initial(
                job.job_no, job.profile_version, job.schema_key, job.schema_version, job.source_sha256,
                job.scenario_code,
            ),
            "failure": {"code": job.failure_code, "message": job.failure_message} if job.failure_code else None,
            "latest_operation": OperationBackgroundService().operation_view(latest) if (latest := OperationBackgroundService(self.db).latest_operation( OBJECT_IMPORT_JOB, job.job_no)) else None,
            "created_at": job.created_at,
        }


    def import_job_summaries(self) -> list[dict[str, Any]]:
        """导入作业摘要列表（只取摘要列，避免大 JSON 列撑爆排序缓冲）。"""
        return [
            {
                "job_no": row.job_no,
                "source_filename": row.source_filename,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in crud.ImportsJobCRUD(self.db).job_summary_rows()
        ]
