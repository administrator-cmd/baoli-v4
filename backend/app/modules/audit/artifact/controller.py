"""审核任务的材料上传、处理状态与单份 OCR 重试接口。"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db, request_session
from app.modules.audit.artifact.dto import DocumentArtifactRetryRequest, DocumentOcrRetryRequest
from app.modules.audit.artifact.schema import (
    DocumentRemoveResponse,
    DocumentUploadResponse,
    ExternalArtifactStatusResponse,
    OcrArtifactStatusResponse,
)
from app.modules.audit.artifact.service.material_retry import AuditArtifactMaterialRetryService
from app.modules.audit.execution.service.operations import AuditExecutionOperationsService
from app.modules.audit.task.service.documents import AuditTaskDocumentsService
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.audit.task.service.processing import AuditTaskProcessingService
from app.modules.system.operation.schema import OperationAcceptedResponse
from app.modules.system.operation.service.background import OperationBackgroundService

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.post("/{task_no}/documents/{document_no}/upload", response_model=ResponseSchema[DocumentUploadResponse], status_code=201)
def upload_task_document(
    task_no: str,
    document_no: str,
    request: Request,
    file: UploadFile = File(...),
    material_type: str = Form(...),
    replace: bool = Form(False),
) -> ResponseSchema[DocumentUploadResponse]:
    # 与后台执行互斥由服务函数在锁定任务行之后检查，避免检查与修改之间的竞态。
    """上传或替换材料位的当前文件，返回本次输入修订的真实影响范围。

    `material_type` 必填：用户从 8018 支持目录中选择的材料类型，随本次上传保存。
    材料位配置不代替本次文件的类型声明，因此这里不能留空。
    """
    # 同步入口由 FastAPI 放入工作线程；Session 在执行它的线程创建和关闭。
    with request_session() as db:
        document, record, impact = AuditTaskDocumentsService(db).upload_document(
             task_no, document_no, file, material_type=material_type, replace=replace
        )
        return ok(DocumentUploadResponse(
            document_no=document.document_no,
            material_name=document.material_name,
            filename=record.original_filename,
            size_bytes=record.size_bytes,
            status=record.status,
            impact=impact,
        ), request)


@router.post("/{task_no}/documents/{document_no}/remove", response_model=ResponseSchema[DocumentRemoveResponse])
def remove_task_document_file(
    task_no: str,
    document_no: str,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[DocumentRemoveResponse]:
    """移除材料位的当前文件（输入修订）。

    旧上传保留为历史版本、不删除文件；依赖的产物、事实与结果失效，
    任务回到材料检查等待重新上传或继续。
    """
    document, impact = AuditTaskDocumentsService(db).remove_current_document_file( task_no, document_no)
    task = AuditTaskLookupService(db).task_or_404( task_no)
    return ok(DocumentRemoveResponse(
        document_no=document.document_no,
        material_name=document.material_name,
        status=document.status,
        input_revision=task.input_revision,
        impact=impact,
    ), request)


@router.get("/{task_no}/documents/{document_no}/ocr", response_model=ResponseSchema[OcrArtifactStatusResponse])
def get_document_ocr(
    task_no: str,
    document_no: str,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[OcrArtifactStatusResponse]:
    """查询当前上传文件 OCR 产物的真实状态；只读，不触发提交或轮询。"""
    return ok(OcrArtifactStatusResponse.model_validate(AuditTaskProcessingService(db).ocr_status_view( task_no, document_no)), request)


@router.post("/{task_no}/documents/{document_no}/ocr/retry", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def retry_document_ocr_endpoint(
    task_no: str,
    document_no: str,
    payload: DocumentOcrRetryRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[OperationAcceptedResponse]:
    """只续查或重试当前材料的 OCR；提交结果未知时需明确确认可能重复创建作业。"""
    task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
    operation, reused = AuditExecutionOperationsService(db).submit_document_ocr_retry(
         task, document_no=document_no, artifact_no=payload.artifact_no,
        input_revision=payload.input_revision, idempotency_key=payload.idempotency_key,
        resubmit_unknown=payload.resubmit_unknown,
    )
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=reused)), request)


@router.get(
    "/{task_no}/documents/{document_no}/artifacts/{artifact_type}",
    response_model=ResponseSchema[ExternalArtifactStatusResponse],
)
def get_document_artifact(
    task_no: str,
    document_no: str,
    artifact_type: str,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[ExternalArtifactStatusResponse]:
    result = AuditTaskProcessingService(db).external_artifact_status_view( task_no, document_no, artifact_type)
    return ok(ExternalArtifactStatusResponse.model_validate(result), request)


@router.post("/{task_no}/documents/{document_no}/artifacts/{artifact_type}/retry", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def retry_document_artifact_endpoint(task_no: str, document_no: str, artifact_type: str,
                                     payload: DocumentArtifactRetryRequest, request: Request,
                                     db: Session = Depends(get_db)) -> ResponseSchema[OperationAcceptedResponse]:
    task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
    operation, reused = AuditArtifactMaterialRetryService(db).submit_document_artifact_retry(
         task, document_no=document_no, artifact_type=artifact_type, payload=payload,
    )
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=reused)), request)
