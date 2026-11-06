"""生成后台工作单元：读取冻结输入、调用模型、再锁定作业提交结果。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.transactions import commit_transaction, rollback_transaction
from app.domain.identity import public_no
from app.domain.rulebook_generation import (
    GENERATION_DOCUMENT_SCHEMA_VERSION,
    GENERATION_PROMPT_VERSION,
    GENERATION_WORKBOOK_PROFILE,
)
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import RulebookGenerationJob
from app.modules.imports.generation.service.llm import GenerationLlmService
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION
from app.services.shared.storage import resolve_storage_path


class GenerationExecution:
    """模型调用前结束事务释放行锁；返回后重新锁定并核对输入当前性。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _job_for_write(
        self, job_no: str, operation_no: str, *, content: dict[str, Any],
    ) -> RulebookGenerationJob:
        job = crud.RulebookGenerationCRUD(self.db).by_no(job_no, lock=True)
        if job is None:
            raise BusinessError(
                "GENERATION_JOB_NOT_FOUND", "生成作业不存在", status_code=404, details={"job_no": job_no},
            )
        if (
            job.document_schema_version != GENERATION_DOCUMENT_SCHEMA_VERSION
            or job.prompt_version != GENERATION_PROMPT_VERSION
        ):
            raise BusinessError(
                "GENERATION_VERSION_UNSUPPORTED",
                "该作业冻结的编制或提示词版本与当前程序不一致，拒绝使用其他版本执行",
                status_code=409,
                details={"job_no": job_no, "document_schema_version": job.document_schema_version,
                         "prompt_version": job.prompt_version},
            )
        operation = SystemOperationCRUD(self.db).lock_by_operation_no(
            operation_no, object_type=OBJECT_RULEBOOK_GENERATION, object_no=job_no,
        )
        if operation is None:
            raise BusinessError("OPERATION_NOT_FOUND", "生成操作不存在", status_code=404)
        if operation.status != "RUNNING":
            raise BusinessError(
                "OPERATION_NOT_ACTIVE", "生成操作已结束或被中止，拒绝写入旧结果", status_code=409,
                details={"operation_no": operation_no, "status": operation.status},
            )
        if (
            job.source_sha256 != content.get("source_sha256")
            or job.request_hash != content.get("request_hash")
            or job.prompt_version != content.get("prompt_version")
            or job.document_schema_version != content.get("document_schema_version")
            or job.revision != content.get("revision")
            or job.document_json is not None
        ):
            raise BusinessError(
                "GENERATION_INPUT_STALE",
                "生成输入已变化，拒绝按过期命令写入结果",
                status_code=409,
                details={"job_no": job_no},
            )
        return job

    def generate(self, job_no: str, *, operation_no: str, content: dict[str, Any]) -> RulebookGenerationJob:
        """读取输入后释放事务，模型返回时再锁定作业并提交完整文档。"""
        job = self._job_for_write(job_no, operation_no, content=content)
        source_path = job.source_path
        source_filename = job.source_filename
        source_size = job.source_size
        source_sha256 = job.source_sha256
        business_background = job.business_background
        generation_requirements = job.generation_requirements
        try:
            workbook_ir = ImportsJobWorkbookService().read_workbook_ir(
                resolve_storage_path(source_path),
                workbook_id=public_no("wb"),
                source_filename=source_filename,
                source_size=source_size,
                source_sha256=source_sha256,
                profile_version=GENERATION_WORKBOOK_PROFILE,
            )
            # 到此只有本地读取：显式结束事务并释放行锁，模型返回前不持有任何锁。
            rollback_transaction(self.db)
            document = GenerationLlmService().build_document(
                workbook_ir,
                business_background=business_background,
                generation_requirements=generation_requirements,
                operation_no=operation_no,
                job_no=job_no,
            )
        except Exception:
            rollback_transaction(self.db)
            raise
        job = self._job_for_write(job_no, operation_no, content=content)
        job.document_json = document
        job.revision = job.revision + 1
        GenerationHistoryService(self.db).snapshot(job, origin="INITIAL", summary=document.get("summary") or "AI 首次整理编制")
        commit_transaction(self.db)
        return job
