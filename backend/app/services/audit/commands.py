"""审核的对外命令入口：节点执行、人工复核提交、字段纠错、OCR 对账、中断恢复。

这些是 API 与后台操作调用的唯一入口；每个入口在进入图之前先完成并发与状态校验
（任务已结束 / 正在执行 / 节点不一致 / 输入修订过期都明确拒绝），
再走「恢复图等待点」的路径，不留下绕开图的旁路。

失败语义：图运行时异常会把任务标记为 FAILED 并返回带摘要的错误，不重试、不静默；
checkpoint 存储暂时不可达只让本次操作失败（可重试），不改变任务状态。"""

import logging
from ...config import get_settings
from ...domain.identity import public_no
from ...errors import BusinessError, exception_summary
from ...graphs.audit_graph import resume_audit_confirmation, resume_audit_manual_review
from . import flow as audit_flow
from ...models import AuditTask, DocumentUpload, ManualReview, NodeExecution, StoredArtifact, TaskDocument, TaskFieldValue, WorkflowCheckpoint
from ...schemas import ExecuteNodeRequest, FieldCorrectionRequest, ManualReviewRequest, OcrReconcileRequest
from ..task_lookup import task_or_404
from .artifacts import _artifact_for_upload, _ocr_job_metadata, _submit_ocr_job
from .evidence import _evidence_snapshot
from .facts import _refresh_extraction_rule_results, _write_human_field_fact
from .flow import _raise_graph_error
from .graph_runtime import _ensure_audit_interrupt, _mark_graph_runtime_failed
from .interrupt import start_audit_interrupt
from .reviews import _current_review_batch, _decision_command_no, _invalidate_validation_results, _review_hash, _validate_review_payloads
from .shared import _normalize_field_value, _scenario, _task_field_definitions
from .state import _scenario_nodes
from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Any


logger = logging.getLogger(__name__)


def recover_interrupted_executions(db: Session) -> dict[str, int]:
    """服务启动时清理上次运行中断留下的 RUNNING 记录。

    只做「显式标记中断 + 保留重试入口」：外部服务调用可能已被供应商接受，
    自动重放会制造重复作业，因此既不自动重发，也不把中断伪装成恢复成功。
    仅适用于单进程部署（当前设计即单 FastAPI 进程）。
    """
    interrupted = db.scalars(select(NodeExecution).where(NodeExecution.status == "RUNNING")).all()
    for execution in interrupted:
        execution.status = "FAILED"
        execution.finished_at = datetime.now(UTC)
        execution.failure_code = "AUDIT_EXECUTION_INTERRUPTED"
        execution.failure_message = "服务重启导致该节点执行中断，已有结果已保留，请重新执行当前节点"
    # 只处理真正停在执行中的任务。等待节点确认或人工复核的任务必须保持等待，
    # 不能一律置 FAILED；它们的图 checkpoint 仍可用于恢复。
    running_tasks = db.scalars(select(AuditTask).where(AuditTask.status == "RUNNING")).all()
    for task in running_tasks:
        task.status = "BLOCKED"
    db.commit()
    return {"interrupted_executions": len(interrupted), "blocked_tasks": len(running_tasks)}

def execute_current_node(db: Session, task_no: str, request: ExecuteNodeRequest) -> dict[str, Any]:
    task = task_or_404(db, task_no, lock=True)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    # 必须在进入流程图之前就拦住并发，否则第二个请求会直接 resume 正在执行的图。
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能并发执行或重跑", status_code=409)
    if request.node_code != task.current_node:
        raise BusinessError("AUDIT_NODE_MISMATCH", "提交节点与任务当前节点不一致", status_code=409)
    running_execution = db.scalar(
        select(NodeExecution).where(
            NodeExecution.audit_task_id == task.id,
            NodeExecution.status == "RUNNING",
        )
    )
    if running_execution:
        raise BusinessError("AUDIT_NODE_RUNNING", "当前任务已有执行中的节点，不能并发执行或重跑", status_code=409)
    graph_namespace = _ensure_audit_interrupt(db, task, "AWAITING_CONFIRMATION")
    try:
        state = resume_audit_confirmation(task.thread_id, graph_namespace, request.node_code, db, audit_flow)
    except BusinessError:
        raise
    except Exception as exc:
        logger.exception("审核流程恢复发生未处理异常", extra={"task_no": task_no, "node_code": request.node_code})
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError(
            "AUDIT_GRAPH_RUNTIME_FAILED",
            f"审核流程恢复失败，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc
    try:
        _raise_graph_error(state)
    except BusinessError as exc:
        if exc.code == "AUDIT_GRAPH_RESULT_INVALID":
            _mark_graph_runtime_failed(db, task_no)
        raise
    except Exception as exc:
        logger.exception("审核流程返回状态无效", extra={"task_no": task_no, "node_code": request.node_code})
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError(
            "AUDIT_GRAPH_RUNTIME_FAILED",
            f"审核流程返回状态无效，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc
    result = state.get("last_execution")
    if not isinstance(result, dict):
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError("AUDIT_GRAPH_RESULT_MISSING", "审核流程未返回当前节点执行结果", status_code=500)
    return result

def submit_manual_reviews(db: Session, task_no: str, request: ManualReviewRequest) -> dict[str, Any]:
    task = task_or_404(db, task_no, lock=True)
    if task.status != "WAITING_REVIEW" or task.current_node != "MANUAL_REVIEW":
        raise BusinessError("MANUAL_REVIEW_NOT_WAITING", "任务当前不在人工复核等待状态", status_code=409)
    reviews = _current_review_batch(db, task)
    current_hash = _review_hash(reviews)
    if current_hash != task.pending_review_hash:
        raise BusinessError("MANUAL_REVIEW_SET_CHANGED", "待复核集合已变化，请刷新后重新提交", status_code=409)
    requested = {item.review_no: item for item in request.reviews}
    if len(requested) != len(request.reviews) or set(requested) != {item.review_no for item in reviews}:
        raise BusinessError("MANUAL_REVIEW_INCOMPLETE", "必须处理当前全部待复核项")
    _validate_review_payloads(db, task, reviews, requested)
    graph_namespace = _ensure_audit_interrupt(db, task, "WAITING_REVIEW")
    decisions = [item.model_dump(mode="json") for item in request.reviews]
    try:
        state = resume_audit_manual_review(task.thread_id, graph_namespace, decisions, db, audit_flow)
    except BusinessError:
        raise
    except Exception as exc:
        logger.exception("人工复核流程恢复发生未处理异常", extra={"task_no": task_no})
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError(
            "AUDIT_GRAPH_RUNTIME_FAILED",
            f"人工复核流程恢复失败，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc
    try:
        _raise_graph_error(state)
    except BusinessError as exc:
        if exc.code == "AUDIT_GRAPH_RESULT_INVALID":
            _mark_graph_runtime_failed(db, task_no)
        raise
    except Exception as exc:
        logger.exception("人工复核流程返回状态无效", extra={"task_no": task_no})
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError(
            "AUDIT_GRAPH_RUNTIME_FAILED",
            f"人工复核流程返回状态无效，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc
    result = state.get("manual_review_result")
    if not isinstance(result, dict):
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError("MANUAL_REVIEW_RESULT_MISSING", "人工复核流程未返回处理结果", status_code=500)
    return result

def correct_field_value(db: Session, task_no: str, request: FieldCorrectionRequest) -> dict[str, Any]:
    """主动字段纠错入口：对 PRESENT 错值与 NOT_PRESENT 漏值统一处理。

    与人工复核共用同一套类型校验、事实版本与下游重算通道：生成新的不可变事实版本、
    把旧的退出当前位、失效依据旧事实的待办并重算下游，最后把任务退回内部校验并重建图等待点。
    旧事实、旧复核与旧证据保持可追溯。
    """
    task = task_or_404(db, task_no, lock=True)
    if task.deleted_at is not None:
        raise BusinessError(
            "AUDIT_TASK_DELETED",
            "任务已被删除，请先恢复后再发起字段纠错",
            status_code=409,
            details={"task_no": task_no},
        )
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("TASK_DOCUMENT_LOCKED", "任务已结束，不能发起字段纠错", status_code=409)
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能发起字段纠错", status_code=409)
    # 与节点执行、复核恢复同一契约：声明修订与当前修订不一致时明确拒绝，避免旧输入覆盖新事实。
    if request.input_revision is not None and int(request.input_revision) != int(task.input_revision):
        raise BusinessError(
            "INPUT_REVISION_CONFLICT",
            "任务输入在提交期间已被其他操作更新，本次纠错基于过期输入；请刷新任务后重新提交",
            status_code=409,
            details={"submitted": int(request.input_revision), "current": int(task.input_revision)},
        )
    field = _task_field_definitions(task).get(request.field_key)
    if not field or field["material_key"] != request.material_key:
        raise BusinessError(
            "FIELD_FACT_MISSING",
            "纠错字段不在当前任务的字段清单中",
            status_code=404,
            details={"field_key": request.field_key, "material_key": request.material_key},
        )
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.material_key == request.material_key,
        )
    )
    if not document or not document.current_upload_id:
        raise BusinessError(
            "FIELD_FACT_MISSING",
            "该材料位尚未上传当前文件，没有可纠错的字段事实",
            status_code=404,
            details={"material_key": request.material_key},
        )
    fact = db.scalar(
        select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.scenario_version_id == task.scenario_version_id,
            TaskFieldValue.field_key == request.field_key,
            TaskFieldValue.material_key == request.material_key,
            TaskFieldValue.document_upload_id == document.current_upload_id,
            TaskFieldValue.is_current.is_(True),
        )
    )
    if not fact:
        raise BusinessError(
            "FIELD_FACT_MISSING",
            "当前上传没有该字段的当前事实，不能纠错",
            status_code=404,
            details={"field_key": request.field_key},
        )
    normalized_value: Any = None
    if request.decision == "MODIFY_FIELD":
        if request.field_value is None or not request.field_value.strip():
            raise BusinessError("FIELD_CORRECTION_VALUE_REQUIRED", "修改字段必须填写纠错后的字段值")
        try:
            # 与人工复核完全同一套类型校验（含金额的单位与币种口径）；不合法就明确报错，不落库。
            normalized_value = _normalize_field_value(
                field["value_type"],
                request.field_value,
                unit=field.get("unit"),
                currency=field.get("currency"),
            )
        except (InvalidOperation, ValueError) as exc:
            raise BusinessError(
                "FIELD_CORRECTION_VALUE_INVALID",
                f"纠错后的字段值不符合字段类型：{exc}",
                details={
                    "field_name": field["name"],
                    "value_type": field["value_type"],
                    "unit": field.get("unit"),
                    "currency": field.get("currency"),
                },
            ) from exc
        new_status, new_raw = "REVIEWED", request.field_value.strip()
    else:
        if request.field_value not in (None, ""):
            raise BusinessError("FIELD_CORRECTION_VALUE_UNEXPECTED", "漏值纠正不能填写字段值")
        new_status, new_raw = "NOT_PRESENT", None
    system_judgment = {
        "status": fact.status,
        "raw_value": fact.raw_value,
        "normalized_value": fact.normalized_value_json,
        "evidence": _evidence_snapshot(db, fact.evidence_id),
    }
    new_fact, previous_revision_no = _write_human_field_fact(
        db,
        task,
        field_key=request.field_key,
        material_key=request.material_key,
        document_upload_id=document.current_upload_id,
        status=new_status,
        raw_value=new_raw,
        normalized_value=normalized_value,
    )
    # 依据旧事实的待办复核必须失效，不能继续挂在待办里。
    invalidated_reviews = db.execute(
        update(ManualReview)
        .where(ManualReview.field_value_id == fact.id, ManualReview.status == "PENDING")
        .values(status="INVALIDATED")
    ).rowcount or 0
    review = ManualReview(
        review_no=public_no("REV"),
        audit_task_id=task.id,
        field_value_id=new_fact.id,
        review_type="FIELD_CONFIRMATION",
        status="RESOLVED",
        system_judgment_json=system_judgment,
        decision=request.decision,
        business_note=request.business_note,
        proposed_value=new_raw,
        before_value=fact.raw_value,
        after_value=new_raw,
        basis_type=request.basis_type,
        decision_command_no=_decision_command_no(db, task),
    )
    db.add(review)
    task.input_revision += 1
    db.flush()
    manifest = _scenario(db, task).manifest_json
    _, nodes, current_index = _scenario_nodes(db, task)
    extraction_index = next(index for index, item in enumerate(nodes) if item["node_code"] == "FIELD_EXTRACTION")
    # 只有已经越过字段抽取节点才有可重算的抽取规则结果；否则抽取重跑时会自然重建。
    recomputed_rules = (
        _refresh_extraction_rule_results(db, task, manifest) if current_index > extraction_index else []
    )
    invalidation = _invalidate_validation_results(db, task)
    # 退回内部校验重新判定；任务停在当前节点之前的节点时保持原节点，不跳过尚未执行的节点。
    target_index = next(index for index, item in enumerate(nodes) if item["node_code"] == "INTERNAL_VALIDATE")
    rewind_index = min(current_index, target_index)
    db.execute(
        update(WorkflowCheckpoint)
        .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
        .values(status="INVALIDATED")
    )
    task.pending_review_hash = None
    task.status = "READY"
    task.current_node = nodes[rewind_index]["node_code"]
    task.checkpoint_json = {
        "node_index": rewind_index,
        "current_node": task.current_node,
        "phase": "AWAITING_CONFIRMATION",
    }
    # 图恢复身份只由 thread_id 决定：退回节点必须换一个新的图实例。沿用旧 thread 会复用
    # 已经废弃的等待点（例如停在旧的人工复核等待点），新的等待点无法建立、任务会被误判为失败。
    task.thread_id = public_no("thread")
    db.flush()
    # 业务提交与图等待点分开提交：先落业务事实与下游重算，再单独建立新的图等待点。
    start_audit_interrupt(db, task)
    return {
        "task_no": task.task_no,
        "field_key": request.field_key,
        "material_key": request.material_key,
        "review_no": review.review_no,
        "fact_revision_no": new_fact.revision_no,
        "previous_revision_no": previous_revision_no,
        "before_value": fact.raw_value,
        "after_value": new_raw,
        "normalized_value": normalized_value,
        "status": new_status,
        "invalidated_reviews": invalidated_reviews + invalidation["invalidated_reviews"],
        "invalidated_rule_results": invalidation["invalidated_rule_results"],
        "recomputed_rules": recomputed_rules,
        "next_node_code": task.current_node,
    }

def reconcile_ocr(db: Session, task_no: str, document_no: str, request: OcrReconcileRequest) -> dict[str, Any]:
    """OCR 提交结果未知时的人工对账出口。

    系统没有真实的供应商幂等/查询能力时既不自动重发也不标成功：
    ADOPT_JOB 按人工确认的作业号接管并续查；RETRY_SUBMIT 由人工明确放弃未知状态并重新提交。
    本地等待超时只让本次操作失败，不改变产物状态机。
    """
    task = task_or_404(db, task_no, lock=True)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("TASK_DOCUMENT_LOCKED", "任务已结束，不能对账 OCR 作业", status_code=409)
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能对账 OCR 作业", status_code=409)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    if not document.current_upload_id:
        raise BusinessError("OCR_UPLOAD_MISSING", "该材料位没有当前上传文件，不能对账 OCR 作业", status_code=409)
    upload = db.get(DocumentUpload, document.current_upload_id)
    if not upload:
        raise BusinessError("OCR_UPLOAD_MISSING", "该材料位当前上传文件不存在", status_code=500)
    artifact = _artifact_for_upload(db, upload, "OCR")
    if not artifact:
        raise BusinessError("OCR_ARTIFACT_MISSING", "该材料位当前上传没有 OCR 作业记录", status_code=404)
    if request.action == "ADOPT_JOB":
        if artifact.status not in {"SUBMITTING", "PROCESSING"}:
            raise BusinessError(
                "OCR_RECONCILE_STATUS_INVALID",
                "当前 OCR 作业状态明确，不能改挂人工提供的作业号",
                status_code=409,
                details={"artifact_no": artifact.artifact_no, "artifact_type": "OCR"},
            )
        job_id = (request.job_id or "").strip()
        if not job_id:
            raise BusinessError("OCR_JOB_ID_REQUIRED", "采用已确认作业时必须提供非空作业号")
        adopted_at = datetime.now(UTC)
        deadline_at = adopted_at + timedelta(seconds=get_settings().ocr_max_wait_seconds)
        artifact.status = "PENDING"
        artifact.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.pending"
        # 人工接管的事实与来源必须落库，后续查询据此说明这个作业是谁、为什么确认的。
        artifact.metadata_json = {
            "job_id": job_id,
            "deadline_at": deadline_at.isoformat(),
            "adopted_manually": True,
            "adopt_reason": request.reason,
            "adopted_at": adopted_at.isoformat(),
        }
        db.commit()
        return {
            "document_no": document.document_no,
            "artifact_no": artifact.artifact_no,
            "status": artifact.status,
            "job_id": job_id,
            "deadline_at": deadline_at,
            "message": "已按人工确认的作业号接管本次 OCR 作业，后续按该作业查询结果",
        }
    if artifact.status not in {"SUBMITTING", "PROCESSING"}:
        raise BusinessError(
            "OCR_RECONCILE_STATUS_INVALID",
            "当前 OCR 作业状态明确，不能重新提交（会重复产生外部作业）",
            status_code=409,
            details={"artifact_no": artifact.artifact_no, "artifact_type": "OCR"},
        )
    parsed_artifact = db.scalar(
        select(StoredArtifact).where(
            StoredArtifact.document_upload_id == upload.id,
            StoredArtifact.artifact_type == "DOCUMENT_PARSE",
            StoredArtifact.upload_sha256 == upload.sha256,
            StoredArtifact.status == "READY",
        )
    )
    if not parsed_artifact:
        raise BusinessError("PROCESSED_ARTIFACT_MISSING", "当前上传缺少可用的 8010 处理产物，无法重新提交 OCR", status_code=409)
    previous_metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    submit_attempts = list(previous_metadata.get("submit_attempts") or [])
    submit_attempts.append(
        {
            "at": datetime.now(UTC).isoformat(),
            "reason": request.reason,
            "previous_status": artifact.status,
        }
    )
    db.delete(artifact)
    # 仍在等待提交结果的 OCR 占位不可能被证据引用：下游节点只有在 OCR 产物 READY 后才可能运行，
    # 因此删除它不会破坏 evidence.artifact_id 的外键，也不会丢掉已确认的引用原文。
    db.commit()
    # 这是人工明确的重新提交动作（不是自动重试），允许产生新的外部作业。
    record = _submit_ocr_job(db, task, upload, parsed_artifact)
    record.metadata_json = {**(record.metadata_json or {}), "submit_attempts": submit_attempts}
    db.commit()
    job_id, deadline_at = _ocr_job_metadata(record)
    return {
        "document_no": document.document_no,
        "artifact_no": record.artifact_no,
        "status": record.status,
        "job_id": job_id,
        "deadline_at": deadline_at,
        "message": "已按人工明确动作重新提交 OCR 作业，并记录了本次提交尝试",
    }
