"""审核执行、材料核对与 OCR 重试的后台命令提交。"""

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from ...common.exceptions import BusinessError
from ...entity import AuditTask, NodeExecution
from ...schema.dto import ExecuteNodeRequest
from ..system import background as background_service
from ..system.background import PHASE_GRAPH_ADVANCED, PHASE_INTERRUPT_WAITING
from ..system.operation_types import OBJECT_AUDIT_TASK
from ..shared.commands import _idempotency_key, _reject_unsupported_resume


def _manifest_of_task(task: AuditTask) -> dict[str, Any]:
    """任务创建时冻结的方案 manifest。

    节点归属必须按它解析：`task.current_node` 是节点实例码（用户配置的），
    拿它与能力类型比较在用户改了实例码之后恒为假，人工复核等待点会永远判定不成立。
    """
    manifest = task.manifest_json
    if not isinstance(manifest, dict) or not manifest:
        raise BusinessError("TASK_MANIFEST_MISSING", "任务缺少已冻结的执行配置", status_code=500)
    return manifest


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
        # 节点执行后图已推进；任务未结束则重新停在节点确认等待点。
        command.phase(PHASE_GRAPH_ADVANCED if result.get("task_status") == "COMPLETED" else PHASE_INTERRUPT_WAITING)
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


def submit_document_classification(db: Session, task: AuditTask, upload) -> tuple[Any, bool]:
    """上传保存与分类命令在同一事务认领，提交后由独立会话执行 8018。"""
    request = {"task_no": task.task_no, "upload_id": upload.id, "input_revision": task.input_revision}

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        from ...entity import DocumentUpload, TaskDocument
        from .artifacts import _ensure_external_artifact
        from .document_ports import document_service
        from ..shared.storage import resolve_storage_path

        content = command.request
        current = session.scalar(select(AuditTask).where(AuditTask.task_no == content["task_no"]))
        record = session.get(DocumentUpload, content["upload_id"])
        document = session.get(TaskDocument, record.task_document_id) if record is not None else None
        if current is None or record is None or document is None or record.audit_task_id != current.id:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "上传分类命令对应的任务文件不存在", status_code=404)
        if document.current_upload_id != record.id or not record.is_current or current.input_revision != content["input_revision"]:
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
