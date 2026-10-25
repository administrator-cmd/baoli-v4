"""规则库导入作业列表、摘要与详情查询。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...common.pagination import PageResult, page_result
from ...domain import node_profile
from ...entity.imports import ImportJob
from ...entity.operations import BackgroundOperation
from ...entity.scenarios import ScenarioTemplate, ScenarioVersion
from ..system.background import latest_operation, operation_view
from ..system.operation_types import OBJECT_IMPORT_JOB
from .lookup import _job_or_404, _job_schema
from .proposals import _proposal_business_view


def list_import_jobs_page(
    db: Session,
    *,
    filename: str | None,
    status: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    page: int,
    page_size: int,
    scenario_code: str | None = None,
) -> PageResult[dict[str, Any]]:
    """导入作业分页列表（管理端）。"""
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
        # 作业所属场景：列表页显示「哪个场景的规则库」要用它，字段很小不影响排序缓冲。
        ImportJob.scenario_code,
        ImportJob.draft_revision,
        # 文件资产信息：规则库管理页要显示原件的真实大小。
        ImportJob.source_size,
        ImportJob.failure_code,
        ImportJob.failure_message,
        ImportJob.created_at,
        ImportJob.updated_at,
    )
    if conditions:
        count_statement = count_statement.where(*conditions)
        rows_statement = rows_statement.where(*conditions)
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        rows_statement.order_by(ImportJob.created_at.desc(), ImportJob.id.desc()).limit(page_size).offset((page - 1) * page_size)
    ).all()

    # 场景名一次查出，逐行查会让列表页变成 N+1 次查询。
    scenario_codes = sorted({row.scenario_code for row in rows if row.scenario_code})
    scenario_names = (
        {
            code: name
            for code, name in db.execute(
                select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name).where(
                    ScenarioTemplate.scenario_code.in_(scenario_codes)
                )
            ).all()
        }
        if scenario_codes
        else {}
    )
    # 转换期间的作业状态不会变（retry_conversion 全程持有作业行锁、结束时才提交），
    # 所以「有没有未结束的后台操作」只能问 background_operation，不能靠作业状态猜。
    # 列表只用来决定「中止」按钮是否可用，因此只回活跃操作数，不回整个操作对象。
    active_operation_counts: dict[str, int] = {}
    job_nos = [row.job_no for row in rows]
    if job_nos:
        for object_no, count in db.execute(
            select(BackgroundOperation.object_no, func.count())
            .where(
                BackgroundOperation.object_type == "IMPORT_JOB",
                BackgroundOperation.object_no.in_(job_nos),
                BackgroundOperation.status.in_(("PENDING", "RUNNING")),
            )
            .group_by(BackgroundOperation.object_no)
        ).all():
            active_operation_counts[str(object_no)] = int(count)

    # 该作业发布出的场景版本（一个作业最多对应一个版本，import_job_id 有唯一约束）。
    published_versions: dict[int, int] = {}
    job_ids = [row.id for row in rows]
    if job_ids:
        for version_job_id, version_no in db.execute(
            select(ScenarioVersion.import_job_id, ScenarioVersion.version_no).where(
                ScenarioVersion.import_job_id.in_(job_ids)
            )
        ).all():
            published_versions[int(version_job_id)] = int(version_no)

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


def get_import_job(db: Session, job_no: str) -> ImportJob:
    """按作业编号取导入作业；取不到即 404。"""
    return _job_or_404(db, job_no)


def _job_profile_summary(db: Session, job: ImportJob) -> dict[str, Any]:
    """返回执行契约版本与作业所属场景身份。"""
    summary = node_profile.node_summary()
    scenario_name: str | None = None
    if job.scenario_code:
        template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == job.scenario_code))
        scenario_name = template.scenario_name if template is not None else None
    return {
        **summary,
        "scenario_code": job.scenario_code,
        "scenario_name": scenario_name,
        "scenario_schema_key": job.schema_key,
        "scenario_schema_version": job.schema_version,
    }


def import_job_view(db: Session, job: ImportJob) -> dict[str, Any]:
    """导入作业的业务视图：二轮整理结果、修订号、失败与可重试阶段。"""
    from .llm import can_resume_initial

    return {
        "job_no": job.job_no,
        "source_filename": job.source_filename,
        "status": job.status,
        "profile_version": job.profile_version,
        "schema_key": job.schema_key,
        "schema_version": job.schema_version,
        "schema_json": _job_schema(job),
        # 作业所属场景及执行契约摘要。
        "profile": _job_profile_summary(db, job),
        "draft_revision": job.draft_revision,
        "proposals": _proposal_business_view(job),
        # 第一轮目录可复用时可只重跑第二轮；检查点缺失或契约变化时为 False。
        "can_resume_refinement": can_resume_initial(
            job.job_no, job.profile_version, job.schema_key, job.schema_version, job.source_sha256,
            job.scenario_code,
        ),
        "failure": {"code": job.failure_code, "message": job.failure_message} if job.failure_code else None,
        "latest_operation": operation_view(latest) if (latest := latest_operation(db, OBJECT_IMPORT_JOB, job.job_no)) else None,
        "created_at": job.created_at,
    }


def import_job_summaries(db: Session) -> list[dict[str, Any]]:
    # 只取摘要要用的列：整行 SELECT * 时，import_job 的大 JSON 列会撑爆 ORDER BY 的
    # 排序缓冲（默认 256KB），直接报 1038 Out of sort memory。
    """导入作业摘要列表（只取摘要列，避免大 JSON 列撑爆排序缓冲）。"""
    rows = db.execute(
        select(ImportJob.job_no, ImportJob.source_filename, ImportJob.status, ImportJob.created_at)
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
    ).all()
    return [
        {
            "job_no": row.job_no,
            "source_filename": row.source_filename,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]
