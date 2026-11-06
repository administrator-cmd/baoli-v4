"""规则库文件上传与导入作业创建。"""

import shutil
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.core.transactions import after_rollback
from app.domain import scenario_schema
from app.domain.identity import public_no
from app.modules.imports.job import crud
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD
from app.services.shared.storage import RULEBOOK_EXTENSIONS, resolve_storage_path, save_upload_sync


class ImportsJobUploadService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _scenario_schema(self, scenario_code: str) -> dict[str, Any]:
        """场景必须真实存在且未删除，schema 身份必须与快照一致。"""
        template = ScenarioTemplateCRUD(self.db).template_by_code(scenario_code)
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
        return schema

    def _discard_unreferenced_source(self, job_no: str, relative_path: str) -> None:
        """请求回滚后核对真实引用再删除本次保存的文件。"""
        bind = self.db.get_bind()

        def discard_unreferenced_source() -> None:
            with Session(bind=bind) as verification:
                if crud.ImportsJobCRUD(verification).by_no(job_no) is None:
                    resolve_storage_path(relative_path).unlink(missing_ok=True)

        after_rollback(self.db, discard_unreferenced_source)

    def _create_job_from_saved_source(self, *,
        job_no: str,
        source_filename: str,
        relative_path: str,
        size: int,
        sha256: str,
        scenario_code: str,
        schema: dict[str, Any],
    ) -> ImportJob:
        """文件已在最终位置；读取工作簿 IR 并建立作业。"""
        workbook_ir = ImportsJobWorkbookService().read_workbook_ir(
            resolve_storage_path(relative_path),
            workbook_id=public_no("wb"),
            source_filename=source_filename,
            source_size=size,
            source_sha256=sha256,
            profile_version=schema["profile_version"],
            scenario_code=scenario_code,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
        )
        job = ImportJob(
            job_no=job_no,
            source_filename=source_filename,
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
            status="PARSED",
        )
        crud.ImportsJobCRUD(self.db).add(job)
        crud.ImportsJobCRUD(self.db).flush()
        crud.ImportsJobCRUD(self.db).refresh(job)
        return job

    def create_import_job(self, upload: UploadFile, *, scenario_code: str) -> ImportJob:
        """在指定场景下上传规则库、读取工作簿并建立导入作业。

        `scenario_code` 是必填的：上传流程以场景为起点（用户先建场景、再在该场景下上传），
        场景不由 Excel 表头推断；解析使用该场景的实际节点与能力目录。
        """
        settings = get_settings()
        schema = self._scenario_schema(scenario_code)
        job_no = public_no("IMP")
        suffix = Path(upload.filename or "").suffix.lower()
        relative_path = f"imports/{job_no}/source{suffix}"
        size, sha256 = save_upload_sync(
            upload,
            relative_path=relative_path,
            allowed_extensions=RULEBOOK_EXTENSIONS,
            max_bytes=settings.max_rulebook_bytes,
        )
        self._discard_unreferenced_source(job_no, relative_path)
        return self._create_job_from_saved_source(
            job_no=job_no,
            source_filename=upload.filename or f"规则库{suffix}",
            relative_path=relative_path,
            size=size,
            sha256=sha256,
            scenario_code=scenario_code,
            schema=schema,
        )

    def create_import_job_from_file(self, source_path: Path, *, source_filename: str, scenario_code: str) -> ImportJob:
        """把已冻结的规则库文件复制为本作业自己的原件并建立导入作业。

        生成记录与导入作业各自拥有文件副本：删除导入原件不影响生成作业的源文件和导出。
        """
        settings = get_settings()
        schema = self._scenario_schema(scenario_code)
        suffix = Path(source_filename).suffix.lower()
        if suffix not in RULEBOOK_EXTENSIONS:
            raise BusinessError(
                "FILE_TYPE_UNSUPPORTED", "文件格式不受支持",
                details={"filename": source_filename, "allowed_extensions": sorted(RULEBOOK_EXTENSIONS)},
            )
        if not source_path.is_file():
            raise BusinessError(
                "GENERATION_EXPORT_FILE_MISSING", "待导入的规则库导出文件不在存储目录中", status_code=404,
                details={"path": str(source_path)},
            )
        job_no = public_no("IMP")
        relative_path = f"imports/{job_no}/source{suffix}"
        target = resolve_storage_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source_path, target)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise BusinessError(
                "GENERATION_EXPORT_FILE_MISSING", "待导入的规则库导出文件无法复制", status_code=409,
                details={"path": str(source_path)},
            ) from exc
        self._discard_unreferenced_source(job_no, relative_path)
        size = target.stat().st_size
        if size > settings.max_rulebook_bytes:
            target.unlink(missing_ok=True)
            raise BusinessError(
                "FILE_TOO_LARGE", "文件超过允许大小", details={"max_bytes": settings.max_rulebook_bytes},
            )
        if size == 0:
            target.unlink(missing_ok=True)
            raise BusinessError("FILE_EMPTY", "待导入的规则库导出文件为空")
        sha256 = ImportsJobWorkbookService().file_sha256(target)
        return self._create_job_from_saved_source(
            job_no=job_no,
            source_filename=source_filename,
            relative_path=relative_path,
            size=size,
            sha256=sha256,
            scenario_code=scenario_code,
            schema=schema,
        )
