"""AI 辅助生成规则库 API。

- 上传只保存原件与生成输入，不调用模型；
- 开始生成通过既有后台操作受理，返回 202 语义；
- 导出按修订冻结，导入只创建真实导入作业并跳转旧工作台。"""

from io import BytesIO
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.response import ResponseSchema, ok
from app.core.database import get_db, request_session
from app.domain.rulebook_generation import GENERATION_INPUT_MAX_LENGTH
from app.modules.imports.generation.dto import (
    CreateGenerationExportRequest,
    ImportGenerationRequest,
    RetryGenerationRequest,
    SaveGenerationDocumentRequest,
    StartGenerationRequest,
    AssistantMessageRequest,
    RestoreGenerationRequest,
)
from app.modules.imports.generation.schema import (
    GenerationCreatedResponse,
    GenerationExportResponse,
    GenerationImportResponse,
    GenerationJobPageResponse,
    GenerationJobResponse,
    GenerationRetryResponse,
    GenerationMessageResponse,
    GenerationMessagesResponse,
    GenerationRevisionResponse,
)
from app.modules.imports.generation.service.editing import GenerationEditingService
from app.modules.imports.generation.service.export import GenerationExportService
from app.modules.imports.generation.service.import_bridge import GenerationImportService
from app.modules.imports.generation.service.operations import GenerationOperationsService
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.imports.generation.service.upload import GenerationUploadService
from app.modules.imports.generation.service.assistant import GenerationAssistantService
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.generation.crud import GenerationMessageCRUD, GenerationRevisionCRUD
from app.modules.system.operation.schema import OperationAcceptedResponse
from app.modules.system.operation.service.background import OperationBackgroundService
from app.schema.query.lists import RulebookGenerationListQueryParam
from app.schema.query.pagination import PaginationQueryParam
from app.services.shared.storage import resolve_storage_path, rulebook_media_type

router = APIRouter(prefix="/rulebook-generations", tags=["AI辅助生成规则库"])


@router.post("", response_model=ResponseSchema[GenerationCreatedResponse], status_code=201)
def create_generation(
    request: Request,
    file: UploadFile = File(...),
    business_background: str = Form("", max_length=GENERATION_INPUT_MAX_LENGTH),
    generation_requirements: str = Form("", max_length=GENERATION_INPUT_MAX_LENGTH),
    idempotency_key: str = Form("", max_length=120),
) -> ResponseSchema[GenerationCreatedResponse]:
    """上传 Excel 并创建生成作业；此时不调用模型。"""
    with request_session() as db:
        job, reused = GenerationUploadService(db).create_job(
            file,
            business_background=business_background,
            generation_requirements=generation_requirements,
            idempotency_key=idempotency_key,
        )
        view = GenerationQueryService(db).job_view(job, reused=reused)
        return ok(GenerationCreatedResponse(
            job=GenerationJobResponse.model_validate(view), reused=reused,
        ), request)


@router.get("/page", response_model=ResponseSchema[GenerationJobPageResponse])
def list_generations_page(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[RulebookGenerationListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[GenerationJobPageResponse]:
    """生成记录分页：状态筛选与展示共用同一套推导，total 与列表同范围。"""
    result = GenerationQueryService(db).list_page(
        filename=search.filename,
        status=search.status,
        created_from=search.created_from,
        created_to=search.created_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(GenerationJobPageResponse.model_validate(result), request)


@router.get("/{job_no}", response_model=ResponseSchema[GenerationJobResponse])
def get_generation(job_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[GenerationJobResponse]:
    """生成作业详情：当前文档、修订号和最近后台操作。"""
    job = GenerationQueryService(db).get_job(job_no)
    return ok(GenerationJobResponse.model_validate(GenerationQueryService(db).job_view(job)), request)


@router.get("/{job_no}/source-file")
def download_source_file(job_no: str, db: Session = Depends(get_db)) -> FileResponse:
    """下载原始 Excel；文件缺失明确 404。"""
    job = GenerationQueryService(db).get_job(job_no)
    path = resolve_storage_path(job.source_path)
    if not path.is_file():
        raise BusinessError(
            "GENERATION_SOURCE_FILE_MISSING", "该作业的原始 Excel 已不在存储目录中，无法下载",
            status_code=404, details={"job_no": job_no},
        )
    return FileResponse(path, media_type=rulebook_media_type(path), filename=job.source_filename)


@router.post("/{job_no}/generate", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def start_generation(
    job_no: str,
    request: Request,
    payload: StartGenerationRequest | None = None,
    db: Session = Depends(get_db),
) -> ResponseSchema[OperationAcceptedResponse]:
    """开始生成或失败后显式重试；返回 202 只表示已受理。

    成功生成后没有原地再次 AI 生成入口；重试必须等原操作结束且作业仍无文档。
    """
    job = GenerationQueryService(db).get_job(job_no, lock=True)
    operation, reused = GenerationOperationsService(db).submit_generation(
        job, idempotency_key=payload.idempotency_key if payload else None,
    )
    return ok(OperationAcceptedResponse.model_validate(
        OperationBackgroundService().operation_view(operation, reused=reused),
    ), request)


@router.post("/{job_no}/retry", response_model=ResponseSchema[GenerationRetryResponse], status_code=202)
def retry_generation(
    job_no: str,
    payload: RetryGenerationRequest,
    request: Request,
) -> ResponseSchema[GenerationRetryResponse]:
    """复用已上传资料显式重试；旧版本自动打开新的编制记录。"""
    with request_session() as db:
        job = GenerationQueryService(db).get_job(job_no, lock=True)
        target, operation, reused = GenerationOperationsService(db).retry_generation(
            job, idempotency_key=payload.idempotency_key,
        )
        return ok(GenerationRetryResponse(
            job=GenerationJobResponse.model_validate(GenerationQueryService(db).job_view(target)),
            operation=OperationAcceptedResponse.model_validate(
                OperationBackgroundService().operation_view(operation, reused=reused),
            ),
            reused=reused,
        ), request)


@router.post("/{job_no}/messages", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def send_assistant_message(
    job_no: str, payload: AssistantMessageRequest, request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[OperationAcceptedResponse]:
    operation, reused = GenerationAssistantService(db).submit(
        job_no, message=payload.message, expected_revision=payload.expected_revision,
        idempotency_key=payload.idempotency_key, issue_id=payload.issue_id,
    )
    return ok(OperationAcceptedResponse.model_validate(
        OperationBackgroundService().operation_view(operation, reused=reused),
    ), request)


@router.get("/{job_no}/messages", response_model=ResponseSchema[GenerationMessagesResponse])
def list_assistant_messages(
    job_no: str, request: Request, before_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
) -> ResponseSchema[GenerationMessagesResponse]:
    job = GenerationQueryService(db).get_job(job_no)
    rows = GenerationMessageCRUD(db).page(job.id, before_id=before_id, limit=41)
    selected = rows[:40]
    return ok(GenerationMessagesResponse(
        items=[GenerationMessageResponse(
            id=item.id, role=item.role, content=item.content, revision=item.revision, created_at=item.created_at,
            issue_id=operation.request_json.get("issue_id") if item.role == "USER" else None,
            answer=operation.request_json.get("message") if item.role == "USER" and operation.request_json.get("issue_id") else None,
            operation=OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation)),
        ) for item, operation in reversed(selected)],
        next_before_id=selected[-1][0].id if len(rows) > 40 else None,
    ), request)


@router.get("/{job_no}/revisions", response_model=ResponseSchema[list[GenerationRevisionResponse]])
def list_revisions(job_no: str, request: Request, db: Session = Depends(get_db)):
    job = GenerationQueryService(db).get_job(job_no)
    rows = [GenerationRevisionResponse(
        revision=item.revision, summary=item.summary, origin=item.origin, created_at=item.created_at,
    ) for item in GenerationRevisionCRUD(db).summaries(job.id)]
    if job.document_json is not None and not any(item.revision == job.revision for item in rows):
        rows.insert(0, GenerationRevisionResponse(
            revision=job.revision, summary="当前已保存版本（此前修改过程未留存）",
            origin="BASELINE", created_at=job.updated_at,
        ))
    return ok(rows, request)


@router.post("/{job_no}/restore", response_model=ResponseSchema[GenerationJobResponse])
def restore_revision(job_no: str, payload: RestoreGenerationRequest, request: Request, db: Session = Depends(get_db)):
    job = GenerationHistoryService(db).restore(
        job_no, expected_revision=payload.expected_revision, target_revision=payload.target_revision,
    )
    return ok(GenerationJobResponse.model_validate(GenerationQueryService(db).job_view(job)), request)


@router.put("/{job_no}/document", response_model=ResponseSchema[GenerationJobResponse])
def save_document(
    job_no: str,
    payload: SaveGenerationDocumentRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[GenerationJobResponse]:
    """保存完整编辑文档；旧修订保存明确冲突，不覆盖新结果。"""
    job = GenerationEditingService(db).save_document(
        job_no, expected_revision=payload.expected_revision, document_payload=payload.document,
    )
    return ok(GenerationJobResponse.model_validate(GenerationQueryService(db).job_view(job)), request)


@router.post("/{job_no}/exports", response_model=ResponseSchema[GenerationExportResponse])
def create_export(
    job_no: str,
    payload: CreateGenerationExportRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[GenerationExportResponse]:
    """按指定修订创建或取得导出；重复导出复用已有真实文件。"""
    export = GenerationExportService(db).create_export(
        job_no, expected_revision=payload.expected_revision,
    )
    return ok(GenerationExportResponse.model_validate(
        GenerationQueryService(db).export_view(export),
    ), request)


@router.get("/{job_no}/exports", response_model=ResponseSchema[list[GenerationExportResponse]])
def list_exports(job_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[list[GenerationExportResponse]]:
    """查询该作业已创建的全部导出修订。"""
    job = GenerationQueryService(db).get_job(job_no)
    return ok([
        GenerationExportResponse.model_validate(item)
        for item in GenerationQueryService(db).export_views(job)
    ], request)


@router.get("/{job_no}/exports/{export_no}/file")
def download_export_file(
    job_no: str, export_no: str, layout: Literal["frozen", "source"] = Query("frozen"),
    db: Session = Depends(get_db),
) -> Response:
    """默认下载冻结原件；页面可按相同快照内容使用上传文件的格式。"""
    service = GenerationExportService(db)
    path, export = service.source_layout_file(job_no, export_no) if layout == "source" else service.export_file(job_no, export_no)
    if isinstance(path, BytesIO):
        return Response(
            content=path.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(export.filename)}"},
        )
    return FileResponse(path, media_type=rulebook_media_type(path), filename=export.filename)


@router.post("/{job_no}/imports", response_model=ResponseSchema[GenerationImportResponse])
def import_export(
    job_no: str,
    payload: ImportGenerationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[GenerationImportResponse]:
    """把指定导出导入已有场景，创建真实导入作业；不自动写入草稿或发布。"""
    _link, import_job, export, reused = GenerationImportService(db).import_export(
        job_no,
        export_no=payload.export_no,
        scenario_code=payload.scenario_code,
        idempotency_key=payload.idempotency_key,
    )
    return ok(GenerationImportResponse(
        export_no=export.export_no,
        revision=export.revision,
        import_job_no=import_job.job_no,
        scenario_code=payload.scenario_code,
        import_status=import_job.status,
        reused=reused,
    ), request)
