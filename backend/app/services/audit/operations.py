"""审核执行、字段核对确认、材料核对与 OCR 重试的后台命令提交。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...constants.status import TASK_STATUS_COMPLETED, TASK_STATUS_WAITING_FIELD_REVIEW
from ...entity.tasks import AuditTask, NodeExecution
from ...schema.dto.tasks import ExecuteNodeRequest, FieldReviewConfirmRequest
from ..shared.commands import _idempotency_key, _reject_unsupported_resume
from ..system import background as background_service
from ..system.background import (
    PHASE_FIELD_REVIEW_WAITING,
    PHASE_GRAPH_ADVANCED,
    PHASE_INTERRUPT_WAITING,
)
from ..system.operation_types import OBJECT_AUDIT_TASK


def _result_phase(result: dict[str, Any] | None) -> str:
    """本轮结束时任务停在哪个明确阶段；等待字段核对与等待恢复必须分开显示。"""
    status = (result or {}).get("task_status")
    if status == TASK_STATUS_COMPLETED:
        return PHASE_GRAPH_ADVANCED
    if status == TASK_STATUS_WAITING_FIELD_REVIEW:
        return PHASE_FIELD_REVIEW_WAITING
    return PHASE_INTERRUPT_WAITING


def _reject_stale_input_revision(task: AuditTask, input_revision: int | None) -> None:
    """提交者声明的输入修订与任务当前修订不一致时明确拒绝。

    ExecuteNodeRequest / ManualReviewRequest / FieldCorrectionRequest 的契约都承诺
    「与任务当前修订不一致时明确冲突，不执行旧输入」；校验放在受理侧（validate），
    幂等重放在此之前已返回，不影响「响应丢失后重发拿回原操作」的语义。
    """
    if input_revision is None or int(input_revision) == int(task.input_revision):
        return
    raise BusinessError(
        "INPUT_REVISION_CONFLICT",
        "任务输入在提交期间已被其他操作更新，本次提交基于过期输入；请刷新任务后重新提交",
        status_code=409,
        details={"submitted": int(input_revision), "current": int(task.input_revision)},
    )


def submit_node_execution(
    db: Session,
    task: AuditTask,
    *,
    node_code: str,
    idempotency_key: str | None,
    input_revision: int | None = None,
) -> tuple[Any, bool]:
    """提交审核节点执行或失败后的手动重试，新一轮执行使用新的幂等键。"""
    from . import commands as audit_commands

    request = {"task_no": task.task_no, "node_code": node_code, "input_revision": input_revision}

    def validate() -> None:
        if task.status == "COMPLETED":
            raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
        if task.status == TASK_STATUS_WAITING_FIELD_REVIEW:
            raise BusinessError(
                "AUDIT_TASK_WAITING_FIELD_REVIEW",
                "任务正在等待字段核对，请在字段核对中确认后继续审核；不能另建批次绕过核对",
                status_code=409,
            )
        if task.status == "RUNNING":
            raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行，不能并发执行或重跑", status_code=409)
        if db.scalar(select(NodeExecution.id).where(
            NodeExecution.audit_task_id == task.id, NodeExecution.status == "RUNNING",
        ).limit(1)) is not None:
            raise BusinessError("AUDIT_NODE_RUNNING", "任务已有执行中的批次，不能重复提交", status_code=409)
        if node_code != task.current_node:
            raise BusinessError("AUDIT_NODE_MISMATCH", "提交节点与任务当前节点不一致", status_code=409)
        _reject_stale_input_revision(task, input_revision)

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        _reject_unsupported_resume(command)
        content = command.request
        result = audit_commands.execute_current_node(
            session,
            content["task_no"],
            ExecuteNodeRequest(node_code=content["node_code"], input_revision=content.get("input_revision")),
        )
        # 节点执行后图已推进；任务未结束则停在字段核对或恢复等待点。
        command.phase(_result_phase(result))
        return result

    return background_service.submit_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        operation_type="NODE_EXECUTE",
        idempotency_key=_idempotency_key(idempotency_key),
        request=request,
        runner=runner,
        target_node=node_code,
        input_version=str(task.input_revision),
        validate=validate,
    )


def submit_node_resume(
    db: Session,
    task: AuditTask,
    *,
    idempotency_key: str | None,
    input_revision: int | None = None,
) -> tuple[Any, bool]:
    """提交「继续执行」：在同一执行批次里重跑未完成规则及其下游。

    与「全部重新执行」不同，继续执行不失效旧批次、不重跑不受影响的已有结果。
    目标批次只在首次受理时确定：同一幂等键的重发沿用原受理记录里的批次，
    不按提交时的任务状态重新推导，否则响应丢失后的重发会被误判成不可恢复。
    """
    from . import resume as audit_resume

    key = _idempotency_key(idempotency_key)
    existing = background_service.existing_operation(
        db, object_type=OBJECT_AUDIT_TASK, object_no=task.task_no,
        operation_type="NODE_RESUME", idempotency_key=key,
    )
    if existing is not None:
        stored = existing.request_json if isinstance(existing.request_json, dict) else {}
        execution_id = stored.get("execution_id")
        if not isinstance(execution_id, int):
            raise BusinessError(
                "OPERATION_REQUEST_INVALID", "原续跑命令缺少目标批次，无法按同一请求重放",
                status_code=500, details={"operation_no": existing.operation_no},
            )
    else:
        state = audit_resume.resume_state_of_task(db, task)
        if state["execution_id"] is None:
            raise BusinessError(
                "AUDIT_EXECUTION_NOT_RESUMABLE",
                state["blocked_reason"] or "当前没有可继续的未完成批次",
                status_code=409,
                details={"task_no": task.task_no, "status": task.status},
            )
        execution_id = state["execution_id"]
    request = {"task_no": task.task_no, "execution_id": execution_id, "input_revision": input_revision}

    def validate() -> None:
        if task.status == "COMPLETED":
            raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
        if task.status == "RUNNING":
            raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行，不能并发恢复", status_code=409)
        if db.scalar(select(NodeExecution.id).where(
            NodeExecution.audit_task_id == task.id, NodeExecution.status == "RUNNING",
        ).limit(1)) is not None:
            raise BusinessError("AUDIT_NODE_RUNNING", "任务已有执行中的批次，不能重复提交", status_code=409)
        target = db.get(NodeExecution, execution_id)
        if target is None or target.audit_task_id != task.id:
            raise BusinessError(
                "AUDIT_EXECUTION_MISSING", "恢复目标执行批次不存在", status_code=404,
                details={"execution_id": execution_id},
            )
        if audit_resume.resumable_execution(task, target) is None:
            raise BusinessError(
                "AUDIT_EXECUTION_NOT_RESUMABLE",
                audit_resume.resume_blocked_reason(task, target) or "该执行批次没有可继续的未完成项",
                status_code=409,
                details={"execution_id": execution_id, "status": target.status,
                         "failure_code": target.failure_code},
            )
        _reject_stale_input_revision(task, input_revision)

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        _reject_unsupported_resume(command)
        content = command.request
        result = audit_resume.resume_interrupted_execution(
            session, content["task_no"], execution_id=content["execution_id"],
        )
        command.phase(_result_phase(result))
        return result

    return background_service.submit_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        operation_type="NODE_RESUME",
        idempotency_key=key,
        request=request,
        runner=runner,
        target_node=task.current_node,
        input_version=str(task.input_revision),
        validate=validate,
    )


def _reject_stale_field_versions(
    db: Session, task: AuditTask, corrections: list[Any],
) -> None:
    """页面看到的字段版本必须仍是当前版本，旧页面不能覆盖新输入。"""
    from .facts import _current_field_fact_pairs

    pairs = _current_field_fact_pairs(db, task)
    for correction in corrections:
        current = pairs.get((correction.field_key, correction.material_key))
        revision = current[0].revision_no if current is not None else None
        if revision != correction.expected_revision_no:
            raise BusinessError(
                "FIELD_REVISION_CONFLICT",
                "字段取值在核对期间已被其他操作更新，本次提交基于过期页面；请刷新后重新核对",
                status_code=409,
                details={
                    "field_key": correction.field_key,
                    "material_key": correction.material_key,
                    "submitted": correction.expected_revision_no,
                    "current": revision,
                },
            )


def submit_field_review_confirm(
    db: Session,
    task: AuditTask,
    request: FieldReviewConfirmRequest,
) -> tuple[Any, bool]:
    """提交「确认并继续审核」：保存本次剩余修改、记录确认，再受理后续执行。

    同一幂等标识的重发先返回原受理记录，不按当前状态重新推导目标批次：
    响应丢失后重发必须拿回同一次受理，不能因为任务已进入执行而变成状态错误。
    """
    from . import field_review as audit_field_review
    from .commands import _waiting_review_execution

    key = _idempotency_key(request.idempotency_key)
    existing = background_service.existing_operation(
        db, object_type=OBJECT_AUDIT_TASK, object_no=task.task_no,
        operation_type="FIELD_REVIEW_CONFIRM", idempotency_key=key,
    )
    if existing is not None:
        stored = existing.request_json if isinstance(existing.request_json, dict) else {}
        execution_id = stored.get("execution_id")
        if not isinstance(execution_id, int):
            raise BusinessError(
                "OPERATION_REQUEST_INVALID", "原确认命令缺少目标批次，无法按同一请求重放",
                status_code=500, details={"operation_no": existing.operation_no},
            )
    else:
        execution_id = _waiting_review_execution(db, task).id
    corrections = [correction.model_dump() for correction in request.corrections]
    payload = {
        "task_no": task.task_no,
        "execution_id": execution_id,
        "input_revision": request.input_revision,
        "corrections": corrections,
        "note": request.note,
    }

    def validate() -> None:
        if task.status != TASK_STATUS_WAITING_FIELD_REVIEW:
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_NOT_WAITING",
                f"任务当前状态为 {task.status}，不在字段核对等待点，不能确认继续审核",
                status_code=409,
                details={"task_no": task.task_no, "status": task.status},
            )
        if db.scalar(select(NodeExecution.id).where(
            NodeExecution.audit_task_id == task.id, NodeExecution.status == "RUNNING",
        ).limit(1)) is not None:
            raise BusinessError("AUDIT_NODE_RUNNING", "任务已有执行中的批次，不能重复提交", status_code=409)
        _reject_stale_input_revision(task, request.input_revision)
        _reject_stale_field_versions(db, task, request.corrections)
        # 缺项不阻止继续，但必须由用户明确确认当前情况；不自动补值，也不默认通过。
        # 缺项按本次修改提交后的事实计算：改值会补上缺项，标记「材料中不存在」会新增缺项，
        # 后者同样要先让用户看到并确认。
        remaining_by_key = {
            (item["field_key"], item["material_key"]): item
            for item in audit_field_review.missing_fields(db, task)
        }
        field_names = {
            field["field_key"]: str(field.get("name") or field["field_key"])
            for field in task.task_schema_json["fields"]
        }
        for item in request.corrections:
            field_pair = (item.field_key, item.material_key)
            if item.decision == "MODIFY_FIELD":
                if (item.field_value or "").strip():
                    remaining_by_key.pop(field_pair, None)
            else:
                remaining_by_key.setdefault(field_pair, {
                    "field_key": item.field_key,
                    "material_key": item.material_key,
                    "field_name": field_names.get(item.field_key, item.field_key),
                })
        remaining = list(remaining_by_key.values())
        if remaining and not request.acknowledge_missing:
            names = "、".join(item["field_name"] for item in remaining[:8])
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_MISSING_UNACKNOWLEDGED",
                f"仍有 {len(remaining)} 项字段没有可执行取值（{names}），"
                "请先核对缺项；确认继续这些字段不会被补齐",
                status_code=409,
                details={"missing_fields": remaining},
            )

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        from . import commands as audit_commands

        _reject_unsupported_resume(command)
        content = command.request
        current = session.scalar(
            select(AuditTask).where(AuditTask.task_no == content["task_no"]).with_for_update()
        )
        if current is None:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404)
        _reject_stale_input_revision(current, content["input_revision"])
        from ...schema.dto.tasks import FieldReviewCorrection

        result = audit_commands.confirm_field_review(
            session,
            content["task_no"],
            execution_id=content["execution_id"],
            corrections=[FieldReviewCorrection(**item) for item in content["corrections"]],
            note=content.get("note"),
            operation_no=command.operation_no,
        )
        command.phase(_result_phase(result))
        return result

    return background_service.submit_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        operation_type="FIELD_REVIEW_CONFIRM",
        idempotency_key=key,
        request=payload,
        runner=runner,
        target_node=task.current_node,
        input_version=str(task.input_revision),
        validate=validate,
    )


def submit_document_classification(db: Session, task: AuditTask, upload) -> tuple[Any, bool]:
    """上传保存与分类命令在同一事务认领，提交后由独立会话执行 8018。"""
    request = {"task_no": task.task_no, "upload_id": upload.id, "input_revision": task.input_revision}

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        from ...entity.tasks import DocumentUpload, TaskDocument
        from ..shared.storage import resolve_storage_path
        from .artifacts import _ensure_external_artifact
        from .document_ports import document_service

        content = command.request
        current = session.scalar(select(AuditTask).where(AuditTask.task_no == content["task_no"]))
        record = session.get(DocumentUpload, content["upload_id"])
        document = session.get(TaskDocument, record.task_document_id) if record is not None else None
        if current is None or record is None or document is None or record.audit_task_id != current.id:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "上传分类命令对应的任务文件不存在", status_code=404)
        if not record.is_current or current.input_revision != content["input_revision"]:
            raise BusinessError("DOCUMENT_CLASSIFICATION_INPUT_STALE", "分类命令对应的文件版本已失效", status_code=409)
        artifact = _ensure_external_artifact(
            session, current, record, "MATERIAL_CLASSIFY",
            lambda: document_service().validate_material_type(
                file_path=resolve_storage_path(record.relative_path), filename=record.original_filename,
                content_type=record.content_type, material_type=record.declared_material_type,
            ),
        )
        session.commit()
        command.phase(background_service.PHASE_RESULT_READY)
        return {"task_no": current.task_no, "document_no": document.document_no, "artifact_no": artifact.artifact_no}

    return background_service.submit_operation(
        db, object_type=OBJECT_AUDIT_TASK, object_no=task.task_no,
        operation_type="DOCUMENT_CLASSIFY", idempotency_key=upload.upload_no,
        request=request, runner=runner, input_version=str(task.input_revision),
    )


def submit_document_ocr_retry(
    db: Session,
    task: AuditTask,
    *,
    document_no: str,
    artifact_no: str | None,
    input_revision: int,
    idempotency_key: str,
    resubmit_unknown: bool = False,
) -> tuple[Any, bool]:
    """为单份材料提交 OCR 续查/重试操作，不重新执行整个任务节点。"""
    from .commands import retry_document_ocr

    request = {
        "task_no": task.task_no,
        "document_no": document_no,
        "artifact_no": artifact_no,
        "input_revision": input_revision,
        "resubmit_unknown": resubmit_unknown,
    }

    def validate() -> None:
        if task.status in {"RUNNING", "COMPLETED"}:
            raise BusinessError("TASK_DOCUMENT_LOCKED", "任务正在执行或已结束，不能重试材料识别", status_code=409)
        if int(task.input_revision) != int(input_revision):
            raise BusinessError(
                "INPUT_REVISION_CONFLICT", "任务输入已更新，请刷新任务后再重试材料识别", status_code=409,
            )

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        current = session.scalar(select(AuditTask).where(AuditTask.task_no == command.request["task_no"]).with_for_update())
        if current is None:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404)
        _reject_stale_input_revision(current, command.request["input_revision"])
        return retry_document_ocr(
            session, current, command.request["document_no"], command.request.get("artifact_no"),
            operation_no=command.operation_no,
            resubmit_unknown=command.request["resubmit_unknown"],
        )

    return background_service.submit_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        operation_type="OCR_DOCUMENT_RETRY",
        idempotency_key=idempotency_key,
        request=request,
        runner=runner,
        input_version=str(input_revision),
        validate=validate,
    )
