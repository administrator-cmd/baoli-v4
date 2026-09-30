"""审核的对外命令入口：节点执行、人工复核提交、字段纠错、OCR 对账、中断恢复。

这些是 API 与后台操作调用的唯一入口；每个入口在进入图之前先完成并发与状态校验
（任务已结束 / 正在执行 / 节点不一致 / 输入修订过期都明确拒绝），
再走「恢复图等待点」的路径，不留下绕开图的旁路。

失败语义：图运行时异常会把任务标记为 FAILED 并返回带摘要的错误，不重试、不静默；
checkpoint 存储暂时不可达只让本次操作失败（可重试），不改变任务状态。"""

import logging
from ...config import get_settings
from ...domain.identity import public_no
from ...common.exceptions import BusinessError, exception_summary
from ...graphs.audit_graph import resume_audit_confirmation, resume_audit_manual_review
from . import flow as audit_flow
from ...entities import (
    AuditTask,
    DocumentUpload,
    ManualReview,
    NodeExecution,
    StoredArtifact,
    TaskDocument,
    WorkflowCheckpoint,
)
from ...dto import ExecuteNodeRequest, ExternalArtifactReconcileRequest, FieldCorrectionRequest, ManualReviewRequest, OcrReconcileRequest, RecoverFailedNodeRequest
from ..task_lookup import task_or_404
from .artifacts import _artifact_for_upload, _ocr_job_metadata, _submit_authorized_ocr_retry
from .evidence import _evidence_snapshot, _evidence_snapshots
from .facts import _current_field_fact_pairs, _refresh_extraction_rule_results, _write_human_field_fact
from .flow import _raise_graph_error
from .graph_runtime import _ensure_audit_interrupt, _mark_graph_runtime_failed
from .interrupt import start_audit_interrupt
from .recovery import failed_node_recovery
from ..background_service import assert_no_active_operation
from ..operation_log_service import find_by_idempotency, record_operation, require_idempotent_replay
from ..operation_types import OBJECT_AUDIT_TASK
from .reviews import _current_review_batch, _decision_command_no, _invalidate_retry_results, _invalidate_validation_results, _next_node_for_recalculation, _review_hash, _validate_review_payloads
from .shared import _normalize_field_value, _task_field_definitions, _task_manifest
from .state import _node_codes_of_type, _save_flow_checkpoint, _scenario_nodes, _set_task_checkpoint, _single_node_code_of_type
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
    interrupted_ocr_retries = 0
    for artifact in db.scalars(
        select(StoredArtifact).where(
            StoredArtifact.artifact_type == "OCR",
            StoredArtifact.status == "SUBMITTING",
        )
    ).all():
        metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        attempts = list(metadata.get("submit_attempts") or [])
        if not attempts or attempts[-1].get("state") != "SUBMITTING":
            continue
        attempts[-1] = {**attempts[-1], "state": "UNKNOWN", "failure_code": "OCR_RETRY_INTERRUPTED"}
        artifact.metadata_json = {**metadata, "submit_attempts": attempts}
        interrupted_ocr_retries += 1
    db.commit()
    return {
        "interrupted_executions": len(interrupted),
        "blocked_tasks": len(running_tasks),
        "interrupted_ocr_retries": interrupted_ocr_retries,
    }

def execute_current_node(db: Session, task_no: str, request: ExecuteNodeRequest) -> dict[str, Any]:
    """节点执行命令入口：先做并发与状态校验（任务已结束 / 正在执行 / 节点不一致都明确拒绝），再恢复图执行。"""
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
    if task.status == "BLOCKED":
        # 失败或阻断的图已离开确认 interrupt。重新执行当前节点必须建立新的图等待点；
        # 旧执行记录由 _begin_node_execution 在真正开始时失效，保留原失败供审计。
        latest_execution = db.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code == task.current_node,
                NodeExecution.status.in_(["FAILED", "BLOCKED"]),
            )
            .order_by(NodeExecution.id.desc())
        )
        if latest_execution is None:
            raise BusinessError(
                "AUDIT_EXECUTION_NOT_RECOVERABLE",
                "当前节点没有可重新执行的失败记录",
                status_code=409,
            )
        _, nodes, current_index = _scenario_nodes(db, task)
        db.execute(
            update(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
            .values(status="INVALIDATED")
        )
        task.thread_id = public_no("thread")
        task.checkpoint_json = {
            "node_index": current_index,
            "current_node": nodes[current_index]["node_code"],
            "phase": "AWAITING_CONFIRMATION",
        }
        db.flush()
        start_audit_interrupt(db, task)
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


def recover_failed_checkpoint_node(
    db: Session,
    task_no: str,
    request: RecoverFailedNodeRequest,
) -> AuditTask:
    """恢复检查点丢失或图等待点初始化失败的当前节点。

    这不是通用的 FAILED 重试，也不把数据库里的旧检查点改回 WAITING：旧检查点
    必须明确标记初始化失败，或最近一次节点命令确为检查点丢失；当前节点不能
    留有有效的执行结果。
    旧执行、旧检查点和材料全部保留，
    只让当前节点及其后继结果失效，然后用新的 thread/namespace 产生真实 interrupt。
    """
    task = task_or_404(db, task_no, lock=True)
    recovery = failed_node_recovery(db, task)
    if request.input_revision != task.input_revision:
        raise BusinessError(
            "AUDIT_INPUT_REVISION_STALE",
            "任务输入已变化，请刷新任务后再恢复",
            status_code=409,
        )
    if request.node_code != task.current_node:
        raise BusinessError("AUDIT_NODE_MISMATCH", "恢复节点与任务当前节点不一致", status_code=409)

    # 只失效当前节点及其后继，不动已经完成的上游节点；历史记录仍可审计。
    _invalidate_retry_results(db, task, recovery.nodes, recovery.current_index)
    task.status = "READY"
    task.pending_review_hash = None
    task.checkpoint_json = {
        "node_index": recovery.current_index,
        "current_node": task.current_node,
        "phase": "AWAITING_CONFIRMATION",
    }
    # 图恢复身份只由 thread_id 决定。新线程与新业务 namespace 使旧的丢失等待点
    # 永远不会被误当成新的等待点；旧 WorkflowCheckpoint 保持 FAILED 不被改写。
    old_thread_id = task.thread_id
    task.thread_id = public_no("thread")
    db.flush()
    start_audit_interrupt(db, task)

    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="NODE_RECOVER",
        detail={
            "node_code": task.current_node,
            "failed_execution_id": recovery.latest_execution.id if recovery.latest_execution is not None else None,
            "old_thread_id": old_thread_id,
            "new_thread_id": task.thread_id,
            "old_checkpoint_id": recovery.old_checkpoint.id,
            "source_operation_no": recovery.source_operation_no,
            "init_failure_code": recovery.init_failure_code,
        },
        result="SUCCEEDED",
    )
    db.commit()
    db.refresh(task)
    return task

def submit_manual_reviews(db: Session, task_no: str, request: ManualReviewRequest) -> dict[str, Any]:
    """人工复核提交命令入口：校验等待状态与待复核集合身份后交回图执行；幂等复用由 operation_gateway 先行处理。"""
    task = task_or_404(db, task_no, lock=True)
    if task.status != "WAITING_REVIEW" or task.current_node != _single_node_code_of_type(_task_manifest(task), "MANUAL_REVIEW", purpose="人工复核等待点"):
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
    把旧的退出当前位、失效依据旧事实的待办，并从实际引用该字段的节点开始重算。
    旧事实、旧复核与旧证据保持可追溯。
    """
    task = task_or_404(db, task_no, lock=True)
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
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
    current_pair = _current_field_fact_pairs(db, task).get((request.field_key, request.material_key))
    if current_pair is None:
        raise BusinessError(
            "FIELD_FACT_MISSING",
            "当前上传没有成对生成的字段版本与统一事实，不能纠错",
            status_code=404,
            details={"field_key": request.field_key},
        )
    fact, _ = current_pair
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
                extraction_strategy=field.get("extraction_strategy"),
                structure_schema=field.get("structure_schema"),
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
        "evidences": _evidence_snapshots(db, fact.id),
    }
    new_fact, previous_revision_no, invalidated_reviews = _write_human_field_fact(
        db,
        task,
        field_key=request.field_key,
        material_key=request.material_key,
        document_upload_id=document.current_upload_id,
        status=new_status,
        raw_value=new_raw,
        normalized_value=normalized_value,
    )
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
    manifest = _task_manifest(task)
    _, nodes, current_index = _scenario_nodes(db, task)
    # 节点实例码由用户配置：按**能力类型**解析成实例码，不写死能力类型当实例码。
    extraction_codes = _node_codes_of_type(manifest, "FIELD_EXTRACTION")
    extraction_indexes = [index for index, item in enumerate(nodes) if item["node_code"] in extraction_codes]
    # 只有已经越过字段抽取节点才有可重算的抽取规则结果；否则抽取重跑时会自然重建。
    recomputed_rules = (
        _refresh_extraction_rule_results(db, task, manifest)
        if extraction_indexes and current_index > min(extraction_indexes)
        else []
    )
    invalidation = _invalidate_validation_results(db, task, manifest, {request.field_key})
    # 事实变化后按 DAG 取第一个尚未完成的启用节点，计算必须先于依赖它的校验和风险重跑。
    next_node = _next_node_for_recalculation(db, task, manifest, nodes)
    if next_node is None:
        # 没有规则、计算或报告消费该字段，且 DAG 已全部完成：字段事实仍应保存，
        # 不能把“无需重算”当成纠错失败。
        task.pending_review_hash = None
        task.status = "COMPLETED"
        _set_task_checkpoint(task, node_index=current_index, phase="COMPLETED")
        _save_flow_checkpoint(db, task)
        db.commit()
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
            "next_node_code": None,
        }
    rewind_index = next(index for index, item in enumerate(nodes) if item["node_code"] == next_node["node_code"])
    db.execute(
        update(WorkflowCheckpoint)
        .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
        .values(status="INVALIDATED")
    )
    task.pending_review_hash = None
    task.status = "READY"
    task.current_node = next_node["node_code"]
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

def reconcile_external_artifact(
    db: Session,
    task_no: str,
    document_no: str,
    artifact_type: str,
    request: ExternalArtifactReconcileRequest,
) -> dict[str, Any]:
    """人工确认同步外部调用状态未知，解除占位以允许重新执行当前节点。"""
    if artifact_type not in {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE"}:
        raise BusinessError("ARTIFACT_TYPE_INVALID", "仅支持对账 8018 或 8010 文件处理产物", status_code=400)
    task = task_or_404(db, task_no, lock=True)
    operation_request = {
        "task_no": task_no,
        "document_no": document_no,
        "artifact_type": artifact_type,
        "artifact_no": request.artifact_no,
        "reason": request.reason,
    }
    prior_operation, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_AUDIT_TASK,
        operation_type="ARTIFACT_RECONCILE",
        idempotency_key=request.idempotency_key,
        request=operation_request,
    )
    if prior_operation is not None:
        return dict((prior_operation.detail_json or {})["response"])
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    if task.status in {"COMPLETED", "FAILED", "RUNNING"}:
        raise BusinessError("AUDIT_TASK_NOT_RECONCILABLE", "任务当前不能对账文件处理产物", status_code=409)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if document is None or document.current_upload_id is None:
        raise BusinessError("OCR_UPLOAD_MISSING", "该材料位没有当前上传文件", status_code=404)
    upload = db.get(DocumentUpload, document.current_upload_id)
    if upload is None:
        raise BusinessError("OCR_UPLOAD_MISSING", "该材料位当前上传文件不存在", status_code=500)
    artifact = _artifact_for_upload(db, upload, artifact_type)
    if artifact is None or artifact.artifact_no != request.artifact_no or artifact.status != "PROCESSING":
        raise BusinessError(
            "ARTIFACT_RECONCILE_STATUS_INVALID",
            "当前文件没有结果未知的处理占位，不能解除",
            status_code=409,
        )
    result = {
        "document_no": document_no,
        "artifact_type": artifact_type,
        "artifact_no": artifact.artifact_no,
        "status": "CLEARED",
        "message": "结果未知的占位已解除；重新执行当前节点会再次调用外部服务",
    }
    db.delete(artifact)
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="ARTIFACT_RECONCILE",
        detail={"request": operation_request, "response": result},
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
    )
    db.commit()
    return result


def reconcile_ocr(db: Session, task_no: str, document_no: str, request: OcrReconcileRequest) -> dict[str, Any]:
    """OCR 提交结果未知时的人工对账出口。

    系统没有真实的供应商幂等/查询能力时既不自动重发也不标成功：
    ADOPT_JOB 按人工确认的作业号接管并续查；RETRY_SUBMIT 由人工明确放弃未知状态并重新提交。
    本地等待超时只让本次操作失败，不改变产物状态机。
    """
    task = task_or_404(db, task_no, lock=True)
    operation_request = {
        "task_no": task_no,
        "document_no": document_no,
        "action": request.action,
        "artifact_no": request.artifact_no,
        "job_id": request.job_id,
        "reason": request.reason,
    }
    prior_operation, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_AUDIT_TASK,
        operation_type="OCR_RECONCILE",
        idempotency_key=request.idempotency_key,
        request=operation_request,
    )
    if prior_operation is not None:
        if request.action == "RETRY_SUBMIT":
            outcome = find_by_idempotency(
                db,
                object_type=OBJECT_AUDIT_TASK,
                operation_type="OCR_RETRY_OUTCOME",
                idempotency_key=request.idempotency_key,
            )
            if outcome is None:
                submitted = db.scalar(
                    select(StoredArtifact).where(
                        StoredArtifact.audit_task_id == task.id,
                        StoredArtifact.artifact_no == request.artifact_no,
                        StoredArtifact.artifact_type == "OCR",
                    )
                )
                metadata = submitted.metadata_json if submitted and isinstance(submitted.metadata_json, dict) else {}
                attempts = metadata.get("submit_attempts") or []
                attempt = next(
                    (
                        item for item in attempts
                        if isinstance(item, dict)
                        and item.get("idempotency_key") == request.idempotency_key
                        and item.get("request_hash") == request_hash
                        and item.get("state") == "PENDING"
                    ),
                    None,
                )
                if attempt is not None:
                    job_id = attempt.get("job_id")
                    deadline_raw = attempt.get("deadline_at")
                    if not isinstance(job_id, str) or not job_id or not isinstance(deadline_raw, str):
                        raise BusinessError("OCR_RETRY_RESULT_INVALID", "已提交的 OCR 作业缺少作业身份", status_code=500)
                    try:
                        deadline_at = datetime.fromisoformat(deadline_raw)
                    except ValueError as exc:
                        raise BusinessError("OCR_RETRY_RESULT_INVALID", "已提交的 OCR 作业截止时间无效", status_code=500) from exc
                    return {
                        "document_no": document_no,
                        "artifact_no": submitted.artifact_no,
                        "status": "PENDING",
                        "job_id": job_id,
                        "deadline_at": deadline_at,
                        "message": "已按人工明确动作重新提交 OCR 作业，并记录了本次提交尝试",
                    }
                raise BusinessError(
                    "OCR_RETRY_OUTCOME_UNKNOWN",
                    "该人工重提请求已受理，但外部提交结果尚未确认，不能重复提交",
                    status_code=409,
                    details={"artifact_no": request.artifact_no},
                )
            if outcome.result == "FAILED":
                raise BusinessError(
                    outcome.failure_code or "OCR_RETRY_FAILED",
                    outcome.failure_message or "OCR 人工重提失败",
                    status_code=int((outcome.detail_json or {})["status_code"]),
                )
            return dict((outcome.detail_json or {})["response"])
        return dict((prior_operation.detail_json or {})["response"])
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
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
    if artifact.artifact_no != request.artifact_no:
        raise BusinessError("OCR_ARTIFACT_STALE", "OCR 作业记录已变化，请刷新后重新确认", status_code=409)
    current_metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    current_attempts = current_metadata.get("submit_attempts") or []
    if current_attempts and current_attempts[-1].get("state") == "SUBMITTING":
        raise BusinessError(
            "OCR_SUBMISSION_STATE_UNKNOWN",
            "已有人工重提请求正在提交或结果未知，请先核对，不能并发提交",
            status_code=409,
        )
    expired_pending = (
        artifact.status == "PENDING"
        and _ocr_job_metadata(artifact)[1].astimezone(UTC) <= datetime.now(UTC)
    )
    if request.action == "ADOPT_JOB":
        if artifact.status not in {"SUBMITTING", "PROCESSING"} and not expired_pending:
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
        previous_metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        artifact.metadata_json = {
            "job_id": job_id,
            "deadline_at": deadline_at.isoformat(),
            "adopted_manually": True,
            "adopt_reason": request.reason,
            "adopted_at": adopted_at.isoformat(),
            "previous_job_id": previous_metadata.get("job_id"),
            "submit_attempts": list(previous_metadata.get("submit_attempts") or []),
        }
        result = {
            "document_no": document.document_no,
            "artifact_no": artifact.artifact_no,
            "status": artifact.status,
            "job_id": job_id,
            "deadline_at": deadline_at,
            "message": "已按人工确认的作业号接管本次 OCR 作业，后续按该作业查询结果",
        }
        record_operation(
            db,
            object_type=OBJECT_AUDIT_TASK,
            object_no=task.task_no,
            object_name=task.task_name,
            operation_type="OCR_RECONCILE",
            detail={"request": operation_request, "response": {**result, "deadline_at": deadline_at.isoformat()}},
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
        )
        db.commit()
        return result
    if artifact.status not in {"SUBMITTING", "PROCESSING"} and not expired_pending:
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
    previous_attempts = list(previous_metadata.get("submit_attempts") or [])
    previous_job_metadata = {key: value for key, value in previous_metadata.items() if key != "submit_attempts"}
    attempt = {
        "at": datetime.now(UTC).isoformat(),
        "reason": request.reason,
        "idempotency_key": request.idempotency_key,
        "request_hash": request_hash,
        "state": "SUBMITTING",
        "previous_status": artifact.status,
        "previous_relative_path": artifact.relative_path,
        "previous_metadata": previous_job_metadata,
    }
    artifact.status = "SUBMITTING"
    artifact.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.submitting"
    artifact.metadata_json = {**previous_job_metadata, "submit_attempts": [*previous_attempts, attempt]}
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="OCR_RECONCILE",
        detail={
            "request": operation_request,
            "intent": {
                "artifact_no": artifact.artifact_no,
                "previous_status": attempt["previous_status"],
                "previous_job_id": previous_metadata.get("job_id"),
                "previous_deadline_at": previous_metadata.get("deadline_at"),
                "previous_attempts": previous_attempts,
            },
        },
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        result="ACCEPTED",
    )
    # 授权、幂等身份和提交占位同一事务持久化，之后才联系供应商。
    db.commit()
    try:
        record = _submit_authorized_ocr_retry(
            db, task, upload, parsed_artifact, artifact, idempotency_key=request.idempotency_key
        )
    except BusinessError as exc:
        record_operation(
            db,
            object_type=OBJECT_AUDIT_TASK,
            object_no=task.task_no,
            object_name=task.task_name,
            operation_type="OCR_RETRY_OUTCOME",
            detail={"status_code": exc.status_code, "artifact_no": artifact.artifact_no},
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            result="FAILED",
            failure_code=exc.code,
            failure_message=exc.message,
        )
        db.commit()
        raise
    except Exception as exc:
        record_operation(
            db,
            object_type=OBJECT_AUDIT_TASK,
            object_no=task.task_no,
            object_name=task.task_name,
            operation_type="OCR_RETRY_OUTCOME",
            detail={"status_code": 500, "artifact_no": artifact.artifact_no},
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            result="FAILED",
            failure_code="OCR_RETRY_UNEXPECTED_ERROR",
            failure_message=exception_summary(exc),
        )
        db.commit()
        raise
    job_id, deadline_at = _ocr_job_metadata(record)
    result = {
        "document_no": document.document_no,
        "artifact_no": record.artifact_no,
        "status": record.status,
        "job_id": job_id,
        "deadline_at": deadline_at,
        "message": "已按人工明确动作重新提交 OCR 作业，并记录了本次提交尝试",
    }
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="OCR_RETRY_OUTCOME",
        detail={"response": {**result, "deadline_at": deadline_at.isoformat()}},
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
    )
    db.commit()
    return result
