"""将规则库转换结果应用到场景草稿。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...repositories.scenario.version import find_latest_published_version
from ...domain.manifest import draft_config_of_import
from ...common.exceptions import BusinessError
from ...entity import ImportJob
from ...entity import ScenarioVersion, ScenarioDraft
from ..system.background import assert_no_active_operation
from ..system.operation_types import OBJECT_IMPORT_JOB
from ..system.operation_log import record_operation
from ...domain import scenario_schema
from .lookup import _job_or_404, _job_schema, _saved_proposals


def apply_import_to_draft(
    db: Session, job_no: str, *, expected_job_revision: int, expected_scene_revision: int | None
) -> ImportJob:
    """把解析结果写入场景草稿，不生成或发布场景版本。

    不创建场景：场景身份以上传时记录的 scenario_code 为准，不从文件名、Sheet 名或表头另找。
    写入替换材料、字段、规则、报告与计算定义。
    写入前核对草稿修订，执行时由 LLM 按生效规则处理。
    """
    from ..system.operation_types import OBJECT_SCENARIO
    from ..scenario.draft import _bump_draft, _commit_config_change, ensure_draft, load_import_config_into_draft
    from ..scenario.lookup import template_or_404

    job = _job_or_404(db, job_no, lock=True)
    if job.status == "APPLIED":
        return job
    if job.draft_revision != expected_job_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT",
            "导入候选在本次确认期间已修改，请重新核对后再写入场景草稿",
            status_code=409,
            details={"job_no": job_no, "expected": expected_job_revision, "actual": job.draft_revision},
        )
    existing_version_id = db.scalar(select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == job.id).limit(1))
    if existing_version_id is not None:
        raise BusinessError(
            "IMPORT_ALREADY_PUBLISHED",
            "该导入作业已经产生过场景版本，不能再改写场景草稿",
            status_code=409,
            details={"job_no": job.job_no},
        )
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    if not job.scenario_code:
        raise BusinessError(
            "IMPORT_SCENARIO_REQUIRED",
            "导入作业没有所属场景，不能写入草稿。请先创建场景，再在该场景下重新上传",
            status_code=409,
            details={"job_no": job.job_no},
        )
    template = template_or_404(db, job.scenario_code, lock=True)
    job_schema = _job_schema(job)
    template_schema = scenario_schema.normalize_schema(template.schema_json)
    if job_schema != template_schema or job.schema_key != template.schema_key or job.schema_version != template.schema_version:
        raise BusinessError(
            "IMPORT_SCENARIO_SCHEMA_CHANGED",
            "场景导入协议已在上传后改变，请按当前场景重新导入规则库",
            status_code=409,
            details={"job_no": job.job_no, "scenario_code": job.scenario_code},
        )
    if job.status not in {"PROPOSED"}:
        raise BusinessError("IMPORT_STATUS_NOT_READY", "导入作业尚未成功完成规则转换", status_code=409)
    if not job.proposals_json:
        raise BusinessError("PROPOSALS_MISSING", "规则尚未转换", status_code=409)
    proposals = _saved_proposals(job)
    try:
        draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
        actual_revision = draft.revision_no if draft is not None else None
        if actual_revision != expected_scene_revision:
            raise BusinessError(
                "SCENARIO_DRAFT_REVISION_CONFLICT", "场景草稿在本次确认期间已修改，请重新核对后再写入",
                status_code=409,
                details={"scenario_code": template.scenario_code, "expected": expected_scene_revision, "actual": actual_revision},
            )
        draft = ensure_draft(db, template)
        if (
            draft.schema_json != job_schema
            or draft.schema_key != job.schema_key
            or draft.schema_version != job.schema_version
        ):
            raise BusinessError(
                "IMPORT_DRAFT_SCHEMA_MISMATCH",
                "场景草稿的导入协议与当前作业不一致，请重新导入规则库",
                status_code=409,
                details={"job_no": job.job_no, "scenario_code": job.scenario_code},
            )
        config = draft_config_of_import(
            proposals=proposals,
            namespace=template.scenario_code,
        )
        load_import_config_into_draft(db, draft, config, profile_version=job.profile_version)
        draft.config_json = {**draft.config_json, "import_proposals": job.proposals_json,
                             "import_job_no": job.job_no}
        draft.source = "EXCEL"
        latest = find_latest_published_version(db, template.scenario_code)
        if latest is not None and draft.base_version_no is None:
            draft.base_version_no = latest.version_no
        _bump_draft(draft)
        job.status = "APPLIED"
        record_operation(
            db,
            object_type=OBJECT_IMPORT_JOB,
            object_no=job.job_no,
            object_name=job.source_filename,
            operation_type="APPLY_DRAFT",
            detail={"job_no": job.job_no, "scenario_code": template.scenario_code},
        )
        record_operation(
            db,
            object_type=OBJECT_SCENARIO,
            object_no=template.scenario_code,
            object_name=template.scenario_name,
            operation_type="IMPORT_APPLY",
            detail={"job_no": job.job_no, "scenario_code": template.scenario_code},
        )
        _commit_config_change(db, template, draft)
    except Exception:
        db.rollback()
        raise
    db.refresh(job)
    return job
