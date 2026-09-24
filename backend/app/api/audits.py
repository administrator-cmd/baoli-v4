from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BusinessError
from ..models import ManualReview, RuleResult, TaskDocument, TaskFieldValue, AuditTask
from ..schemas import (
    ApiResponse,
    AuditTaskResponse,
    DocumentRemoveResponse,
    DocumentUploadResponse,
    EvidenceResponse,
    ExecuteNodeRequest,
    FieldCorrectionRequest,
    FieldCorrectionResponse,
    ManualReviewRequest,
    OcrReconcileRequest,
    OcrReconcileResponse,
    OperationAcceptedResponse,
    PagedResponse,
    ReportResponse,
    RetrievalDecisionRequest,
    RetrievalDecisionResponse,
    RetrievalRequest,
    RetrievalResponse,
    TaskEvidenceIndexResponse,
    ReviewDraftRequest,
    ReviewDraftResponse,
    RiskListResponse,
    TaskCopyRequest,
    TaskUpdateRequest,
)
from ..services import admin_query_service, rag_service, task_admin_service
from ..services.audit import correct_field_value, reconcile_ocr
from ..services.audit import evidence_view, report_view, risks_view
from ..services.background_service import operation_view
from ..services.operation_gateway import submit_manual_reviews, submit_node_execution
from ..services.operation_log_service import record_operation, require_idempotent_replay
from ..services.operation_types import OBJECT_AUDIT_TASK
from ..services.task_lookup import task_or_404
from ..services.task_service import (
    copy_task,
    ocr_status_view,
    remove_current_document_file,
    task_view,
    upload_document,
)
from .export_utils import EXPORT_PAGE_SIZE, csv_response


router = APIRouter(prefix="/audit-tasks", tags=["合同审核"])


class OcrArtifactStatusResponse(ApiResponse):
    """当前上传文件 OCR 产物的只读状态；没有产物时如实说明未提交。"""

    document_no: str
    artifact_no: str | None = None
    status: str
    job_id: str | None = None
    deadline_at: datetime | None = None
    message: str
    submit_attempts: int | None = None


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size


@router.get("", response_model=PagedResponse)
def list_tasks(
    request: Request,
    name: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    legacy: bool | None = None,
    status: str | None = None,
    current_node: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    deleted: bool = False,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核任务分页列表；不传 deleted 时不返回已逻辑删除的任务。"""
    page, page_size = _page_params(page, page_size)
    result = task_admin_service.list_tasks_page(
        db,
        name=name,
        scenario_code=scenario_code,
        plan_code=plan_code,
        legacy=legacy,
        status=status,
        current_node=current_node,
        created_from=created_from,
        created_to=created_to,
        deleted=deleted,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/options")
def task_options(request: Request, keyword: str | None = None, db: Session = Depends(get_db)) -> dict:
    """任务下拉选项：按名称或编号模糊匹配，最多 50 条，不含已删除任务。"""
    return {"request_id": request.state.request_id, "items": task_admin_service.list_task_options(db, keyword=keyword)}


@router.get("/export")
def export_tasks(
    name: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    legacy: bool | None = None,
    status: str | None = None,
    current_node: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    deleted: bool = False,
    db: Session = Depends(get_db),
):
    """按当前筛选导出任务列表 CSV；与列表页使用同一查询作用域，不做静默截断。"""
    result = task_admin_service.list_tasks_page(
        db,
        name=name,
        scenario_code=scenario_code,
        plan_code=plan_code,
        legacy=legacy,
        status=status,
        current_node=current_node,
        created_from=created_from,
        created_to=created_to,
        deleted=deleted,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["task_no"],
            item["task_name"],
            item["scenario_name"],
            item["plan_name"] if not item["is_legacy_config"] else "历史任务配置",
            item["current_node_name"],
            item["status"],
            item["created_at"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "审核任务列表.csv",
        ["任务编号", "任务名称", "场景", "方案", "当前阶段", "任务状态", "创建时间"],
        rows,
    )


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
    """按源任务所属方案的**当前范围**重新创建任务。

    明确不是「复制原任务的执行快照」：新任务冻结的是方案此刻的范围。没有方案或
    方案、场景已失效时拒绝。同一幂等标识 + 同一请求内容的重发返回原任务。
    成功操作日志只在任务与审核等待点都成功之后写入；等待点失败时记一条失败记录，
    重放该标识返回原来的失败原因。
    """
    request_payload = {"task_no": task_no, "task_name": payload.task_name}
    existing, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_AUDIT_TASK,
        operation_type="TASK_COPY",
        idempotency_key=payload.idempotency_key,
        request=request_payload,
    )
    if existing is not None:
        if str(existing.result or "") != "SUCCEEDED":
            raise BusinessError(
                "TASK_COPY_PREVIOUSLY_FAILED",
                f"该幂等标识对应的复制任务命令此前执行失败：{existing.failure_message or '原因未记录'}。"
                "请更换幂等标识后重试",
                status_code=409,
                details={"log_no": existing.log_no, "failure_code": existing.failure_code},
            )
        detail = existing.detail_json if isinstance(existing.detail_json, dict) else {}
        replayed = db.scalar(select(AuditTask).where(AuditTask.task_no == detail.get("new_task_no")))
        if replayed is None:
            raise BusinessError(
                "TASK_COPY_REPLAY_UNAVAILABLE",
                "该幂等标识对应的任务记录不存在，请更换标识后重新复制",
                status_code=409,
                details={"log_no": existing.log_no},
            )
        return AuditTaskResponse.model_validate({**task_view(db, replayed), "request_id": request.state.request_id})
    created = copy_task(db, task_no, task_name=payload.task_name)
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task_no,
        object_name=payload.task_name,
        operation_type="TASK_COPY",
        detail={
            "source_task_no": task_no,
            "new_task_no": created.task_no,
            # 复制复制的是方案当前范围，指纹留痕以说明新任务冻结的是哪一版。
            "plan_scope_hash": created.task_schema_json.get("plan_scope_hash"),
        },
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
    )
    db.commit()
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, created.task_no)), "request_id": request.state.request_id})


@router.delete("/{task_no}", response_model=AuditTaskResponse)
def delete_task(task_no: str, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """逻辑删除任务：历史事实、文件、证据与报告保留；执行中的任务拒绝删除。"""
    task_admin_service.delete_task(db, task_no)
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, task_no)), "request_id": request.state.request_id})


@router.post("/{task_no}/restore", response_model=AuditTaskResponse)
def restore_task(task_no: str, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """恢复已逻辑删除的任务；任务不在已删除列表时明确返回 409。"""
    task_admin_service.restore_task(db, task_no)
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, task_no)), "request_id": request.state.request_id})


@router.get("/{task_no}/reviews")
def get_task_reviews(task_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """任务内的人工复核记录（含已复核与已失效），系统原判断与人工结论分开呈现。"""
    task = task_or_404(db, task_no)
    rows = db.scalars(
        select(ManualReview).where(ManualReview.audit_task_id == task.id).order_by(ManualReview.id.desc())
    ).all()
    rule_names = {
        item.id: item.rule_name
        for item in db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task.id)).all()
    }
    fact_keys = {
        item.id: (item.field_key, item.material_key)
        for item in db.scalars(select(TaskFieldValue).where(TaskFieldValue.audit_task_id == task.id)).all()
    }
    field_names = {item["field_key"]: item["name"] for item in task.task_schema_json.get("fields", [])}
    documents = {
        item.material_key: item.material_name
        for item in db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task.id)).all()
    }
    result = []
    for review in rows:
        judgment = review.system_judgment_json if isinstance(review.system_judgment_json, dict) else {}
        if review.rule_result_id is not None:
            object_name = f"规则：{rule_names.get(review.rule_result_id, '未知规则')}"
            object_kind = "RULE"
        elif review.field_value_id is not None:
            field_key, material_key = fact_keys.get(review.field_value_id, (None, None))
            material_name = documents.get(material_key, material_key or "未知材料")
            object_name = f"字段：{material_name}·{field_names.get(field_key, field_key or '未知字段')}"
            object_kind = "FIELD"
        elif review.document_upload_id is not None:
            object_name = "材料文件"
            object_kind = "MATERIAL"
        else:
            object_name = "未关联业务对象"
            object_kind = "UNKNOWN"
        result.append(
            {
                "review_no": review.review_no,
                "review_type": review.review_type,
                "object_name": object_name,
                "object_kind": object_kind,
                "status": review.status,
                "system_judgment": {
                    "status": judgment.get("status"),
                    "message": judgment.get("message"),
                    "raw_value": judgment.get("raw_value"),
                    # 取证检索与印章记录随复核快照一起返回：主体核对的待办必须能看到
                    # 系统查过的范围、候选、双方原文与完整印章条目，不能只在后端保存。
                    "retrieval": judgment.get("retrieval"),
                    "seal_records": judgment.get("seal_records"),
                    # 材料类型待复核的原始识别结论：置信度、8018 判定的类型与命中关键词。
                    # 这些是人工判断"是不是放错材料位"的依据，只在后端保存等于没给。
                    "service_code": judgment.get("service_code"),
                    "service_result": judgment.get("service_result"),
                },
                "decision": review.decision,
                "business_note": review.business_note,
                "before_value": review.before_value,
                "after_value": review.after_value,
                "basis_type": review.basis_type,
                "decision_command_no": review.decision_command_no,
                "created_at": review.created_at,
                "updated_at": review.updated_at,
            }
        )
    return {"request_id": request.state.request_id, "task_no": task.task_no, "items": result}


@router.get("/{task_no}/review-drafts", response_model=ReviewDraftResponse)
def get_review_drafts(task_no: str, request: Request, db: Session = Depends(get_db)) -> ReviewDraftResponse:
    """读取当前待办版本下的复核草稿；旧待办版本的草稿不会返回。"""
    from ..services import review_draft_service

    task = task_or_404(db, task_no)
    drafts = review_draft_service.load_drafts(db, task_no)
    return ReviewDraftResponse(
        request_id=request.state.request_id,
        task_no=task_no,
        pending_hash=task.pending_review_hash,
        drafts=drafts,
    )


@router.put("/{task_no}/review-drafts", response_model=ReviewDraftResponse)
def put_review_drafts(
    task_no: str, payload: ReviewDraftRequest, request: Request, db: Session = Depends(get_db)
) -> ReviewDraftResponse:
    """保存复核草稿（独立于正式提交的持久化暂存）。"""
    from ..services import review_draft_service

    result = review_draft_service.save_drafts(db, task_no, [item.model_dump() for item in payload.items])
    return ReviewDraftResponse(
        request_id=request.state.request_id,
        task_no=result["task_no"],
        pending_hash=result["pending_hash"],
        saved_review_nos=result["saved_review_nos"],
    )


@router.get("/{task_no}/executions", response_model=PagedResponse)
def get_task_executions(
    task_no: str,
    request: Request,
    page: int = Query(default=1),
    page_size: int = Query(default=50),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """该任务的节点执行记录分页（默认每页 50 条）。"""
    page, page_size = _page_params(page, page_size)
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
async def upload_task_document(
    task_no: str,
    document_no: str,
    request: Request,
    file: UploadFile = File(...),
    material_type: str = Form(...),
    replace: bool = Form(False),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    # 与后台执行互斥由服务函数在锁定任务行之后检查，避免检查与修改之间的竞态。
    """上传或替换材料位的当前文件，返回本次输入修订的真实影响范围。

    `material_type` 必填：用户从 8018 支持目录中选择的材料类型，随本次上传保存。
    材料位本身不再声明类型（见 01_材料位 的列协议），因此这里不能留空。
    """
    record, impact = await upload_document(
        db, task_no, document_no, file, material_type=material_type, replace=replace
    )
    document = db.scalar(select(TaskDocument).where(TaskDocument.id == record.task_document_id))
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=500)
    return DocumentUploadResponse(
        request_id=request.state.request_id,
        document_no=document.document_no,
        material_name=document.material_name,
        filename=record.original_filename,
        size_bytes=record.size_bytes,
        status=record.status,
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


@router.get("/{task_no}/documents/{document_no}/files/{upload_no}/download")
def download_document_file(
    task_no: str,
    document_no: str,
    upload_no: str,
    request: Request,
    preview: bool = False,
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载/预览一个上传版本的文件（历史版本同样可下载）。

    preview=true 时以内联方式返回，供浏览器自行预览支持的格式（PDF/图片）；
    不支持的格式由浏览器决定下载，页面不生成假的预览。
    """
    task = task_or_404(db, task_no)
    document, _ = admin_query_service.document_upload_by_no(db, document_no, upload_no)
    if document.audit_task_id != task.id:
        raise BusinessError("DOCUMENT_UPLOAD_NOT_FOUND", "上传文件不属于该任务", status_code=404, details={"upload_no": upload_no})
    return admin_query_service.upload_file_response(db, document_no, upload_no, preview=preview)


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


@router.post("/{task_no}/manual-review", response_model=OperationAcceptedResponse, status_code=202)
def manual_review(task_no: str, payload: ManualReviewRequest, request: Request, db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    """提交人工复核恢复为后台操作。

    返回 202 只代表已受理；复核是否真正生效用 operation_no 查询，或刷新任务详情查看。
    任务状态校验在幂等查询之后由服务层执行：同一命令的重发必须优先复用原操作。
    """
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_manual_reviews(
        db,
        task,
        reviews=[item.model_dump(mode="json") for item in payload.reviews],
        idempotency_key=payload.idempotency_key,
        input_revision=payload.input_revision,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )


@router.get("/{task_no}/agent-tool-calls")
def get_agent_tool_calls(
    task_no: str,
    request: Request,
    evaluation_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Agent 工具调用账本的只读查询：模型请求了什么、依据哪一版事实与工具定义、得到什么。

    `evaluation_id` 给出时只返回该评估批次的调用；不给出时返回本任务的全部批次，
    并按批次分组，便于页面按「一次规则评估」查看取证过程。

    只读、不触发任何执行；返回的是账本里真实记录的内容，不由页面重新计算比较过程。
    密钥与外部服务原始载荷不进这个视图（见 `services.agent_tool_ledger.call_view`）。
    """
    task = task_or_404(db, task_no)
    from ..services import agent_tool_ledger as ledger

    batches = [evaluation_id] if evaluation_id else ledger.evaluations_of_task(db, task_id=task.id)
    return {
        "request_id": request.state.request_id,
        "task_no": task.task_no,
        "evaluations": batches,
        "items": [
            call
            for batch in batches
            for call in ledger.ledger_view(db, task_id=task.id, evaluation_id=batch)
        ],
    }


@router.get("/{task_no}/risks", response_model=RiskListResponse)
def get_risks(task_no: str, request: Request, db: Session = Depends(get_db)) -> RiskListResponse:
    """该任务的风险结果：命中只统计 ABNORMAL，未配置 / 未执行 / 无法判断分别表达。"""
    return RiskListResponse.model_validate({**risks_view(db, task_no), "request_id": request.state.request_id})


@router.get("/{task_no}/reports", response_model=ReportResponse)
def get_report(task_no: str, request: Request, db: Session = Depends(get_db)) -> ReportResponse:
    """该任务的审核报告视图：正文、三信号与生成时的范围快照。"""
    return ReportResponse.model_validate({**report_view(db, task_no), "request_id": request.state.request_id})


@router.get("/{task_no}/reports/download")
def download_report(task_no: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """下载报告正文 Markdown；报告尚未生成时明确 409，不返回空文件。"""
    report = report_view(db, task_no)
    content = report["content"]
    if report["status"] == "NOT_GENERATED" or not isinstance(content, str) or not content:
        raise BusinessError("REPORT_NOT_GENERATED", "审核报告尚未生成，无法下载", status_code=409)
    filename = f"{task_no}-审核报告.md"
    return StreamingResponse(
        BytesIO(content.encode("utf-8")),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


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


@router.post("/{task_no}/documents/{document_no}/ocr/reconcile", response_model=OcrReconcileResponse)
def reconcile_document_ocr(
    task_no: str,
    document_no: str,
    payload: OcrReconcileRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OcrReconcileResponse:
    """OCR 提交结果未知时的人工对账：采纳已确认作业号续查，或明确重新提交。"""
    result = reconcile_ocr(db, task_no, document_no, payload)
    return OcrReconcileResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/{task_no}/evidence-index", response_model=TaskEvidenceIndexResponse)
def get_evidence_index(task_no: str, request: Request, db: Session = Depends(get_db)) -> TaskEvidenceIndexResponse:
    """当前任务各上传版本的证据索引状态；只读，不触发建索引。

    索引状态直接来自真实构建结果：BUILDING / READY / FAILED / INVALIDATED 分别表示
    构建中、可检索、构建失败与不再适用于当前上传版本。索引为空与“索引构建失败”不是一回事。
    """
    task = task_or_404(db, task_no)
    return TaskEvidenceIndexResponse.model_validate(
        {**rag_service.index_status_view(db, task), "request_id": request.state.request_id}
    )


@router.get("/{task_no}/retrievals")
def list_task_retrievals(task_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """本任务的检索记录：查询内容、限定范围、模型与索引版本、候选与采用结果。"""
    task = task_or_404(db, task_no)
    return {**rag_service.retrieval_history_view(db, task), "request_id": request.state.request_id}


@router.post("/{task_no}/retrievals", response_model=RetrievalResponse)
def create_task_retrieval(
    task_no: str,
    payload: RetrievalRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> RetrievalResponse:
    """用待核对主体在当前任务、当前材料范围内检索相似印章主体，返回候选。

    检索范围严格限定在本任务、本材料范围、当前上传版本、产物内容版本与当前模型内；
    相似度只用于候选排序，不构成主体一致的结论，结论由工作人员另行提交。
    """
    task = task_or_404(db, task_no)
    result = rag_service.retrieve_evidence(
        db,
        task,
        query_text=payload.query_text,
        material_keys=payload.material_keys,
        rule_key=payload.rule_key,
        top_k=payload.top_k,
    )
    db.commit()
    return RetrievalResponse.model_validate(
        {
            "task_no": task.task_no,
            "retrieval_no": result["retrieval_no"],
            "status": result["status"],
            "message": result["message"],
            "candidates": result["candidates"],
            "adopted": result["adopted"],
            "uncovered_materials": result.get("uncovered_materials", []),
            "excluded_indexes": result.get("excluded_indexes", []),
            "request_id": request.state.request_id,
        }
    )


@router.post("/{task_no}/retrievals/{retrieval_no}/decision", response_model=RetrievalDecisionResponse)
def submit_retrieval_decision(
    task_no: str,
    retrieval_no: str,
    payload: RetrievalDecisionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> RetrievalDecisionResponse:
    """保存工作人员对一次主体检索候选的结论。

    只接受一致 / 不一致 / 证据不足：相似度不能代替审核结论，
    也不存在“自动采纳前几条候选”的取值。
    """
    task = task_or_404(db, task_no)
    record = rag_service.record_human_decision(
        db,
        task,
        retrieval_no=retrieval_no,
        decision=payload.decision,
        note=payload.note,
        selected=payload.selected,
        expected_input_revision=payload.expected_input_revision,
    )
    db.commit()
    return RetrievalDecisionResponse.model_validate(
        {
            "task_no": task.task_no,
            "retrieval_no": record.retrieval_no,
            "decision": record.human_decision,
            "note": record.human_decision_note,
            "selected": record.human_selected,
            "decided_at": record.human_decided_at,
            "decision_input_revision": record.decision_input_revision,
            "request_id": request.state.request_id,
        }
    )
