"""只重试当前上传的一项外部处理，保留其他已成功产物。"""

from functools import partial

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.audit.artifact.execution import ArtifactExecution
from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
from app.modules.audit.artifact.service.document_ports import document_service
from app.modules.audit.shared import current_upload
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.system.operation.service import background
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK
from app.services.shared.storage import resolve_storage_path


class AuditArtifactMaterialRetryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _target(self, db: Session, task, document_no, artifact_type, request):
        if artifact_type not in {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL"}:
            raise BusinessError("ARTIFACT_TYPE_INVALID", "不支持重试该处理类型", status_code=400)
        if task.status in {"RUNNING", "COMPLETED"}:
            raise BusinessError("TASK_DOCUMENT_LOCKED", "任务正在执行或已结束，不能重试材料处理", status_code=409)
        if task.input_revision != request["input_revision"]:
            raise BusinessError("INPUT_REVISION_CONFLICT", "任务输入已更新，请刷新后重试", status_code=409)
        document = MaterialDocumentCRUD(db).by_task_and_no(task.id, document_no)
        upload = current_upload(db, document) if document else None
        if upload is None:
            raise BusinessError("TASK_DOCUMENT_UPLOAD_MISSING", "材料没有当前上传文件", status_code=409)
        artifact = AuditArtifactArtifactsService(db)._artifact_for_upload( upload, artifact_type)
        if artifact is None or artifact.artifact_no != request["artifact_no"]:
            raise BusinessError("ARTIFACT_VERSION_CONFLICT", "处理产物已更新，请刷新后重试", status_code=409)
        if artifact.status not in {"FAILED", "PROCESSING"}:
            raise BusinessError("ARTIFACT_NOT_RETRYABLE", "仅允许重试失败或结果未知的处理项", status_code=409)
        if artifact.status == "PROCESSING" and not request["resubmit_unknown"]:
            raise BusinessError("ARTIFACT_RETRY_CONFIRMATION_REQUIRED", "上次处理结果未知，请确认可能重复调用后重试",
                                status_code=409)
        return upload, artifact


    def submit_document_artifact_retry(self, task, *, document_no, artifact_type, payload):
        request = {"task_no": task.task_no, "document_no": document_no, "artifact_type": artifact_type,
                   **payload.model_dump(exclude={"idempotency_key"})}

        def validate():
            self._target(self.db, task, document_no, artifact_type, request)

        def runner(session, command):
            content = command.request
            current = AuditTaskCRUD(session).lock_by_no(content["task_no"])
            if current is None:
                raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404)
            upload, artifact = self._target(session, current, content["document_no"], content["artifact_type"], content)
            port = document_service()
            kwargs = {"file_path": resolve_storage_path(upload.relative_path), "filename": upload.original_filename,
                      "content_type": upload.content_type}
            if content["artifact_type"] == "MATERIAL_CLASSIFY":
                call = partial(port.validate_material_type, **kwargs, material_type=upload.declared_material_type)
            elif content["artifact_type"] == "DOCUMENT_PARSE":
                call = partial(port.process_document, **kwargs)
            else:
                call = partial(port.detect_seals, **kwargs)
            ArtifactExecution(session).execute_external_artifact_call( current, upload, artifact, call,
                                           operation_no=command.operation_no, resubmit_unknown=content["resubmit_unknown"])
            return {"task_no": current.task_no, "document_no": content["document_no"], "artifact_no": artifact.artifact_no}

        return background.OperationBackgroundService(self.db).submit_operation(object_type=OBJECT_AUDIT_TASK, object_no=task.task_no, operation_type="DOCUMENT_ARTIFACT_RETRY",
            idempotency_key=payload.idempotency_key, request=request, runner=runner,
            input_version=str(task.input_revision), validate=validate,
        )
