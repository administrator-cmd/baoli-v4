"""远端材料处理编排：调用前占位，阶段进度与失败独立提交。"""

from datetime import UTC, datetime
from typing import Any, Callable

from app.common.exceptions import BusinessError, exception_summary
from app.core.transactions import commit_transaction, rollback_transaction
from app.domain.materials.external_artifacts import ExternalServiceResult
from app.domain.shared.identity import public_no
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
from app.modules.audit.artifact.service.document_ports import document_service
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.system.operation.service.background import PHASE_RESULT_READY
from app.services.shared.storage import resolve_storage_path

PRE_CONTACT_ERROR_CODES = {
    "EXTERNAL_CONFIG_MISSING",
    "MATERIAL_TYPE_CATALOG_FAILED",
    "PROCESSED_ARTIFACT_MISSING",
    "DOCUMENT_SOURCE_MISSING",
    "MATERIAL_TYPE_MISSING",
    "MATERIAL_TYPE_UNSUPPORTED",
    "OCR_MULTIPART_INVALID",
}


class ArtifactExecution(AuditArtifactArtifactsService):
    """远端调用的阶段编排；调用前占位、进度与失败各自提交。"""

    def _assert_quality_precheck(self, task, upload) -> None:
        from app.modules.audit.execution.service.material_precheck import AuditMaterialPrecheckService
        AuditMaterialPrecheckService(self.db).assert_processing_allowed(task, upload)

    def _ensure_external_artifact(self, task: AuditTask,
        upload: DocumentUpload,
        service_code: str,
        call: Callable[[], ExternalServiceResult],
    ) -> StoredArtifact:
        """先占位再调用外部服务：唯一键 (上传, 产物类型) 让同一上传同一产物只调用一次。

        占位在调用前提交，因此并发与进程中断都不会重复产生外部作业：
        已 READY 直接复用；FAILED 或 PROCESSING 明确报错，由材料弹窗单项重试。
        占位与每次真实调用历史均保留，避免自动重发制造重复作业。
        """
        self._assert_quality_precheck(task, upload)
        existing = self._artifact_for_upload( upload, service_code)
        if existing is not None:
            if existing.status == "READY":
                return existing
            if existing.status == "FAILED":
                raise BusinessError("ARTIFACT_PROCESSING_FAILED", "此前该项处理失败，请在材料处理状态中重试此项", status_code=409,
                                    details={"artifact_no": existing.artifact_no, "artifact_type": service_code,
                                             "failure_message": existing.failure_message})
            raise BusinessError(
                "ARTIFACT_PROCESSING_UNKNOWN",
                f"此前对当前文件的 {service_code} 调用结果未能确认，系统不会自动重复提交外部服务，请在材料处理状态中确认后重试此项",
                status_code=409,
                details={"artifact_no": existing.artifact_no, "artifact_type": service_code},
            )
        record = StoredArtifact(
            artifact_no=public_no("ART"),
            audit_task_id=task.id,
            document_upload_id=upload.id,
            artifact_type=service_code,
            relative_path=f"tasks/{task.task_no}/artifacts/{upload.upload_no}/{service_code.lower()}.processing",
            status="PROCESSING",
            metadata_json={"submission_state": "PROCESSING"},
        )
        AuditArtifactCRUD(self.db).add(record)
        AuditArtifactCRUD(self.db).flush()
        commit_transaction(self.db)
        return self.execute_external_artifact_call( task, upload, record, call)

    def execute_external_artifact_call(self, task, upload, record, call, *, operation_no=None, resubmit_unknown=False):
        """联系服务前记录本次调用，保留失败原因与用户确认的重试意图。"""
        self._assert_quality_precheck(task, upload)
        metadata = record.metadata_json or {}
        attempts = list(metadata.get("processing_attempts") or [])
        attempt = {"operation_no": operation_no, "started_at": datetime.now(UTC).isoformat(),
                   "previous_status": record.status, "state": "PROCESSING", "resubmit_unknown": resubmit_unknown}
        attempts.append(attempt)
        record.status = "PROCESSING"
        record.failure_code = None
        record.failure_message = None
        record.metadata_json = {**metadata, "processing_attempts": attempts}
        commit_transaction(self.db)
        try:
            result = call()
            relative, artifact = self._persist_external_artifact(task, upload, record.artifact_type, result)
            content = artifact.model_dump(mode="json")
            record.relative_path = relative
            record.content_sha256 = self._artifact_content_digest(content, result.binary_content)
            record.status = "READY"
            record.failure_code = None
            record.failure_message = None
            attempts[-1] = {**attempt, "state": "READY", "finished_at": datetime.now(UTC).isoformat()}
            record.metadata_json = {**metadata, "processing_attempts": attempts}
            AuditArtifactCRUD(self.db).flush()
        except Exception as exc:
            rollback_transaction(self.db)
            # 调用前错误和明确拒绝属于已知失败；网络或落盘失败无法证明远端未处理。
            known_failure = isinstance(exc, BusinessError) and exc.code in PRE_CONTACT_ERROR_CODES | {"EXTERNAL_SERVICE_REJECTED"}
            record.status = "FAILED" if known_failure else "PROCESSING"
            attempts[-1] = {**attempt, "state": "FAILED" if known_failure else "UNKNOWN",
                            "finished_at": datetime.now(UTC).isoformat(),
                            "failure_code": exc.code if isinstance(exc, BusinessError) else type(exc).__name__,
                            "failure_message": exc.message if isinstance(exc, BusinessError) else exception_summary(exc)}
            record.failure_code = attempts[-1]["failure_code"]
            record.failure_message = attempts[-1]["failure_message"]
            record.metadata_json = {**metadata, "processing_attempts": attempts}
            commit_transaction(self.db)
            raise
        return record

    def _submit_ocr_job(self, task: AuditTask, upload: DocumentUpload, parsed_artifact: StoredArtifact) -> StoredArtifact:
        self._assert_quality_precheck(task, upload)
        existing = self._artifact_for_upload( upload, "OCR")
        if existing:
            if existing.status == "FAILED":
                raise BusinessError(
                    "OCR_JOB_FAILED",
                    "当前 OCR 作业已失败，请在材料处理状态中使用单份重试后再执行审核",
                    status_code=409,
                    details={"artifact_no": existing.artifact_no, "job_id": existing.external_job_id,
                             "failure_message": existing.failure_message},
                )
            if existing.status == "SUBMITTING":
                raise BusinessError(
                    "OCR_SUBMISSION_STATE_UNKNOWN",
                    "此前 OCR 提交结果未能确认，请在材料处理状态中确认后重新提交该材料的 OCR；系统不会自动重提",
                    status_code=409,
                )
            if existing.status == "PENDING":
                _, deadline_at = self._ocr_job_metadata(existing)
                if deadline_at.astimezone(UTC) <= datetime.now(UTC):
                    raise BusinessError(
                        "OCR_JOB_EXPIRED",
                        "此前 OCR 作业已超过总等待时间，请在材料处理状态中使用单份重试继续查询原作业",
                        status_code=409,
                    )
            if existing.status not in {"PENDING", "READY"}:
                raise BusinessError("OCR_ARTIFACT_STATUS_INVALID", "当前上传文件的 OCR 作业状态无效", status_code=500)
            return existing
        processed_path, processed_filename = self._processed_document_source(parsed_artifact)
        record = StoredArtifact(
            artifact_no=public_no("ART"),
            audit_task_id=task.id,
            document_upload_id=upload.id,
            artifact_type="OCR",
            relative_path=f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.submitting",
            status="SUBMITTING",
            metadata_json={"submission_state": "SUBMITTING"},
        )
        AuditArtifactCRUD(self.db).add(record)
        AuditArtifactCRUD(self.db).flush()
        # 先占位再联系供应商：提交中断是显式状态，绝不能自动重试——供应商可能已经受理。
        commit_transaction(self.db)
        try:
            submission = document_service().submit_processed_document(
                file_path=processed_path, filename=processed_filename,
            )
        except BusinessError as exc:
            if exc.code in {"EXTERNAL_CONFIG_MISSING", "PROCESSED_ARTIFACT_MISSING", "OCR_MULTIPART_INVALID"}:
                AuditArtifactCRUD(self.db).delete(record)
                commit_transaction(self.db)
            else:
                record.failure_code = exc.code
                record.failure_message = exc.message
                commit_transaction(self.db)
            raise
        record.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.pending"
        record.status = "PENDING"
        record.external_job_id = submission.job_id
        record.deadline_at = submission.deadline_at
        record.failure_code = None
        record.failure_message = None
        record.metadata_json = {"submission_state": "PENDING"}
        commit_transaction(self.db)
        return record

    def _submit_authorized_ocr_retry(self, task: AuditTask,
        upload: DocumentUpload,
        parsed_artifact: StoredArtifact,
        record: StoredArtifact,
        *,
        idempotency_key: str,
    ) -> StoredArtifact:
        """执行已持久化的单份重试提交意图；保留原作业与每次提交尝试。"""
        self._assert_quality_precheck(task, upload)
        metadata = record.metadata_json or {}
        attempts = list(metadata["submit_attempts"])
        attempt = dict(attempts[-1])
        if (
            record.status != "SUBMITTING"
            or attempt["idempotency_key"] != idempotency_key
            or attempt["state"] != "SUBMITTING"
        ):
            raise BusinessError("OCR_RETRY_INTENT_INVALID", "OCR 单份重试提交意图与当前作业状态不一致", status_code=409)

        try:
            processed_path, processed_filename = self._processed_document_source(parsed_artifact)
        except Exception as exc:
            attempt = {
                **attempt,
                "state": "PRE_CONTACT_FAILED",
                "failure_code": exc.code if isinstance(exc, BusinessError) else "OCR_RETRY_PREPARE_FAILED",
                "failure_message": exc.message if isinstance(exc, BusinessError) else exception_summary(exc),
            }
            record.status = attempt["previous_status"]
            record.relative_path = attempt["previous_relative_path"]
            # 未联系供应商就失败：产物恢复为原状态与原失败信息，本次失败只记在提交历史里。
            self._restore_job_columns(record, attempt)
            record.metadata_json = {
                **attempt["previous_metadata"],
                "submit_attempts": [*attempts[:-1], attempt],
            }
            AuditArtifactCRUD(self.db).flush()
            raise
        try:
            submission = document_service().submit_processed_document(
                file_path=processed_path, filename=processed_filename,
            )
        except BusinessError as exc:
            attempt = {**attempt, "failure_code": exc.code, "failure_message": exc.message}
            if exc.code in {"EXTERNAL_CONFIG_MISSING", "PROCESSED_ARTIFACT_MISSING", "OCR_MULTIPART_INVALID"}:
                attempt["state"] = "PRE_CONTACT_FAILED"
                record.status = attempt["previous_status"]
                record.relative_path = attempt["previous_relative_path"]
                self._restore_job_columns(record, attempt)
                record.metadata_json = {
                    **attempt["previous_metadata"],
                    "submit_attempts": [*attempts[:-1], attempt],
                }
            else:
                # 已联系供应商但结果未知：保留本次失败信息，状态位 SUBMITTING。
                attempt["state"] = "UNKNOWN"
                record.failure_code = exc.code
                record.failure_message = exc.message
                record.metadata_json = {**metadata, "submit_attempts": [*attempts[:-1], attempt]}
            AuditArtifactCRUD(self.db).flush()
            raise
        except Exception as exc:
            attempt = {
                **attempt,
                "state": "UNKNOWN",
                "failure_code": "OCR_RETRY_UNEXPECTED_ERROR",
                "failure_message": exception_summary(exc),
            }
            record.failure_code = attempt["failure_code"]
            record.failure_message = attempt["failure_message"]
            record.metadata_json = {**metadata, "submit_attempts": [*attempts[:-1], attempt]}
            AuditArtifactCRUD(self.db).flush()
            raise

        attempt = {
            **attempt,
            "state": "PENDING",
            "job_id": submission.job_id,
            "deadline_at": submission.deadline_at.isoformat(),
        }
        record.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.pending"
        record.status = "PENDING"
        record.external_job_id = submission.job_id
        record.deadline_at = submission.deadline_at
        record.failure_code = None
        record.failure_message = None
        record.metadata_json = {"submit_attempts": [*attempts[:-1], attempt]}
        # 供应商已返回作业号时先持久化；结果日志中断也不能丢失可续查的作业身份。
        commit_transaction(self.db)
        return record

    def _complete_ocr_job(self, task: AuditTask, upload: DocumentUpload, artifact: StoredArtifact) -> StoredArtifact:
        self._assert_quality_precheck(task, upload)
        if artifact.status == "READY":
            return artifact
        job_id, deadline_at = self._ocr_job_metadata(artifact)

        def save_progress(progress: dict[str, Any]) -> None:
            artifact.metadata_json = {
                **(artifact.metadata_json or {}), **progress,
                "progress_updated_at": datetime.now(UTC).isoformat(),
            }
            commit_transaction(self.db)

        try:
            result = document_service().poll_ocr_job(job_id=job_id, deadline_at=deadline_at, on_progress=save_progress)
        except BusinessError as exc:
            if exc.code == "OCR_JOB_FAILED":
                # 供应商明确的失败终态必须保存；网络错误或本地超时仍保留可续查作业。
                artifact.status = "FAILED"
                artifact.failure_code = exc.code
                artifact.failure_message = exc.message
                artifact.metadata_json = {
                    **(artifact.metadata_json or {}),
                    "provider_state": "failed",
                    "provider_code": exc.details.get("provider_code"),
                    "provider_error": exc.details.get("provider_error"),
                    "provider_trace_id": exc.details.get("provider_trace_id"),
                    "failed_at": datetime.now(UTC).isoformat(),
                }
                commit_transaction(self.db)
            raise
        relative, payload = self._persist_external_artifact(task, upload, "OCR", result)
        artifact.relative_path = relative
        artifact.status = "READY"
        artifact.failure_code = None
        artifact.failure_message = None
        current_metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        artifact.metadata_json = {
            "submit_attempts": list(current_metadata.get("submit_attempts") or []),
            "provider_state": current_metadata.get("provider_state"),
            "extract_progress": current_metadata.get("extract_progress"),
            "progress_updated_at": current_metadata.get("progress_updated_at"),
        }
        artifact.content_sha256 = self._artifact_content_digest(payload.model_dump(mode="json"), result.binary_content)
        AuditArtifactCRUD(self.db).flush()
        return artifact


    def classify_upload(self, command) -> dict[str, Any]:
        """分类结果独立提交后，才推进后台命令阶段。"""
        content = command.request
        current = AuditTaskCRUD(self.db).by_no(content["task_no"])
        record = MaterialDocumentCRUD(self.db).get_of(DocumentUpload, content["upload_id"])
        document = (
            MaterialDocumentCRUD(self.db).get_of(TaskDocument, record.task_document_id)
            if record is not None else None
        )
        if current is None or record is None or document is None or record.audit_task_id != current.id:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "上传分类命令对应的任务文件不存在", status_code=404)
        if not record.is_current or current.input_revision != content["input_revision"]:
            raise BusinessError("DOCUMENT_CLASSIFICATION_INPUT_STALE", "分类命令对应的文件版本已失效", status_code=409)
        artifact = self._ensure_external_artifact(
             current, record, "MATERIAL_CLASSIFY",
            lambda: document_service().validate_material_type(
                file_path=resolve_storage_path(record.relative_path), filename=record.original_filename,
                content_type=record.content_type, material_type=record.declared_material_type,
            ),
        )
        commit_transaction(self.db)
        command.phase(PHASE_RESULT_READY)
        return {"task_no": current.task_no, "document_no": document.document_no, "artifact_no": artifact.artifact_no}
