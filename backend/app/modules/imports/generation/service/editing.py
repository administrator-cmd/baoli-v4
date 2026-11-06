"""业务文档的人工保存：版本冲突、表格结构与来源标记。"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.identity import public_no
from app.domain.rulebook_generation import (
    GENERATION_WORKBOOK_PROFILE,
    GenerationDocument,
    collect_document_problems,
    mark_user_edits,
    normalize_business_codes,
    summarize_generation_problems,
)
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import RulebookGenerationJob
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION
from app.services.shared.storage import resolve_storage_path


class GenerationEditingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def source_workbook(self, job_no: str):
        job = GenerationQueryService(self.db).get_job(job_no)
        path = resolve_storage_path(job.source_path)
        if not path.is_file():
            raise BusinessError(
                "GENERATION_SOURCE_FILE_MISSING", "原始 Excel 已不在存储目录中，无法核对来源位置",
                status_code=404, details={"job_no": job_no},
            )
        return ImportsJobWorkbookService().read_workbook_ir(
            path,
            workbook_id=public_no("wb"),
            source_filename=job.source_filename,
            source_size=job.source_size,
            source_sha256=job.source_sha256,
            profile_version=GENERATION_WORKBOOK_PROFILE,
        )

    def _source_rows(self, job_no: str) -> set[tuple[str, int]]:
        workbook_ir = self.source_workbook(job_no)
        return {
            (sheet.name, row.row_no)
            for sheet in workbook_ir.sheets for row in sheet.rows
        }

    def save_document(
        self, job_no: str, *, expected_revision: int, document_payload: dict[str, Any],
    ) -> RulebookGenerationJob:
        """保存完整编辑文档；旧页面保存明确冲突，不最后写入者覆盖。"""
        job = GenerationQueryService(self.db).get_job(job_no, lock=True)
        if job.document_json is None:
            raise BusinessError(
                "GENERATION_DOCUMENT_MISSING", "该作业还没有生成结果，不能编辑", status_code=409,
                details={"job_no": job_no},
            )
        if job.revision != expected_revision:
            raise BusinessError(
                "GENERATION_REVISION_CONFLICT",
                "编制文档已被其他保存更新，当前修订与页面提交的修订不一致，请重新读取后再修改",
                status_code=409,
                details={"job_no": job_no, "current_revision": job.revision,
                         "expected_revision": expected_revision},
            )
        OperationBackgroundService(self.db).assert_no_active_operation(
            OBJECT_RULEBOOK_GENERATION, job_no,
        )
        try:
            document = GenerationDocument.model_validate(document_payload)
        except ValidationError as exc:
            raise BusinessError(
                "GENERATION_DOCUMENT_INVALID", "提交的编制文档结构不完整", status_code=400,
                details={"mismatches": exc.errors(
                    include_url=False, include_input=False, include_context=False,
                )},
            ) from exc
        previous = GenerationQueryService(self.db).document(job)
        if [item.model_dump(mode="json") for item in previous.requirements] != [
            item.model_dump(mode="json") for item in document.requirements
        ]:
            raise BusinessError(
                "GENERATION_REQUIREMENTS_READONLY",
                "原文要求目录用于溯源，不能在编辑中改写；请在业务表格中修改正文，或通过待明确事项记录补充",
                status_code=409,
                details={"job_no": job_no},
            )
        document = normalize_business_codes(document, previous)
        document = mark_user_edits(previous, document)
        problems = collect_document_problems(document, self._source_rows(job_no))
        if problems:
            raise BusinessError(
                "GENERATION_DOCUMENT_INVALID",
                "提交的业务表格存在栏目、来源或条目关联问题："
                + summarize_generation_problems(problems),
                status_code=400,
                details={"problems": problems[:30], "count": len(problems)},
            )
        if document.model_dump(mode="json") == previous.model_dump(mode="json"):
            return job
        history = GenerationHistoryService(self.db)
        history.snapshot(job, origin="BASELINE", summary="启用修订记录时的已保存版本")
        job.document_json = document.model_dump(mode="json")
        job.revision = job.revision + 1
        history.snapshot(job, origin="MANUAL", summary="人工修改业务规则库")
        crud.RulebookGenerationCRUD(self.db).flush()
        crud.RulebookGenerationCRUD(self.db).refresh(job)
        return job
