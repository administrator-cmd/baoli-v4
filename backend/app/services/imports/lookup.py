"""规则库导入作业查询、结构读取与可变状态检查。"""

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from ...common.exceptions import BusinessError
from ...entity import ImportJob
from ...entity import ScenarioVersion
from ...domain import scenario_schema


METADATA_SHEET_STATUS = "EXCLUDED"


def _job_or_404(db: Session, job_no: str, *, lock: bool = False) -> ImportJob:
    statement = select(ImportJob).where(ImportJob.job_no == job_no)
    if lock:
        statement = statement.with_for_update()
    job = db.scalar(statement)
    if not job:
        raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": job_no})
    return job


def _job_schema(job: ImportJob) -> dict[str, Any]:
    if not isinstance(job.schema_json, dict):
        raise BusinessError(
            "IMPORT_SCENARIO_SCHEMA_MISSING",
            "导入作业缺少创建时的场景 schema 快照，不能继续解释工作簿",
            status_code=409,
            details={"job_no": job.job_no},
        )
    schema = scenario_schema.normalize_schema(job.schema_json)
    if job.schema_key != schema["schema_key"] or job.schema_version != schema["schema_version"]:
        raise BusinessError(
            "IMPORT_SCENARIO_SCHEMA_MISMATCH",
            "导入作业的场景 schema 身份与快照不一致",
            status_code=409,
            details={"job_no": job.job_no},
        )
    return schema


def _ensure_import_mutable(db: Session, job: ImportJob) -> None:
    published_version_id = db.scalar(
        select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == job.id).limit(1)
    )
    if job.status == "APPLIED":
        raise BusinessError(
            "IMPORT_ALREADY_APPLIED",
            "该导入作业已写入场景草稿，不能再修改。请到场景草稿里核对配置，或重新上传一份规则库",
            status_code=409,
            details={"job_no": job.job_no, "scenario_code": job.scenario_code},
        )
    if published_version_id is not None:
        raise BusinessError(
            "IMPORT_ALREADY_PUBLISHED",
            "该导入作业已产生场景版本，不能再修改或重新编译",
            status_code=409,
        )


def _saved_proposals(job: ImportJob) -> dict[str, Any]:
    """最终模型结果入库前已解析，读取时只确认有 JSON 对象。"""
    if not isinstance(job.proposals_json, dict):
        raise BusinessError("IMPORT_PROPOSALS_INVALID", "导入作业没有已保存的配置 JSON", status_code=409,
                            details={"job_no": job.job_no})
    return job.proposals_json
