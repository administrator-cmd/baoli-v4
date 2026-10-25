"""审核任务的材料上传、处理状态与单份 OCR 重试接口。"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from ...core.database import SessionLocal, get_db
from ...schema.dto.tasks import DocumentArtifactRetryRequest, DocumentOcrRetryRequest
from ...schema.vo.audits import OcrArtifactStatusResponse
from ...schema.vo.operations import OperationAcceptedResponse
from ...schema.vo.tasks import DocumentRemoveResponse, DocumentUploadResponse, ExternalArtifactStatusResponse
from ...services.audit.material_retry import submit_document_artifact_retry
from ...services.audit.operations import submit_document_ocr_retry
from ...services.audit.tasks.documents import remove_current_document_file, upload_document
from ...services.audit.tasks.lookup import task_or_404
from ...services.audit.tasks.processing import external_artifact_status_view, ocr_status_view
from ...services.system.background import operation_view

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.post("/{task_no}/documents/{document_no}/upload", response_model=DocumentUploadResponse, status_code=201)
def upload_task_document(
    task_no: str,
    document_no: str,
    request: Request,
    file: UploadFile = File(...),
    material_type: str = Form(...),
    replace: bool = Form(False),
) -> DocumentUploadResponse:
    # 与后台执行互斥由服务函数在锁定任务行之后检查，避免检查与修改之间的竞态。
    """上传或替换材料位的当前文件，返回本次输入修订的真实影响范围。

    `material_type` 必填：用户从 8018 支持目录中选择的材料类型，随本次上传保存。
    材料位配置不代替本次文件的类型声明，因此这里不能留空。
    """
    # 同步入口由 FastAPI 放入工作线程；Session 在执行它的线程创建和关闭。
    with SessionLocal() as db:
        document, record, impact, operation = upload_document(
            db, task_no, document_no, file, material_type=material_type, replace=replace
        )
        return DocumentUploadResponse(
            request_id=request.state.request_id,
            document_no=document.document_no,
            material_name=document.material_name,
            filename=record.original_filename,
            size_bytes=record.size_bytes,
            status=record.status,
            classification_operation={**operation_view(operation), "request_id": request.state.request_id},
            impact=impact,
        )


@router.post("/{task_no}/documents/{document_no}/remove", response_model=DocumentRemoveResponse)
def remove_task_document_file(
    task_no: str,
    document_no: str,
    request: Request,
    db: Session = Depends(get_db),
) -> DocumentRemoveResponse:
    """移除材料位的当前文件（输入修订）。

    旧上传保留为历史版本、不删除文件；依赖的产物、事实与结果失效，
    任务回到材料检查等待重新上传或继续。
    """
    document, impact = remove_current_document_file(db, task_no, document_no)
    task = task_or_404(db, task_no)
    return DocumentRemoveResponse(
        request_id=request.state.request_id,
        document_no=document.document_no,
        material_name=document.material_name,
        status=document.status,
        input_revision=task.input_revision,
        impact=impact,
    )


@router.get("/{task_no}/documents/{document_no}/ocr", response_model=OcrArtifactStatusResponse)
def get_document_ocr(
    task_no: str,
    document_no: str,
    request: Request,
    db: Session = Depends(get_db),
) -> OcrArtifactStatusResponse:
    """查询当前上传文件 OCR 产物的真实状态；只读，不触发提交或轮询。"""
    return OcrArtifactStatusResponse.model_validate(
        {**ocr_status_view(db, task_no, document_no), "request_id": request.state.request_id}
    )


@router.post("/{task_no}/documents/{document_no}/ocr/retry", response_model=OperationAcceptedResponse, status_code=202)
def retry_document_ocr_endpoint(
    task_no: str,
    document_no: str,
    payload: DocumentOcrRetryRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OperationAcceptedResponse:
    """只续查或重试当前材料的 OCR；提交结果未知时需明确确认可能重复创建作业。"""
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_document_ocr_retry(
        db, task, document_no=document_no, artifact_no=payload.artifact_no,
        input_revision=payload.input_revision, idempotency_key=payload.idempotency_key,
        resubmit_unknown=payload.resubmit_unknown,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )


@router.get(
    "/{task_no}/documents/{document_no}/artifacts/{artifact_type}",
    response_model=ExternalArtifactStatusResponse,
)
def get_document_artifact(
    task_no: str,
    document_no: str,
    artifact_type: str,
    request: Request,
    db: Session = Depends(get_db),
) -> ExternalArtifactStatusResponse:
    result = external_artifact_status_view(db, task_no, document_no, artifact_type)
    return ExternalArtifactStatusResponse.model_validate({**result, "request_id": request.state.request_id})


@router.post("/{task_no}/documents/{document_no}/artifacts/{artifact_type}/retry", response_model=OperationAcceptedResponse, status_code=202)
def retry_document_artifact_endpoint(task_no: str, document_no: str, artifact_type: str,
                                     payload: DocumentArtifactRetryRequest, request: Request,
                                     db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_document_artifact_retry(
        db, task, document_no=document_no, artifact_type=artifact_type, payload=payload,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )


