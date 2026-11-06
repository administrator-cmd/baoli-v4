"""材料前置流水线：8018 → (8010 → OCR) ∥ 8003。

8018、8010、8003 各自顺序处理材料，三条服务队列同时推进；PaddleOCR 使用单条队列，
逐份执行提交与续查。每个工作步骤使用独立数据库会话，执行进度只由调度线程写入。
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
from queue import Queue
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.common.exceptions import BusinessError, exception_summary
from app.core.transactions import commit_transaction
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.artifact.execution import ArtifactExecution
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
from app.modules.audit.artifact.service.document_ports import document_service
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.shared import _current_uploads
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.material.document.model import DocumentUpload
from app.services.shared.storage import resolve_storage_path

logger = logging.getLogger(__name__)

STAGES = ("MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL", "OCR")
DEPENDENTS = {
    "SOURCE": STAGES,
    "MATERIAL_CLASSIFY": ("DOCUMENT_PARSE", "SEAL", "OCR"),
    "DOCUMENT_PARSE": ("OCR",),
    "SEAL": (),
    "OCR": (),
}


class ArtifactPreprocessingExecution:
    """调度独立材料处理工作单元并保存进度。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _prepare_stage(self, sessions: sessionmaker,
        task_id: int,
        upload_id: int,
        stage: str,
        parsed_id: int | None,
    ) -> tuple[int, str]:
        """线程只接收标量身份；会话和 ORM 对象均在当前线程创建、关闭。"""
        with sessions() as db:
            task = AuditTaskCRUD(db).get_of(AuditTask, task_id)
            upload = MaterialDocumentCRUD(db).get_of(DocumentUpload, upload_id)
            if task is None or upload is None or upload.audit_task_id != task.id:
                raise BusinessError("TASK_DOCUMENT_UPLOAD_MISSING", "前置处理的任务或上传记录不存在", status_code=500)
            if stage == "OCR":
                parsed = AuditArtifactCRUD(db).get_of(StoredArtifact, parsed_id)
                if parsed is None or parsed.document_upload_id != upload.id or parsed.artifact_type != "DOCUMENT_PARSE":
                    raise BusinessError("PROCESSED_ARTIFACT_MISSING", "OCR 缺少当前上传的去章产物", status_code=500)
                artifact = ArtifactExecution(db)._complete_ocr_job( task, upload, ArtifactExecution(db)._submit_ocr_job( task, upload, parsed))
            else:
                port = document_service()
                kwargs = {
                    "file_path": resolve_storage_path(upload.relative_path),
                    "filename": upload.original_filename, "content_type": upload.content_type,
                }
                if stage == "MATERIAL_CLASSIFY":
                    call = partial(port.validate_material_type, **kwargs, material_type=upload.declared_material_type)
                elif stage == "DOCUMENT_PARSE":
                    call = partial(port.process_document, **kwargs)
                elif stage == "SEAL":
                    call = partial(port.detect_seals, **kwargs)
                else:
                    raise ValueError(f"未知材料处理步骤：{stage}")
                artifact = ArtifactExecution(db)._ensure_external_artifact( task, upload, stage, call)
            AuditArtifactArtifactsService()._artifact_payload(artifact)
            commit_transaction(db)
            return artifact.id, artifact.artifact_no


    def prepare_task_materials(self, task: AuditTask, execution: NodeExecution, *,
                               blocked_materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """跨材料流水线；全部分支收尾且四项成功后，才进入业务规则。

        某步骤失败仅停止该材料的依赖步骤，独立分支和其他材料继续落真实结果。
        所有已调度步骤收尾后汇总失败、阻断审核，不取消已经受理的外部作业，也不自动重提。
        """
        uploads = _current_uploads(self.db, task)
        documents = {}
        progress: dict[str, Any] = {"status": "RUNNING", "materials": []}
        failures: list[tuple[dict[str, Any], str, Exception]] = []
        runnable = []
        task_id, task_no = task.id, task.task_no
        sessions = sessionmaker(bind=self.db.get_bind(), autoflush=False, expire_on_commit=False)

        def save_progress() -> None:
            execution.result_json = {**(execution.result_json or {}), "preprocessing": deepcopy(progress)}
            commit_transaction(self.db)

        def fail(item: dict[str, Any], stage: str, exc: Exception) -> None:
            detail = {
                "material_key": item["material_key"], "material_name": item["material_name"],
                "service": stage, "reason": exception_summary(exc),
            }
            if isinstance(exc, BusinessError):
                detail["code"] = exc.code
                detail["details"] = exc.details
            item["stages"][stage] = {"status": "FAILED", "error": detail}
            item.update(status="FAILED", stage=stage)
            item.setdefault("errors", []).append(detail)
            for dependent in DEPENDENTS[stage]:
                item["stages"][dependent] = {"status": "SKIPPED", "reason": f"前置步骤 {stage} 失败"}
            failures.append((item, stage, exc))
            logger.error("材料前置处理失败 task_no=%s material=%s service=%s reason=%s",
                         task_no, item["material_name"], stage, detail["reason"])

        for document, upload in uploads:
            if upload is None:
                # 可选材料未上传时确实没有要处理的文件；必填材料没有当前上传不能跳过，
                # 否则整轮审核会在缺输入的情况下继续，页面却看不到任何缺项。
                if document.required:
                    missing = {
                        "material_key": document.material_key, "material_name": document.material_name,
                        "upload_id": None, "filename": None,
                        "status": "WAITING", "stage": "SOURCE", "artifacts": {},
                        "stages": {stage: {"status": "WAITING"} for stage in STAGES},
                    }
                    progress["materials"].append(missing)
                    fail(missing, "SOURCE", BusinessError(
                        "TASK_DOCUMENT_UPLOAD_MISSING", "必填材料没有当前上传文件，无法进行前置处理",
                        status_code=500,
                    ))
                continue
            item = {
                "material_key": document.material_key, "material_name": document.material_name,
                "upload_id": upload.id, "filename": upload.original_filename,
                "status": "WAITING", "stage": "SOURCE", "artifacts": {},
                "stages": {stage: {"status": "WAITING"} for stage in STAGES},
            }
            progress["materials"].append(item)
            block = blocked_materials.get(document.material_key)
            if block is not None:
                reason = "；".join(entry["message"] for entry in block["reasons"])
                item.update(status="SKIPPED", stage="QUALITY_CHECK", reason=reason)
                item["stages"] = {stage: {"status": "SKIPPED", "reason": reason} for stage in STAGES}
                continue
            documents[upload.id] = document
            if not resolve_storage_path(upload.relative_path).is_file():
                fail(item, "SOURCE", BusinessError("DOCUMENT_SOURCE_MISSING", "当前上传文件不存在，无法进行前置处理", status_code=500))
            else:
                runnable.append(item)
        save_progress()  # 释放原任务锁后才让工作会话读任务。

        if runnable:
            events: Queue = Queue()
            pending: dict[Future, tuple[dict[str, Any], str]] = {}
            with ExitStack() as stack:
                # 每个本地服务各一条处理队列，符合逐份进站、站间并行的流水线。
                lanes = {
                    stage: stack.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"material-{stage.lower()}"))
                    for stage in STAGES if stage != "OCR"
                }
                # 本次任务的 OCR 步骤逐份执行；8010 仍可准备后续材料。
                # 这不是账号级全局锁；本地超时也不代表远端作业已终止。
                lanes["OCR"] = stack.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix="material-ocr"))

                def schedule(item: dict[str, Any], stage: str, parsed_id: int | None = None) -> None:
                    item["stages"][stage] = {"status": "QUEUED"}
                    upload_id = item["upload_id"]
                    material_name = item["material_name"]

                    def run() -> tuple[int, str]:
                        events.put(("STARTED", upload_id, stage))
                        logger.info("材料前置处理 task_no=%s material=%s service=%s", task_no, material_name, stage)
                        return self._prepare_stage(sessions, task_id, upload_id, stage, parsed_id)

                    future = lanes[stage].submit(run)
                    pending[future] = (item, stage)
                    future.add_done_callback(lambda done: events.put(("FINISHED", done, stage)))

                by_upload = {item["upload_id"]: item for item in runnable}
                for item in runnable:
                    schedule(item, "MATERIAL_CLASSIFY")
                save_progress()
                while pending:
                    event, identity, stage = events.get()
                    if event == "STARTED":
                        item = by_upload[identity]
                        item["stages"][stage] = {"status": "RUNNING"}
                        if item["status"] != "FAILED":
                            item.update(status="RUNNING", stage=stage)
                        save_progress()
                        continue
                    future = identity
                    item, stage = pending.pop(future)
                    try:
                        artifact_id, artifact_no = future.result()
                    except Exception as exc:
                        # 失败显式落进每个分支，汇总后原样上抛；不生成任何成功占位。
                        fail(item, stage, exc)
                    else:
                        item["stages"][stage] = {"status": "READY", "artifact_no": artifact_no}
                        item["artifacts"][stage] = artifact_no
                        if stage == "MATERIAL_CLASSIFY":
                            schedule(item, "DOCUMENT_PARSE")
                            schedule(item, "SEAL")
                        elif stage == "DOCUMENT_PARSE":
                            schedule(item, "OCR", parsed_id=artifact_id)
                        if all(item["stages"][step]["status"] == "READY" for step in STAGES):
                            item.update(status="READY", stage="READY")
                            documents[item["upload_id"]].status = "PREPARED"
                    save_progress()

        progress["status"] = "FAILED" if failures else "READY"
        save_progress()
        if failures:
            item, stage, first = failures[0]
            if not isinstance(first, BusinessError):
                raise first
            messages = [
                f"材料「{failed_item['material_name']}」前置处理 {failed_stage} 失败："
                + (exc.message if isinstance(exc, BusinessError) else exception_summary(exc))
                for failed_item, failed_stage, exc in failures
            ]
            raise BusinessError(
                first.code, "；".join(messages), status_code=first.status_code,
                details={**first.details, "material_key": item["material_key"], "material_name": item["material_name"],
                         "service": stage, "failures": [detail for failed in progress["materials"] for detail in failed.get("errors", [])]},
            ) from first
        return progress
