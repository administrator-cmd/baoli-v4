"""生成作业上传：保存原始 Excel、冻结生成输入并建立作业。"""

import hashlib
import json
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.core.transactions import after_rollback
from app.domain.shared.identity import public_no
from app.domain.generation.document import (
    GENERATION_DOCUMENT_SCHEMA_VERSION,
    GENERATION_INPUT_MAX_LENGTH,
    GENERATION_PROMPT_VERSION,
    GENERATION_WORKBOOK_PROFILE,
)
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import RulebookGenerationJob
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService
from app.services.shared.commands import _idempotency_key
from app.services.shared.storage import storage_entry, RULEBOOK_EXTENSIONS, resolve_storage_path, save_upload_sync


class GenerationUploadService:
    """上传只保存原件与输入，不调用模型。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _request_hash(self, source_sha256: str, source_filename: str, business_background: str, generation_requirements: str) -> str:
        payload = {
            "source_sha256": source_sha256,
            "source_filename": source_filename,
            "business_background": business_background,
            "generation_requirements": generation_requirements,
            "document_schema_version": GENERATION_DOCUMENT_SCHEMA_VERSION,
            "prompt_version": GENERATION_PROMPT_VERSION,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        ).hexdigest()

    def _discard_unreferenced_source(self, job_no: str, relative_path: str) -> None:
        bind = self.db.get_bind()

        def discard() -> None:
            with Session(bind=bind) as verification:
                if crud.RulebookGenerationCRUD(verification).by_no(job_no) is None:
                    resolve_storage_path(relative_path).unlink(missing_ok=True)
                    # 目录为空时只删除本作业目录，不递归清理其他作业。
                    parent = resolve_storage_path(relative_path).parent
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()

        after_rollback(self.db, discard)

    def create_job(self,
        upload: UploadFile,
        *,
        business_background: str,
        generation_requirements: str,
        idempotency_key: str | None,
    ) -> tuple[RulebookGenerationJob, bool]:
        """保存上传并创建作业；同一幂等键同内容返回原作业，不同内容明确冲突。"""
        for label, value in (("业务背景", business_background), ("编制要求", generation_requirements)):
            if len(value) > GENERATION_INPUT_MAX_LENGTH:
                raise BusinessError("GENERATION_INPUT_TOO_LONG", f"{label}不能超过 {GENERATION_INPUT_MAX_LENGTH} 个字符", status_code=422)
        settings = get_settings()
        key = _idempotency_key(idempotency_key)
        suffix = Path(upload.filename or "").suffix.lower()
        staging_relative = f"rulebook_generations/.staging/{uuid.uuid4().hex}{suffix}"
        size, sha256 = save_upload_sync(
            upload,
            relative_path=staging_relative,
            allowed_extensions=RULEBOOK_EXTENSIONS,
            max_bytes=settings.max_rulebook_bytes,
        )
        staging_path = resolve_storage_path(staging_relative)
        request_hash = self._request_hash(
            sha256, upload.filename or "", business_background.strip(), generation_requirements.strip(),
        )
        try:
            return self._create_saved_job(
                key=key, request_hash=request_hash, staging_path=staging_path,
                source_filename=upload.filename or f"规则库{suffix}", suffix=suffix,
                size=size, sha256=sha256, business_background=business_background.strip(),
                generation_requirements=generation_requirements.strip(),
            )
        finally:
            staging_path.unlink(missing_ok=True)

    def reuse_source(self, source: RulebookGenerationJob) -> tuple[RulebookGenerationJob, bool]:
        """以原件和原输入创建当前版本作业，原记录不变；调用方须先锁定原作业。

        同一原作业在同一编制版本下共用一个目标，刷新或多页面点击不会重复建作业。
        """
        request_hash = self._request_hash(
            source.source_sha256, source.source_filename,
            source.business_background, source.generation_requirements,
        )
        key = "generation-retry:" + hashlib.sha256(
            f"{source.job_no}:{request_hash}".encode("utf-8"),
        ).hexdigest()
        existing = crud.RulebookGenerationCRUD(self.db).by_idempotency_key(key, lock=True)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise BusinessError(
                    "GENERATION_UPLOAD_IDEMPOTENCY_CONFLICT",
                    "已有重试记录的输入与原记录不一致，无法复用", status_code=409,
                )
            return existing, True

        source_path = resolve_storage_path(source.source_path)
        suffix = Path(source.source_filename).suffix.lower()
        staging_relative = f"rulebook_generations/.staging/{uuid.uuid4().hex}{suffix}"
        staging_path = resolve_storage_path(staging_relative)
        try:
            try:
                with source_path.open("rb") as stream:
                    size, sha256 = save_upload_sync(
                        UploadFile(file=stream, filename=source.source_filename),
                        relative_path=staging_relative,
                        allowed_extensions=RULEBOOK_EXTENSIONS,
                        max_bytes=get_settings().max_rulebook_bytes,
                    )
            except FileNotFoundError as exc:
                raise BusinessError(
                    "GENERATION_SOURCE_FILE_MISSING",
                    "原始 Excel 已不存在，请重新上传文件后编制", status_code=404,
                    details={"job_no": source.job_no},
                ) from exc
            if size != source.source_size or sha256 != source.source_sha256:
                raise BusinessError(
                    "GENERATION_SOURCE_FILE_CHANGED",
                    "原始 Excel 与上传时不一致，请重新上传文件后编制", status_code=409,
                    details={"job_no": source.job_no},
                )
            return self._create_saved_job(
                key=key, request_hash=request_hash, staging_path=staging_path,
                source_filename=source.source_filename, suffix=suffix, size=size, sha256=sha256,
                business_background=source.business_background,
                generation_requirements=source.generation_requirements,
            )
        finally:
            staging_path.unlink(missing_ok=True)

    def _create_saved_job(
        self, *, key: str, request_hash: str, staging_path: Path, source_filename: str,
        suffix: str, size: int, sha256: str, business_background: str,
        generation_requirements: str,
    ) -> tuple[RulebookGenerationJob, bool]:
        existing = crud.RulebookGenerationCRUD(self.db).by_idempotency_key(key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise BusinessError(
                    "GENERATION_UPLOAD_IDEMPOTENCY_CONFLICT",
                    "同一上传幂等标识已用于不同的文件或生成输入，拒绝复用已有作业",
                    status_code=409,
                    details={"job_no": existing.job_no},
                )
            return existing, True

        job_no = public_no("GEN")
        relative_path = f"rulebook_generations/{storage_entry('generation', job_no)}/source{suffix}"
        target = resolve_storage_path(relative_path)
        try:
            self._validate_workbook(staging_path, source_filename, size, sha256)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging_path.replace(target)
        except Exception:
            staging_path.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        self._discard_unreferenced_source(job_no, relative_path)

        job = RulebookGenerationJob(
            job_no=job_no,
            idempotency_key=key,
            request_hash=request_hash,
            source_filename=source_filename,
            source_path=relative_path,
            source_sha256=sha256,
            source_size=size,
            business_background=business_background.strip(),
            generation_requirements=generation_requirements.strip(),
            document_schema_version=GENERATION_DOCUMENT_SCHEMA_VERSION,
            prompt_version=GENERATION_PROMPT_VERSION,
            document_json=None,
            revision=0,
        )
        try:
            with self.db.begin_nested():
                crud.RulebookGenerationCRUD(self.db).add(job)
        except IntegrityError as exc:
            # 并发同键同内容：唯一约束兜底，返回先落库的原作业。
            # 唯一键竞争结束后用当前读；普通快照读可能仍看不到另一事务刚提交的行。
            existing = crud.RulebookGenerationCRUD(self.db).by_idempotency_key(key, lock=True)
            target.unlink(missing_ok=True)
            if existing is None:
                raise
            if existing.request_hash == request_hash:
                return existing, True
            raise BusinessError(
                "GENERATION_UPLOAD_IDEMPOTENCY_CONFLICT",
                "同一上传幂等标识已用于不同的文件或生成输入，拒绝复用已有作业",
                status_code=409,
                details={"idempotency_key": key},
            ) from exc
        crud.RulebookGenerationCRUD(self.db).refresh(job)
        return job, False

    def _validate_workbook(
        self, path: Path, source_filename: str, size: int, sha256: str,
    ) -> None:
        """上传即核对文件真实可读，避免把损坏文件保存成可生成作业。"""
        workbook_ir = ImportsJobWorkbookService().read_workbook_ir(
            path,
            workbook_id=public_no("wb"),
            source_filename=source_filename,
            source_size=size,
            source_sha256=sha256,
            profile_version=GENERATION_WORKBOOK_PROFILE,
        )
        if not any(sheet.state != "EMPTY" for sheet in workbook_ir.sheets):
            raise BusinessError(
                "GENERATION_WORKBOOK_EMPTY", "Excel 中没有非空工作表，无法编制规则库",
                status_code=422, details={"filename": source_filename},
            )
