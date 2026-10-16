from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .downloads import report_download_response

from ..database import SessionLocal, get_db
from ..common.exceptions import BusinessError
from ..repositories import task_repository
from ..schema.query import validate_page_params
from ..schema.dto import (
    ExecuteNodeRequest,
    ExternalArtifactReconcileRequest,
    FieldCorrectionRequest,
    OcrReconcileRequest,
    DocumentOcrRetryRequest,
    TaskCopyRequest,
    TaskUpdateRequest,
)
from ..services import (
    admin_query_service,
    task_admin_service,
    task_command_service,
)
from ..services.audit import correct_field_value, reconcile_ocr
from ..services.audit.commands import reconcile_external_artifact
from ..services.audit import evidence_view, report_view, risks_view
from ..services.background_service import operation_view
from ..services.operation_gateway import submit_document_ocr_retry, submit_node_execution
from ..services.task_lookup import task_or_404
from ..services.task_query_service import external_artifact_status_view, ocr_status_view, task_view
from ..services.task_service import (
    remove_current_document_file,
    upload_document,
)
from ..schema.vo import (
    AuditTaskResponse,
    DocumentRemoveResponse,
    DocumentUploadResponse,
    EvidenceResponse,
    ExternalArtifactReconcileResponse,
    ExternalArtifactStatusResponse,
    FieldCorrectionResponse,
    OcrArtifactStatusResponse,
    OcrReconcileResponse,
    OperationAcceptedResponse,
    PagedResponse,
    ReportResponse,
    RiskListResponse,
)


router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("", response_model=PagedResponse)
def list_tasks(
    request: Request,
    name: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    status: str | None = None,
    current_node: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核任务分页列表。"""
    page, page_size = validate_page_params(page, page_size)
    result = task_admin_service.list_tasks_page(
        db,
        name=name,
        scenario_code=scenario_code,
        plan_code=plan_code,
        status=status,
        current_node=current_node,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/options")
def task_options(request: Request, keyword: str | None = None, db: Session = Depends(get_db)) -> dict:
    """任务下拉选项：按名称或编号模糊匹配，最多 50 条，不含已删除任务。"""
    return {"request_id": request.state.request_id, "items": task_admin_service.list_task_options(db, keyword=keyword)}


@router.get("/{task_no}", response_model=AuditTaskResponse)
def get_task(task_no: str, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """任务详情：当前节点、材料位、字段、规则、待办与最近一次后台操作。"""
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, task_no)), "request_id": request.state.request_id})


@router.patch("/{task_no}", response_model=AuditTaskResponse)
def update_task(task_no: str, payload: TaskUpdateRequest, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """编辑任务基本资料（名称、备注）；执行快照不随重命名与方案升级变化。"""
    task_admin_service.update_task(db, task_no, payload.model_dump(exclude_unset=True))
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, task_no)), "request_id": request.state.request_id})


@router.post("/{task_no}/copy", response_model=AuditTaskResponse, status_code=201)
def copy_task_endpoint(task_no: str, payload: TaskCopyRequest, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """按源任务所属方案的当前范围重新创建任务。

    明确不是「复制原任务的执行快照」：新任务冻结的是方案此刻的范围。没有方案或
    方案、场景已失效时拒绝。同一幂等标识 + 同一请求内容的重发返回原任务。
    命令占位与任务在首次提交时一起落库；等待点确认后追加成功操作日志。
    """
    copied = task_command_service.copy_audit_task(
        db,
        task_no,
        task_name=payload.task_name,
        idempotency_key=payload.idempotency_key,
    )
    return AuditTaskResponse.model_validate({**task_view(db, copied), "request_id": request.state.request_id})


@router.delete("/{task_no}", status_code=204, response_class=Response)
def delete_task(task_no: str, db: Session = Depends(get_db)) -> Response:
    """永久删除任务及关联记录、文件；执行中的任务拒绝删除。"""
    task_admin_service.delete_task(db, task_no)
    return Response(status_code=204)


@router.get("/{task_no}/executions", response_model=PagedResponse)
def get_task_executions(
    task_no: str,
    request: Request,
    page: int = Query(default=1),
    page_size: int = Query(default=50),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """该任务的节点执行记录分页（默认每页 50 条）。"""
    page, page_size = validate_page_params(page, page_size)
    result = admin_query_service.list_executions_page(
        db,
        task_no=task_no,
        node_code=None,
        status=None,
        started_from=None,
        started_to=None,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


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
        record, impact, operation = upload_document(
            db, task_no, document_no, file, material_type=material_type, replace=replace
        )
        document = task_repository.find_document_by_id(db, record.task_document_id)
        if not document:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=500)
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


@router.post("/{task_no}/execute", response_model=OperationAcceptedResponse, status_code=202)
def execute(task_no: str, payload: ExecuteNodeRequest, request: Request, db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    """提交当前节点执行为后台操作。

    返回 202 只代表操作已受理，不代表节点执行成功；真实结果用 operation_no
    查询 GET /api/operations/{operation_no}，或刷新任务详情查看。
    """
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_node_execution(
        db,
        task,
        node_code=payload.node_code,
        idempotency_key=payload.idempotency_key,
        input_revision=payload.input_revision,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )




@router.get("/{task_no}/risks", response_model=RiskListResponse)
def get_risks(task_no: str, request: Request, db: Session = Depends(get_db)) -> RiskListResponse:
    """该任务的风险结果：命中只统计 ABNORMAL，未配置 / 未执行 / 无法判断分别表达。"""
    return RiskListResponse.model_validate({**risks_view(db, task_no), "request_id": request.state.request_id})


@router.get("/{task_no}/reports", response_model=ReportResponse)
def get_report(task_no: str, request: Request, report_no: str | None = None, db: Session = Depends(get_db)) -> ReportResponse:
    """该任务的审核报告视图：正文、三信号与生成时的范围快照。"""
    return ReportResponse.model_validate({**report_view(db, task_no, report_no), "request_id": request.state.request_id})


@router.get("/{task_no}/reports/download")
def download_report(task_no: str, report_no: str | None = None, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；报告尚未生成时明确 409，不返回空文件。"""
    report = report_view(db, task_no, report_no)
    content = report["content"]
    if report["status"] == "NOT_GENERATED" or not isinstance(content, str) or not content:
        raise BusinessError("REPORT_NOT_GENERATED", "审核报告尚未生成，无法下载", status_code=409)
    if not report["current"]:
        raise BusinessError("REPORT_STALE", "报告对应的输入或执行批次已失效，不能下载为当前报告", status_code=409)
    safe_name = "".join(char for char in str(report.get("report_name") or "审核报告") if char.isalnum() or char in "-_（）() ").strip() or "审核报告"
    return report_download_response(content, report["content_type"], f"{task_no}-{safe_name}")


@router.get("/{task_no}/evidence/{evidence_no}", response_model=EvidenceResponse)
def get_evidence(task_no: str, evidence_no: str, request: Request, db: Session = Depends(get_db)) -> EvidenceResponse:
    """单条证据详情：引用原文、依据类型与定位核对结论。"""
    return EvidenceResponse.model_validate({**evidence_view(db, task_no, evidence_no), "request_id": request.state.request_id})


@router.post("/{task_no}/field-corrections", response_model=FieldCorrectionResponse)
def correct_field(
    task_no: str,
    payload: FieldCorrectionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> FieldCorrectionResponse:
    """对错值/漏值字段发起主动纠错；生成新的字段事实版本并失效下游结果。"""
    result = correct_field_value(db, task_no, payload)
    return FieldCorrectionResponse.model_validate({**result, "request_id": request.state.request_id})


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
    """只续查或重试当前材料的 OCR，保留同一任务中其他材料的已完成结果。"""
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_document_ocr_retry(
        db, task, document_no=document_no, artifact_no=payload.artifact_no,
        input_revision=payload.input_revision, idempotency_key=payload.idempotency_key,
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


@router.post(
    "/{task_no}/documents/{document_no}/artifacts/{artifact_type}/reconcile",
    response_model=ExternalArtifactReconcileResponse,
)
def reconcile_document_artifact(
    task_no: str,
    document_no: str,
    artifact_type: str,
    payload: ExternalArtifactReconcileRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ExternalArtifactReconcileResponse:
    result = reconcile_external_artifact(db, task_no, document_no, artifact_type, payload)
    return ExternalArtifactReconcileResponse.model_validate({**result, "request_id": request.state.request_id})


@router.post("/{task_no}/documents/{document_no}/ocr/reconcile", response_model=OcrReconcileResponse)
def reconcile_document_ocr(
    task_no: str,
    document_no: str,
    payload: OcrReconcileRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OcrReconcileResponse:
    """OCR 未知或超时作业可采纳已确认作业号续查；未知、超时及失败作业可明确重提。"""
    result = reconcile_ocr(db, task_no, document_no, payload)
    return OcrReconcileResponse.model_validate({**result, "request_id": request.state.request_id})
