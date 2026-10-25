"""规则库转换状态准备、候选清理与重试。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain.identity import public_no
from ...entity.imports import ImportJob
from ...entity.operations import BackgroundOperation
from ..shared.storage import resolve_storage_path
from ..system.operation_types import OBJECT_IMPORT_JOB
from .llm import CONVERSION_MODE_FULL, CONVERSION_MODE_RESUME, can_resume_initial, convert_workbook, initial_fingerprint
from .lookup import _ensure_import_mutable, _job_or_404, _job_schema
from .workbook import read_workbook_ir


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


def retry_conversion(
    db: Session, job_no: str, *, operation_no: str, mode: str = CONVERSION_MODE_FULL,
) -> ImportJob:
    """读取输入后释放事务，模型返回时再锁定作业并提交结果。

    工作簿原文按 source_path 重新读取：命令执行前已经核对过 source_sha256，
    同一份文件重读得到同一份 IR，因此不需要把整份原文也存进数据库。

    `mode=RESUME_REFINEMENT` 复用上次保存的第一轮目录，只重跑第二轮。
    第一轮结果按作业固定存放，输入指纹不符或文件缺失时明确拒绝，不静默退回整份重跑。
    """
    job = _conversion_job_for_write(db, job_no, operation_no)
    if mode not in {CONVERSION_MODE_FULL, CONVERSION_MODE_RESUME}:
        raise BusinessError("IMPORT_CONVERSION_MODE_INVALID", "不支持的规则库解析方式", status_code=400)
    draft_revision = job.draft_revision
    scenario_code = job.scenario_code
    profile_version = job.profile_version
    schema_key = job.schema_key
    schema_version = job.schema_version
    source_sha256 = job.source_sha256
    # 校验作业冻结的场景 schema；本模块不再使用返回值。
    _job_schema(job)
    fingerprint = initial_fingerprint(profile_version, schema_key, schema_version, source_sha256, scenario_code)
    if mode == CONVERSION_MODE_RESUME and not can_resume_initial(
        job_no, profile_version, schema_key, schema_version, source_sha256, scenario_code,
    ):
        raise BusinessError(
            "IMPORT_INITIAL_STAGE_UNAVAILABLE",
            "第一轮解析结果不可用（缺失、损坏或解析契约已变化），需要整份重新解析",
            status_code=409,
            details={"job_no": job_no},
        )
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
        proposals = convert_workbook(
            workbook_ir, operation_no=operation_no, job_no=job_no,
            fingerprint=fingerprint, mode=mode,
        )
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
        if exc.code == "IMPORT_INITIAL_STAGE_UNAVAILABLE":
            # 检查点不可用不是模型失败：作业保持可再次提交，页面按「整份重新解析」处理。
            job.status = "PARSED"
        elif exc.code in {
            "LLM_CONFIG_MISSING",
            "LLM_TIMEOUT",
            "LLM_AUTH_FAILED",
            "LLM_NETWORK_FAILED",
            "LLM_RESULT_UNKNOWN",
            "LLM_EXTERNAL_FAILED",
        }:
            job.status = "EXTERNAL_FAILED"
        else:
            job.status = "CONTRACT_INVALID"
        job.failure_code = exc.code
        job.failure_message = exc.message
        db.commit()
        raise
