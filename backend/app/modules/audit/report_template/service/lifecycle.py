"""报告模板上传、风格分析操作受理与档案维护。"""

import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.core.transactions import after_rollback
from app.domain.identity import public_no
from app.modules.audit.report_template.crud import ReportTemplateCRUD
from app.modules.audit.report_template.model import ReportTemplate
from app.modules.audit.report_template.service.analysis import validate_style_profile
from app.modules.audit.report_template.service.operations import (
    OPERATION_ANALYZE_REPORT_TEMPLATE,
    ReportTemplateAnalysisOperations,
)
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_REPORT_TEMPLATE
from app.services.shared.storage import resolve_storage_path, save_upload_sync


class ReportTemplateLifecycleService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _discard_unreferenced_source(self, template_no: str, relative_path: str) -> None:
        bind = self.db.get_bind()

        def discard() -> None:
            with Session(bind=bind) as verification:
                if ReportTemplateCRUD(verification).by_no(template_no) is None:
                    source_path = resolve_storage_path(relative_path)
                    source_path.unlink(missing_ok=True)
                    parent = source_path.parent
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()

        after_rollback(self.db, discard)

    def _remove_source_file(self, source_path: Path) -> None:
        source_path.unlink(missing_ok=True)
        parent = source_path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def _existing_upload(
        self, *, key: str, filename: str, size: int, sha256: str, lock: bool = False,
    ) -> tuple[ReportTemplate, BackgroundOperation, bool] | None:
        template = ReportTemplateCRUD(self.db).by_upload_idempotency_key(key, lock=lock)
        if template is None:
            return None
        if (
            template.source_filename != filename
            or template.source_size != size
            or template.source_sha256 != sha256
        ):
            raise BusinessError(
                "REPORT_TEMPLATE_UPLOAD_IDEMPOTENCY_CONFLICT",
                "同一上传请求标识已用于不同的 PDF 文件",
                status_code=409,
            )
        operation = OperationBackgroundService(self.db).existing_operation(
            object_type=OBJECT_REPORT_TEMPLATE,
            object_no=template.template_no,
            operation_type=OPERATION_ANALYZE_REPORT_TEMPLATE,
            idempotency_key=f"upload-analysis:{template.template_no}",
        )
        if operation is None:
            raise BusinessError(
                "REPORT_TEMPLATE_ANALYSIS_OPERATION_MISSING", "模板记录存在，但未找到对应的分析操作",
                status_code=500,
            )
        return template, operation, True

    def upload(
        self, upload: UploadFile, *, name: str | None, idempotency_key: str,
    ) -> tuple[ReportTemplate, BackgroundOperation, bool]:
        key = idempotency_key.strip()
        if not key:
            raise BusinessError("REPORT_TEMPLATE_UPLOAD_IDEMPOTENCY_REQUIRED", "上传请求缺少幂等标识", status_code=422)
        suffix = Path(upload.filename or "").suffix.lower()
        source_filename = (upload.filename or f"报告模板{suffix}")[:255]
        normalized_name = (name or Path(upload.filename or "报告模板").stem).strip()[:255] or "报告模板"
        staging_relative = f"report_templates/.staging/{uuid.uuid4().hex}{suffix}"
        staging_path = resolve_storage_path(staging_relative)
        size, sha256 = save_upload_sync(
            upload,
            relative_path=staging_relative,
            allowed_extensions={".pdf"},
            max_bytes=get_settings().max_document_bytes,
        )
        try:
            existing = self._existing_upload(
                key=key, filename=source_filename, size=size, sha256=sha256,
            )
            if existing is not None:
                return existing

            template_no = public_no("RPTPL")
            relative_path = f"report_templates/{template_no}/source.pdf"
            source_path = resolve_storage_path(relative_path)
            source_path.parent.mkdir(parents=True, exist_ok=True)
            staging_path.replace(source_path)
            self._discard_unreferenced_source(template_no, relative_path)
            template = ReportTemplate(
                template_no=template_no,
                upload_idempotency_key=key,
                name=normalized_name,
                source_filename=source_filename,
                source_path=relative_path,
                source_size=size,
                source_sha256=sha256,
                status="PROCESSING",
            )
            try:
                with self.db.begin_nested():
                    ReportTemplateCRUD(self.db).add(template)
            except IntegrityError:
                self._remove_source_file(source_path)
                existing = self._existing_upload(
                    key=key, filename=source_filename, size=size, sha256=sha256, lock=True,
                )
                if existing is not None:
                    return existing
                raise
            locked_template = ReportTemplateCRUD(self.db).lock_by_no(template_no)
            if locked_template is None:
                raise BusinessError("REPORT_TEMPLATE_NOT_FOUND", "报告模板创建失败", status_code=500)
            operation, reused = ReportTemplateAnalysisOperations(self.db).submit(
                locked_template, idempotency_key=f"upload-analysis:{template_no}",
            )
            ReportTemplateCRUD(self.db).refresh(locked_template)
            return locked_template, operation, reused
        finally:
            staging_path.unlink(missing_ok=True)

    def template_or_404(self, template_no: str, *, lock: bool = False) -> ReportTemplate:
        crud = ReportTemplateCRUD(self.db)
        template = crud.lock_by_no(template_no) if lock else crud.by_no(template_no)
        if template is None:
            raise BusinessError("REPORT_TEMPLATE_NOT_FOUND", "报告模板不存在", status_code=404)
        return template

    def update(self, template_no: str, *, name: str, style_profile: dict | None = None) -> ReportTemplate:
        template = self.template_or_404(template_no, lock=True)
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_REPORT_TEMPLATE, template_no)
        normalized_name = name.strip()
        if not normalized_name:
            raise BusinessError("REPORT_TEMPLATE_NAME_REQUIRED", "报告模板名称不能为空", status_code=422)
        validated_profile = validate_style_profile(style_profile) if style_profile is not None else None
        template.name = normalized_name
        if validated_profile is not None:
            template.style_profile_json = validated_profile
            template.status = "READY"
            template.analysis_error = None
        ReportTemplateCRUD(self.db).flush()
        ReportTemplateCRUD(self.db).refresh(template)
        return template

    def reanalyze(
        self, template_no: str, *, idempotency_key: str,
    ) -> tuple[ReportTemplate, BackgroundOperation, bool]:
        template = self.template_or_404(template_no, lock=True)
        operation, reused = ReportTemplateAnalysisOperations(self.db).submit(
            template, idempotency_key=idempotency_key,
        )
        ReportTemplateCRUD(self.db).flush()
        ReportTemplateCRUD(self.db).refresh(template)
        return template, operation, reused
