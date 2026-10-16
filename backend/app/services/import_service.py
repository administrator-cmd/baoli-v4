from datetime import UTC, datetime
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..repositories.scenario_repository import find_latest_published_version
from ..config import get_settings
from ..domain.manifest import (
    draft_config_of_import,
    rule_comparison_view,
)
from ..common.exceptions import BusinessError
from ..entity import ImportJob
from ..entity import (
    BackgroundOperation,
    ScenarioTemplate,
    ScenarioVersion,
    ScenarioDraft,
)
from .background_service import ACTIVE_STATUSES, assert_no_active_operation, latest_operation, operation_view
from ..domain.identity import public_no
from .operation_types import OBJECT_IMPORT_JOB
from .operation_log_service import record_operation
from ..domain import scenario_schema, node_profile
from .storage_service import RULEBOOK_EXTENSIONS, resolve_storage_path, save_upload
from .llm_service import convert_workbook
from .workbook_service import read_workbook_ir


# 工作表不参与解析时的状态值，保存选择与转换共用。
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




async def create_import_job(db: Session, upload: UploadFile, *, scenario_code: str) -> ImportJob:
    """在指定场景下上传规则库、读取工作簿并建立导入作业。

    `scenario_code` 是必填的：上传流程以场景为起点（用户先建场景、再在该场景下上传），
    场景不由 Excel 表头推断；解析使用该场景的实际节点与能力目录。

    场景必须真实存在且未删除：写一个不存在的场景编号会让作业挂在一个查不到的场景上。
    """
    settings = get_settings()
    # 场景必须真实存在且未删除：写一个不存在的场景编号会让作业挂在一个查不到的场景上。
    template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))
    if template is None:
        raise BusinessError(
            "IMPORT_SCENARIO_NOT_FOUND",
            "导入作业必须属于一个已创建的场景，请先在场景管理里建立场景再上传",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    if not template.enabled:
        raise BusinessError(
            "IMPORT_SCENARIO_DISABLED",
            f"场景“{template.scenario_name}”已停用，不能在其下新建导入作业",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    schema = scenario_schema.normalize_schema(template.schema_json)
    if template.schema_key != schema["schema_key"] or template.schema_version != schema["schema_version"]:
        raise BusinessError(
            "SCENARIO_SCHEMA_MISMATCH",
            "场景 schema 身份与保存的配置不一致，不能导入",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    job_no = public_no("IMP")
    suffix = Path(upload.filename or "").suffix.lower()
    relative_path = f"imports/{job_no}/source{suffix}"
    size, sha256 = await save_upload(
        upload,
        relative_path=relative_path,
        allowed_extensions=RULEBOOK_EXTENSIONS,
        max_bytes=settings.max_rulebook_bytes,
    )
    try:
        workbook_ir = read_workbook_ir(
            resolve_storage_path(relative_path),
            workbook_id=public_no("wb"),
            source_filename=upload.filename or f"规则库{suffix}",
            source_size=size,
            source_sha256=sha256,
            profile_version=schema["profile_version"],
            scenario_code=scenario_code,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
        )
        job = ImportJob(
            job_no=job_no,
            source_filename=upload.filename or f"规则库{suffix}",
            source_path=relative_path,
            source_sha256=sha256,
            source_size=size,
            # 记录当前执行契约版本，不约束上传文件的业务表头。
            profile_version=workbook_ir.profile_version,
            # 上传入口的场景身份：作业明确属于哪个场景，不从 Excel 内容猜。
            scenario_code=scenario_code,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
            schema_json=schema,
            profile_display_version=node_profile.NODE_PROFILE_VERSION,
            status="PARSED",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except Exception:
        db.rollback()
        raise


def get_import_job(db: Session, job_no: str) -> ImportJob:
    """按作业编号取导入作业；取不到即 404。"""
    return _job_or_404(db, job_no)


def _saved_proposals(job: ImportJob) -> dict[str, Any]:
    """最终模型结果入库前已解析，读取时只确认有 JSON 对象。"""
    if not isinstance(job.proposals_json, dict):
        raise BusinessError("IMPORT_PROPOSALS_INVALID", "导入作业没有已保存的配置 JSON", status_code=409,
                            details={"job_no": job.job_no})
    return job.proposals_json


def update_import_proposal(
    db: Session, job_no: str, *, group: str, candidate_ref: str,
    expected_job_revision: int, changes: dict[str, Any],
) -> ImportJob:
    """修改一项已解析业务配置，保留其身份、依赖和未编辑属性。"""
    editable_attributes = {
        "materials": {"name", "description", "required"},
        "fields": {"name", "instruction", "required", "config_status"},
        "rules": {"name", "requirement", "rule_role", "config_status"},
        "calculations": {"rule_name", "formula", "requirement", "status"},
        "report_sections": {"section_name", "content_instruction", "config_status"},
    }
    job = _job_or_404(db, job_no, lock=True)
    _ensure_import_mutable(db, job)
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    if job.draft_revision != expected_job_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT", "解析结果已修改，请刷新后再保存", status_code=409,
            details={"expected": expected_job_revision, "actual": job.draft_revision},
        )
    unsupported = changes.keys() - editable_attributes[group]
    if unsupported:
        raise BusinessError("IMPORT_PROPOSAL_ATTRIBUTE_INVALID", "提交的属性不属于这类解析配置",
                            details={"group": group, "attributes": sorted(unsupported)})
    proposals = deepcopy(_saved_proposals(job))
    item = next((item for item in proposals[group] if item["candidate_ref"] == candidate_ref), None)
    if item is None:
        raise BusinessError("IMPORT_PROPOSAL_NOT_FOUND", "解析结果中不存在该项配置", status_code=404,
                            details={"group": group, "candidate_ref": candidate_ref})
    item.update(changes)
    if group == "rules" and changes.get("rule_role") == "CHECK" and item.get("operator") == "RISK_TRIGGER":
        # 管理员明确改为普通核查后，不能仍被旧风险动作识别为风险规则。
        item["operator"] = "LLM_EXECUTE"
    job.proposals_json = proposals
    job.draft_revision += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(job)
    return job


def _proposal_business_view(job: ImportJob) -> dict[str, Any] | None:
    """直接展示模型的业务配置，不要求来源坐标。"""
    if job.proposals_json is None:
        return None
    proposals = _saved_proposals(job)
    material_names = {item["candidate_ref"]: item["name"] for item in proposals["materials"]}
    field_labels = {item["candidate_ref"]: item["name"] for item in proposals["fields"]}
    rule_names = {item["candidate_ref"]: item["name"] for item in proposals["rules"]}
    field_meta = {item["candidate_ref"]: item for item in proposals["fields"]}
    calculation_meta = {item["result_code"]: item for item in proposals["calculations"]}
    return {
        "materials": proposals["materials"],
        "fields": [
            {**item, "material_name": material_names.get(item["material_ref"])}
            for item in proposals["fields"]
        ],
        "rules": [
            {**item, "comparison": rule_comparison_view(
                item, material_names, field_labels, rule_names, field_meta,
                calculation_meta=calculation_meta,
            )}
            for item in proposals["rules"]
        ],
        "calculations": proposals["calculations"],
        "report_sections": proposals["report_sections"],
    }


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
    """导入作业的业务视图：二轮整理结果、修订号与最新后台操作。"""
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
        "failure": {"code": job.failure_code, "message": job.failure_message} if job.failure_code else None,
        "latest_operation": operation_view(latest) if (latest := latest_operation(db, OBJECT_IMPORT_JOB, job.job_no)) else None,
        "created_at": job.created_at,
    }


def abort_import_job(db: Session, job_no: str) -> BackgroundOperation:
    """中止该导入作业尚未结束的后台操作。

    只把命令本身标成终态 INTERRUPTED 并释放活跃占用，让作业回到可重试 / 可删除的状态。这是
    「释放占用」而不是「回滚」：作业状态与草稿数据一概不改，重试时由 retry_conversion 按自己
    的草稿修订整体重建。没有活跃操作时明确报错，不静默成功。

    不强杀正在等待模型的线程；模型返回后写入前必须重新检查命令状态，中止结果不能覆盖草稿。
    模型调用期间不持有数据库事务。中止与转换落库都按「作业、命令」顺序加锁，
    保证中止提交以后，旧转换不能再写入候选或覆盖中止状态。
    """
    job = _job_or_404(db, job_no, lock=True)
    operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_type == OBJECT_IMPORT_JOB,
            BackgroundOperation.object_no == job_no,
            BackgroundOperation.status.in_(ACTIVE_STATUSES),
        )
        .order_by(BackgroundOperation.id.desc())
        .with_for_update()
    )
    if operation is None:
        raise BusinessError(
            "OPERATION_NOT_ACTIVE",
            "该导入作业没有正在进行的操作，无需中止",
            status_code=409,
            details={"job_no": job_no},
        )
    previous_status = operation.status
    operation.status = "INTERRUPTED"
    operation.failure_code = "OPERATION_ABORTED"
    operation.failure_message = "该操作已被人工中止，可重新发起转换"
    operation.finished_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_IMPORT_JOB,
        object_no=job_no,
        object_name=job.source_filename,
        operation_type="ABORT",
        detail={"job_no": job_no, "operation_no": operation.operation_no, "previous_status": previous_status},
    )
    db.commit()
    db.refresh(operation)
    return operation


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
            "LLM_EXTERNAL_FAILED",
        } else "CONTRACT_INVALID"
        job.failure_code = exc.code
        job.failure_message = exc.message
        db.commit()
        raise














def apply_import_to_draft(
    db: Session, job_no: str, *, expected_job_revision: int, expected_scene_revision: int | None
) -> ImportJob:
    """把解析结果写入场景草稿，不生成或发布场景版本。

    不创建场景：场景身份以上传时记录的 scenario_code 为准，不从文件名、Sheet 名或表头另找。
    写入替换材料、字段、规则、报告与计算定义。
    写入前核对草稿修订，执行时由 LLM 按生效规则处理。
    """
    from .operation_types import OBJECT_SCENARIO
    from .scenario_admin_service import (
        _bump_draft,
        _commit_config_change,
        ensure_draft,
        load_import_config_into_draft,
        template_or_404,
    )

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
