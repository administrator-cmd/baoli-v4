"""生成作业列表、详情与导出视图的组装。"""

from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.pagination import PageResult, page_result
from app.constants.status import ACTIVE_OPERATION_STATUSES
from app.domain.generation.document import (
    GENERATION_DOCUMENT_SCHEMA_VERSION,
    GENERATION_PROMPT_VERSION,
    GENERATION_STATUS_EDITABLE,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_INTERRUPTED,
    GENERATION_STATUS_NO_DEFINITIONS,
    GENERATION_STATUS_NOT_GENERATED,
    GENERATION_STATUS_PENDING,
    GENERATION_STATUS_RUNNING,
    GENERATION_STATUS_UNKNOWN,
    GenerationDocument,
    document_entry_count,
    document_has_business_content,
)
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import (
    RulebookGenerationExport,
    RulebookGenerationImport,
    RulebookGenerationJob,
)
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION


def derive_status(
    *, has_document: bool, content_count: int, latest_operation_status: str | None,
) -> str:
    """列表筛选与逐行展示共用同一套推导。"""
    if latest_operation_status == "PENDING":
        return GENERATION_STATUS_PENDING
    if latest_operation_status == "RUNNING":
        return GENERATION_STATUS_RUNNING
    if not has_document:
        if latest_operation_status is None:
            return GENERATION_STATUS_NOT_GENERATED
        if latest_operation_status == "FAILED":
            return GENERATION_STATUS_FAILED
        if latest_operation_status == "INTERRUPTED":
            return GENERATION_STATUS_INTERRUPTED
        return GENERATION_STATUS_UNKNOWN
    return GENERATION_STATUS_EDITABLE if content_count > 0 else GENERATION_STATUS_NO_DEFINITIONS


def status_message(
    status: str, *, operation: BackgroundOperation | None, has_document: bool,
) -> str:
    if status == GENERATION_STATUS_NOT_GENERATED:
        return "尚未开始 AI 编制"
    if status == GENERATION_STATUS_PENDING:
        return "AI 编制已受理，等待后台执行"
    if status == GENERATION_STATUS_RUNNING:
        return "AI 正在整理业务规则库"
    if status == GENERATION_STATUS_EDITABLE:
        return "AI 编制完成，请核对业务内容；可修改并下载 Excel"
    if status == GENERATION_STATUS_NO_DEFINITIONS:
        return "AI 编制已结束，未识别到可编制的业务内容"
    if status == GENERATION_STATUS_FAILED:
        return operation.failure_message if operation and operation.failure_message else "生成失败"
    if status == GENERATION_STATUS_INTERRUPTED:
        return "生成操作被中断，可显式重新发起"
    if not has_document and operation and operation.status == "SUCCEEDED":
        return "生成操作已完成，但没有保存的编制文档，状态未知"
    return "生成状态未知"


class GenerationQueryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _latest_operation(self, job_no: str) -> BackgroundOperation | None:
        return OperationBackgroundService(self.db).latest_operation(OBJECT_RULEBOOK_GENERATION, job_no)

    def _operation_view(self, operation: BackgroundOperation | None) -> dict[str, Any] | None:
        if operation is None:
            return None
        return OperationBackgroundService().operation_view(operation)

    def get_job(self, job_no: str, *, lock: bool = False) -> RulebookGenerationJob:
        job = crud.RulebookGenerationCRUD(self.db).by_no(job_no, lock=lock)
        if job is None:
            raise BusinessError(
                "GENERATION_JOB_NOT_FOUND", "生成作业不存在", status_code=404, details={"job_no": job_no},
            )
        return job

    def document(self, job: RulebookGenerationJob) -> GenerationDocument:
        if job.document_json is None:
            raise BusinessError(
                "GENERATION_DOCUMENT_MISSING", "该作业还没有已保存的编制文档", status_code=409,
                details={"job_no": job.job_no, "revision": job.revision},
            )
        if job.document_schema_version != GENERATION_DOCUMENT_SCHEMA_VERSION:
            raise BusinessError(
                "GENERATION_DOCUMENT_VERSION_UNSUPPORTED",
                "此记录使用之前的系统配置编制格式，不能作为业务规则库继续编辑；原件和已有下载文件仍保留",
                status_code=409, details={"job_no": job.job_no},
            )
        try:
            return GenerationDocument.model_validate(job.document_json)
        except ValidationError as exc:
            raise BusinessError(
                "GENERATION_DOCUMENT_INVALID", "已保存的编制文档结构不完整，无法预览或编辑", status_code=409,
                details={"job_no": job.job_no, "mismatches": exc.errors(
                    include_url=False, include_input=False, include_context=False,
                )},
            ) from exc

    def list_page(self, *,
        filename: str | None,
        status: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> PageResult[dict[str, Any]]:
        rows, total = crud.RulebookGenerationCRUD(self.db).page_rows(
            filename=filename, status=status, created_from=created_from, created_to=created_to,
            page=page, page_size=page_size,
        )
        latest = crud.RulebookGenerationCRUD(self.db).latest_operations([row.job_no for row in rows])
        latest_exports = crud.RulebookGenerationExportCRUD(self.db).latest_exports([int(row.id) for row in rows])
        items = []
        for row in rows:
            operation = latest.get(row.job_no)
            operation_status = operation.status if operation is not None else row.latest_operation_status
            derived = derive_status(
                has_document=bool(row.has_document),
                content_count=int(row.content_count or 0),
                latest_operation_status=operation_status,
            )
            export = latest_exports.get(int(row.id))
            items.append({
                "job_no": row.job_no,
                "source_filename": row.source_filename,
                "source_size": row.source_size,
                "status": derived,
                "status_message": status_message(
                    derived, operation=operation, has_document=bool(row.has_document),
                ) if row.document_schema_version == GENERATION_DOCUMENT_SCHEMA_VERSION else "此前的编制格式，原始资料与已有文件仍保留",
                "revision": row.revision,
                "latest_operation": self._operation_view(operation),
                "latest_export": {
                    "export_no": export.export_no,
                    "revision": export.revision,
                    "filename": export.filename,
                } if export is not None else None,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            })
        return page_result(items, total, page, page_size)

    def job_view(self, job: RulebookGenerationJob, *, reused: bool = False) -> dict[str, Any]:
        operation = self._latest_operation(job.job_no)
        has_document = job.document_json is not None
        document_supported = job.document_schema_version == GENERATION_DOCUMENT_SCHEMA_VERSION
        document: GenerationDocument | None = None
        if has_document and document_supported:
            document = self.document(job)
        entry_count = document_entry_count(document) if document is not None else 0
        open_issues = sum(1 for issue in document.issues if issue.status == "OPEN") if document else 0
        operation_status = operation.status if operation is not None else None
        derived = derive_status(
            has_document=has_document,
            content_count=1 if has_document and (
                not document_supported or (document is not None and document_has_business_content(document))
            ) else 0,
            latest_operation_status=operation_status,
        )
        active = operation_status in ACTIVE_OPERATION_STATUSES
        retry_requires_new_job = not document_supported or job.prompt_version != GENERATION_PROMPT_VERSION
        return {
            "job_no": job.job_no,
            "source_filename": job.source_filename,
            "source_size": job.source_size,
            "business_background": job.business_background,
            "generation_requirements": job.generation_requirements,
            "document_schema_version": job.document_schema_version,
            "prompt_version": job.prompt_version,
            "revision": job.revision,
            "status": derived,
            "status_message": status_message(derived, operation=operation, has_document=has_document)
            if document_supported else "此记录采用之前的编制格式；原始资料和已导出的文件仍保留，不自动转换业务内容",
            "document": document.model_dump(mode="json") if document is not None else None,
            "document_supported": document_supported,
            "latest_operation": self._operation_view(operation),
            "entry_count": entry_count,
            "open_issue_count": open_issues,
            "can_generate": not has_document and job.revision == 0 and not active and document_supported
            and job.prompt_version == GENERATION_PROMPT_VERSION,
            "can_retry": not has_document and job.revision == 0 and not active and (
                operation_status in {"FAILED", "INTERRUPTED"} or retry_requires_new_job
            ),
            "retry_requires_new_job": retry_requires_new_job,
            "can_edit": has_document and not active and document_supported,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "reused": reused,
        }

    def _import_link_view(
        self, link: RulebookGenerationImport, import_jobs: dict[int, Any],
    ) -> dict[str, Any]:
        import_job = import_jobs.get(link.import_job_id) if link.import_job_id is not None else None
        return {
            "import_job_no": import_job.job_no if import_job is not None else None,
            "scenario_code": import_job.scenario_code if import_job is not None else None,
            "import_status": import_job.status if import_job is not None else None,
            "deleted": import_job is None,
            "created_at": link.created_at,
        }

    def export_views(self, job: RulebookGenerationJob) -> list[dict[str, Any]]:
        exports = crud.RulebookGenerationExportCRUD(self.db).for_job(int(job.id))
        links = crud.RulebookGenerationImportCRUD(self.db).for_exports([int(item.id) for item in exports])
        import_jobs = crud.RulebookGenerationImportCRUD(self.db).import_jobs([
            int(link.import_job_id) for items in links.values() for link in items
            if link.import_job_id is not None
        ])
        return [
            {
                "export_no": export.export_no,
                "revision": export.revision,
                "filename": export.filename,
                "size": export.size,
                "sha256": export.sha256,
                "export_format_version": export.export_format_version,
                "created_at": export.created_at,
                "imports": [
                    self._import_link_view(link, import_jobs) for link in links.get(int(export.id), [])
                ],
            }
            for export in exports
        ]

    def export_view(self, export: RulebookGenerationExport) -> dict[str, Any]:
        links = crud.RulebookGenerationImportCRUD(self.db).for_exports([int(export.id)])
        import_jobs = crud.RulebookGenerationImportCRUD(self.db).import_jobs([
            int(link.import_job_id) for link in links.get(int(export.id), [])
            if link.import_job_id is not None
        ])
        return {
            "export_no": export.export_no,
            "revision": export.revision,
            "filename": export.filename,
            "size": export.size,
            "sha256": export.sha256,
            "export_format_version": export.export_format_version,
            "created_at": export.created_at,
            "imports": [
                self._import_link_view(link, import_jobs) for link in links.get(int(export.id), [])
            ],
        }
