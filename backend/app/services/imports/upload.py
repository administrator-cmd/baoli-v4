"""规则库文件上传与导入作业创建。"""

from pathlib import Path
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from ...config import get_settings
from ...common.exceptions import BusinessError
from ...entity import ImportJob
from ...entity import ScenarioTemplate
from ...domain.identity import public_no
from ...domain import scenario_schema, node_profile
from ..shared.storage import RULEBOOK_EXTENSIONS, resolve_storage_path, save_upload_sync
from .workbook import read_workbook_ir


def create_import_job(db: Session, upload: UploadFile, *, scenario_code: str) -> ImportJob:
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
    size, sha256 = save_upload_sync(
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
