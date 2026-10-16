"""审核的对外命令入口：节点执行、人工复核提交、字段纠错、OCR 对账、中断恢复。

这些是 API 与后台操作调用的唯一入口；每个入口在进入图之前先完成并发与状态校验
（任务已结束 / 正在执行 / 节点不一致 / 输入修订过期都明确拒绝），
再走「恢复图等待点」的路径，不留下绕开图的旁路。

失败语义：图运行时异常会把任务标记为 FAILED 并返回带摘要的错误，不重试、不静默；
checkpoint 存储暂时不可达保留确认/复核等待状态；若本次节点已经开始执行，则明确记录
执行中断并置 BLOCKED，不能留下没有实际执行过程的 RUNNING。"""

import logging
from ...config import get_settings
from ...domain.identity import public_no
from ...common.exceptions import BusinessError, exception_summary
from ...graphs.audit_graph import STORAGE_UNAVAILABLE, run_audit_node
from . import flow as audit_flow
from ...entity import (
    AuditTask,
    DocumentUpload,
    ManualReview,
    NodeExecution,
    StoredArtifact,
    TaskDocument,
)
from ...schema.dto import ExecuteNodeRequest, ExternalArtifactReconcileRequest, FieldCorrectionRequest, ManualReviewRequest, OcrReconcileRequest
from ..task_lookup import task_or_404
from .. import task_value_service
from .artifacts import (
    _artifact_for_upload, _artifact_payload, _complete_ocr_job, _ocr_job_metadata,
    _submit_authorized_ocr_retry, _submit_ocr_job,
)
from .evidence import _evidence_snapshot, _evidence_snapshots
from .facts import _current_field_fact_pairs, _write_human_field_fact
from .flow import _fail_running_execution, _mark_graph_runtime_failed, _raise_graph_error
from ..background_service import assert_no_active_operation
from ..operation_log_service import find_by_idempotency, record_operation, require_idempotent_replay
from ..operation_types import OBJECT_AUDIT_TASK
from .reviews import _decision_command_no, _invalidate_retry_results, _invalidate_validation_results, _next_node_for_recalculation
from .shared import _normalize_field_value, _task_field_definitions, _task_manifest
from .state import _scenario_nodes, _set_task_checkpoint
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
    interrupted = db.scalars(
        select(NodeExecution).join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
        .where(NodeExecution.status == "RUNNING", AuditTask.status != "WAITING_REVIEW")
    ).all()
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
    """节点执行命令入口：先做并发与状态校验，再一次性跑完当前节点的全部规则。

    执行不再有等待点，也没有「恢复」这一段：一次后台操作就是一次完整执行。
    """
    task = task_or_404(db, task_no, lock=True)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    # 必须在进入图之前就拦住并发，否则第二个请求会与正在执行的图互相覆盖。
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
        # 失败或阻断的任务需要重跑当前节点：旧执行记录由 _begin_node_execution 在真正
        # 开始时失效，原失败保留供审计。
        _, nodes, current_index = _scenario_nodes(db, task)
        task.checkpoint_json = {
            "node_index": current_index, "current_node": nodes[current_index]["node_code"],
            "phase": "AWAITING_CONFIRMATION",
        }
        db.flush()
    try:
        state = run_audit_node(task.task_no, db, audit_flow)
    except BusinessError as exc:
        if exc.code == "AUDIT_GRAPH_EXECUTION_FAILED":
            _mark_graph_runtime_failed(db, task_no, failure_code=exc.code,
                                       failure_message=str(exc.details.get("reason") or exc.message))
        elif exc.code == STORAGE_UNAVAILABLE:
            _fail_running_execution(db, task_no, code=exc.code,
                                    message=str(exc.details.get("reason") or exc.message))
        raise
    except Exception as exc:
        logger.exception("审核流程执行发生未处理异常", extra={"task_no": task_no, "node_code": request.node_code})
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError("AUDIT_GRAPH_RUNTIME_FAILED",
                            f"审核流程执行失败，任务已标记为失败（{exception_summary(exc)}）", status_code=500) from exc
    try:
        _raise_graph_error(state)
    except BusinessError as exc:
        if exc.code == "AUDIT_GRAPH_RESULT_INVALID":
            _mark_graph_runtime_failed(db, task_no)
        raise
    except Exception as exc:
        logger.exception("审核流程返回状态无效", extra={"task_no": task_no, "node_code": request.node_code})
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError("AUDIT_GRAPH_RUNTIME_FAILED",
                            f"审核流程返回状态无效，任务已标记为失败（{exception_summary(exc)}）", status_code=500) from exc
    result = state.get("last_execution")
    if not isinstance(result, dict):
        _mark_graph_runtime_failed(db, task_no)
        raise BusinessError("AUDIT_GRAPH_RESULT_MISSING", "审核流程未返回当前节点执行结果", status_code=500,
                            details={"phase": state.get("phase"), "execution_id": state.get("execution_id")})
    return result


def correct_field_value(db: Session, task_no: str, request: FieldCorrectionRequest) -> dict[str, Any]:
    """主动字段纠错入口：对 PRESENT 错值与 NOT_PRESENT 漏值统一处理。

    与人工复核共用同一套类型校验、事实版本与下游重算通道：生成新的不可变事实版本、
    把旧的退出当前位、失效依据旧事实的待办，并从实际引用该字段的节点开始重算。
    旧事实、旧复核与旧证据保持可追溯。
    """
    task = task_or_404(db, task_no, lock=True)
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
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
    # 取值在任务 CSV；表里只留版本与当前位。
    fact_value = task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FIELD_VALUE, fact.id)
    normalized_value: Any = None
    if request.decision == "MODIFY_FIELD":
        if request.field_value is None or not request.field_value.strip():
            raise BusinessError("FIELD_CORRECTION_VALUE_REQUIRED", "修改字段必须填写纠错后的字段值")
        try:
            # 只转换用户填写的接口类型，不从金额文本推断单位或币种。
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
        "raw_value": fact_value.get("raw_value"),
        "normalized_value": fact_value.get("normalized_value_json"),
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
        before_value=fact_value.get("raw_value"),
        after_value=new_raw,
        basis_type=request.basis_type,
        decision_command_no=_decision_command_no(db, task),
    )
    db.add(review)
    task.input_revision += 1
    db.flush()
    manifest = _task_manifest(task)
    _, nodes, current_index = _scenario_nodes(db, task)
    recomputed_rules = []
    invalidation = _invalidate_validation_results(db, task, manifest, {request.field_key})
    # 事实变化后按 DAG 取第一个尚未完成的启用节点，计算必须先于依赖它的校验和风险重跑。
    next_node = _next_node_for_recalculation(db, task, manifest, nodes)
    if next_node is None:
        # 没有规则、计算或报告消费该字段，且 DAG 已全部完成：字段事实仍应保存，
        # 不能把“无需重算”当成纠错失败。
        task.status = "COMPLETED"
        _set_task_checkpoint(task, node_index=current_index, phase="COMPLETED")
        db.flush()
        db.commit()
        return {
            "task_no": task.task_no,
            "field_key": request.field_key,
            "material_key": request.material_key,
            "review_no": review.review_no,
            "fact_revision_no": new_fact.revision_no,
            "previous_revision_no": previous_revision_no,
            "before_value": fact_value.get("raw_value"),
            "after_value": new_raw,
            "normalized_value": normalized_value,
            "status": new_status,
            "invalidated_reviews": invalidated_reviews + invalidation["invalidated_reviews"],
            "invalidated_rule_results": invalidation["invalidated_rule_results"],
            "recomputed_rules": recomputed_rules,
            "next_node_code": None,
        }
    rewind_index = next(index for index, item in enumerate(nodes) if item["node_code"] == next_node["node_code"])

    task.status = "READY"
    task.current_node = next_node["node_code"]
    task.checkpoint_json = {
        "node_index": rewind_index,
        "current_node": task.current_node,
        "phase": "AWAITING_CONFIRMATION",
    }
    db.flush()
    db.commit()
    return {
        "task_no": task.task_no,
        "field_key": request.field_key,
        "material_key": request.material_key,
        "review_no": review.review_no,
        "fact_revision_no": new_fact.revision_no,
        "previous_revision_no": previous_revision_no,
        "before_value": fact_value.get("raw_value"),
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
    if artifact_type not in {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL"}:
        raise BusinessError("ARTIFACT_TYPE_INVALID", "仅支持对账材料校验、去章或印章识别产物", status_code=400)
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


def _document_ocr_retry_target(
    db: Session, task: AuditTask, document_no: str, artifact_no: str | None,
) -> tuple[TaskDocument, DocumentUpload, StoredArtifact, StoredArtifact | None]:
    """续查只定位当前材料；未知提交仍由现有人工对账入口处理。"""
    if task.status in {"RUNNING", "COMPLETED", "FAILED"}:
        raise BusinessError("TASK_DOCUMENT_LOCKED", "任务正在执行或已结束，不能重试材料识别", status_code=409)
    document = db.scalar(select(TaskDocument).where(
        TaskDocument.audit_task_id == task.id, TaskDocument.document_no == document_no,
    ))
    if document is None:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    upload = db.get(DocumentUpload, document.current_upload_id) if document.current_upload_id else None
    if upload is None:
        raise BusinessError("OCR_UPLOAD_MISSING", "该材料位没有当前上传文件", status_code=409)
    artifact = _artifact_for_upload(db, upload, "OCR")
    if (artifact.artifact_no if artifact else None) != artifact_no:
        raise BusinessError("OCR_ARTIFACT_STALE", "OCR 作业记录已变化，请刷新后重试", status_code=409)
    if artifact is not None and artifact.status not in {"PENDING", "FAILED"}:
        raise BusinessError(
            "OCR_SUBMISSION_STATE_UNKNOWN", "OCR 提交结果未知，请在处理状态中核对原作业后再操作", status_code=409,
        )
    parsed = _artifact_for_upload(db, upload, "DOCUMENT_PARSE")
    if parsed is None or parsed.status != "READY":
        raise BusinessError("PROCESSED_ARTIFACT_MISSING", "该材料尚无已完成的去章产物，请先执行材料前置处理", status_code=409)
    return document, upload, parsed, artifact


def retry_document_ocr(
    db: Session, task: AuditTask, document_no: str, artifact_no: str | None, *, operation_no: str,
) -> dict[str, Any]:
    """只处理一份材料：续查原作业，或对明确失败的作业提交一次新识别。"""
    document, upload, parsed, artifact = _document_ocr_retry_target(db, task, document_no, artifact_no)
    if artifact is None:
        artifact = _submit_ocr_job(db, task, upload, parsed)
    elif artifact.status == "FAILED":
        metadata = dict(artifact.metadata_json or {})
        attempts = list(metadata.pop("submit_attempts", []) or [])
        attempt = {
            "at": datetime.now(UTC).isoformat(), "reason": "用户重试当前材料的 OCR 识别",
            "idempotency_key": operation_no, "state": "SUBMITTING",
            "previous_status": artifact.status, "previous_relative_path": artifact.relative_path,
            "previous_metadata": metadata,
        }
        artifact.status = "SUBMITTING"
        artifact.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.submitting"
        artifact.metadata_json = {**metadata, "submit_attempts": [*attempts, attempt]}
        db.commit()  # 新提交意图和旧作业记录先保存，重复点击不会再提交。
        try:
            artifact = _submit_authorized_ocr_retry(
                db, task, upload, parsed, artifact, idempotency_key=operation_no,
            )
        except Exception:
            db.commit()  # 保留真实的提交失败或结果未知状态，后台异常继续上抛。
            raise
    elif artifact.status == "PENDING":
        job_id, old_deadline = _ocr_job_metadata(artifact)
        resumed_at = datetime.now(UTC)
        artifact.metadata_json = {
            **artifact.metadata_json,
            "job_id": job_id,
            "deadline_at": (resumed_at + timedelta(seconds=get_settings().ocr_max_wait_seconds)).isoformat(),
            "previous_deadline_at": old_deadline.isoformat(),
            "resumed_at": resumed_at.isoformat(), "resume_operation_no": operation_no,
        }
        db.commit()  # 只延长这次用户主动续查的等待时间，不创建远端作业。
    artifact = _complete_ocr_job(db, task, upload, artifact)
    _artifact_payload(artifact)
    stages = [_artifact_for_upload(db, upload, stage) for stage in ("MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL", "OCR")]
    if all(item is not None and item.status == "READY" for item in stages):
        for item in stages:
            _artifact_payload(item)
        document.status = "PREPARED"
    db.commit()
    return {
        "document_no": document.document_no, "artifact_no": artifact.artifact_no,
        "status": artifact.status, "document_status": document.status,
        "message": "当前材料的正文识别结果已保存，其他材料的处理结果已保留",
    }


def reconcile_ocr(db: Session, task_no: str, document_no: str, request: OcrReconcileRequest) -> dict[str, Any]:
    """OCR 提交结果未知、等待超时或供应商明确失败后的人工处理出口。

    系统没有真实的供应商幂等/查询能力时既不自动重发也不标成功：
    ADOPT_JOB 按人工确认的作业号接管并续查；RETRY_SUBMIT 由人工明确重提未知、超时或失败的作业。
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
    if artifact.status not in {"SUBMITTING", "PROCESSING", "FAILED"} and not expired_pending:
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
