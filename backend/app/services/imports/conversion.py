"""规则库转换状态准备、候选清理与重试。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...common.exceptions import BusinessError
from ...entity import ImportJob
from ...entity import BackgroundOperation
from ...domain.identity import public_no
from ..system.operation_types import OBJECT_IMPORT_JOB
from ..shared.storage import resolve_storage_path
from .llm import convert_workbook
from .workbook import read_workbook_ir
from .lookup import _ensure_import_mutable, _job_or_404, _job_schema


def _conversion_job_for_write(
    db: Session, job_no: str, operation_no: str, *, draft_revision: int | None = None,
) -> ImportJob:
    """短事务内确认转换仍有效；与中止共用加锁顺序，拒绝旧线程写回。"""
    job = _job_or_404(db, job_no, lock=True)
    operation = db.scalar(
        select(BackgroundOperation).where(
            BackgroundOperation.operation_no == operation_no,
            BackgroundOperation.object_type == OBJECT_IMPORT_JOB,
            BackgroundOperation.object_no == job_no,
        ).with_for_update().execution_options(populate_existing=True)
    )
    if operation is None:
        raise BusinessError("OPERATION_NOT_FOUND", "转换操作不存在", status_code=404)
    if operation.status != "RUNNING":
        raise BusinessError(
            "OPERATION_NOT_ACTIVE", "转换操作已结束或被中止，拒绝写入旧结果", status_code=409,
            details={"operation_no": operation_no, "status": operation.status},
        )
    if draft_revision is not None and job.draft_revision != draft_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT", "导入配置已变化，拒绝写入旧转换结果", status_code=409,
            details={"job_no": job_no},
        )
    _ensure_import_mutable(db, job)
    return job


def _reset_conversion_candidates(db: Session, job: ImportJob) -> None:
    """新结果与旧候选的替换在同一短事务中提交。"""
    job.proposals_json = None
    job.draft_revision = job.draft_revision + 1


def retry_conversion(db: Session, job_no: str, *, operation_no: str) -> ImportJob:
    """读取输入后释放事务，模型返回时再锁定作业并提交结果。

    工作簿原文按 source_path 重新读取：命令执行前已经核对过 source_sha256，
    同一份文件重读得到同一份 IR，因此不需要把整份原文也存进数据库。
    """
    job = _conversion_job_for_write(db, job_no, operation_no)
    draft_revision = job.draft_revision
    scenario_code = job.scenario_code
    schema_json = _job_schema(job)
    try:
        workbook_ir = read_workbook_ir(
            resolve_storage_path(job.source_path),
            workbook_id=public_no("wb"),
            source_filename=job.source_filename,
            source_size=job.source_size,
            source_sha256=job.source_sha256,
            profile_version=job.profile_version,
            scenario_code=scenario_code,
            schema_key=job.schema_key,
            schema_version=job.schema_version,
        )
        # 到此只有本地读取：显式结束事务并释放行锁，模型返回前不持有任何锁，
        # 后续不得访问已过期的 ORM 输入。
        db.rollback()
        proposals = convert_workbook(workbook_ir, operation_no=operation_no)
        job = _conversion_job_for_write(db, job_no, operation_no, draft_revision=draft_revision)
        _reset_conversion_candidates(db, job)
        job.proposals_json = proposals
        job.status = "PROPOSED"
        job.failure_code = None
        job.failure_message = None
        db.commit()
        return job
    except BusinessError as exc:
        # 撤销本次未提交的候选；只给仍在执行且输入未变化的命令记录转换失败。
        db.rollback()
        job = _conversion_job_for_write(db, job_no, operation_no, draft_revision=draft_revision)
        _reset_conversion_candidates(db, job)
        job.status = "EXTERNAL_FAILED" if exc.code in {
            "LLM_CONFIG_MISSING",
            "LLM_TIMEOUT",
            "LLM_AUTH_FAILED",
            "LLM_NETWORK_FAILED",
            "LLM_RESULT_UNKNOWN",
            "LLM_EXTERNAL_FAILED",
        } else "CONTRACT_INVALID"
        job.failure_code = exc.code
        job.failure_message = exc.message
        db.commit()
        raise
