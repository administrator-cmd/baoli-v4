"""任务材料外部处理产物与 OCR 状态视图。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.shared import current_upload
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.material.document.crud import MaterialDocumentCRUD

MATERIAL_ARTIFACT_TYPES = ("MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL")


def quality_block_message(block: dict[str, Any]) -> str:
    prefix = "质量不合格，已停止处理" if block["status"] == "ABNORMAL" else "质量暂时无法判定，已停止处理"
    return prefix + "：" + "；".join(item["message"] for item in block["reasons"])



class AuditTaskProcessingService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _quality_block(self, task, upload) -> dict[str, Any] | None:
        from app.modules.audit.execution.service.material_precheck import AuditMaterialPrecheckService
        state = AuditMaterialPrecheckService(self.db).current_state(task)
        return next((item for item in (state or {}).get("blocked_materials", {}).values()
                     if item["upload_id"] == upload.id), None)

    def material_quality_views(self, task, *, pairs, execution, plan, rule_results, rule_values
    ) -> dict[str, dict[str, Any]]:
        """投影当前文件的前置检查；复用详情已读的 CSV，不另存业务结论。"""
        from app.domain.materials.material_precheck import PRECHECK_KEY

        state = (execution.result_json or {}).get(PRECHECK_KEY) if execution is not None else None
        if state is not None and state["input_revision"] != task.input_revision:
            state = None
        selected = set(state["rule_keys"]) if state is not None else set()
        entries = [entry for entry in plan if entry["rule_key"] in selected]
        records = {record.rule_key: record for record in rule_results
                   if execution is not None and record.node_execution_id == execution.id}
        verdicts = {}
        for key, record in records.items():
            if key not in selected or not (record.result_json or {}).get(PRECHECK_KEY):
                continue
            verdicts[key] = {item["material_key"]: item
                             for item in rule_values[record.id].get("material_quality", [])}

        views = {}
        for document, upload in pairs:
            checks = []
            block = (state or {}).get("blocked_materials", {}).get(document.material_key)
            if block is not None and (upload is None or block["upload_id"] != upload.id):
                block = None
            reasons = block["reasons"] if block is not None else []
            for entry in entries:
                scope = entry["rule"].get("materials") or []
                if scope and document.material_key not in scope:
                    continue
                key = entry["rule_key"]
                verdict = verdicts.get(key, {}).get(document.material_key) if upload is not None else None
                reason = next((item for item in reasons if item["rule_key"] == key), None)
                checks.append({
                    "rule_key": key,
                    "rule_name": entry["rule"]["name"],
                    "requirement": entry["rule"].get("requirement"),
                    "status": verdict["status"] if verdict is not None else (
                        "EVIDENCE_INSUFFICIENT" if reason is not None else "NOT_CHECKED"),
                    "message": verdict["message"] if verdict is not None else (
                        reason["message"] if reason is not None else "尚未形成该文件的检查结论"),
                })

            completed = sum(item["status"] != "NOT_CHECKED" for item in checks)
            ready = state is not None and state["status"] == "READY"
            if upload is None:
                status, message = "NO_FILE", "上传文件后，开始审核时进行前置质量检查。"
            elif state is None:
                status, message = "NOT_CHECKED", "当前文件尚无前置质量检查记录；开始或继续审核后检查。"
            elif not ready:
                running = execution.status == "RUNNING"
                status = "RUNNING" if running else "NOT_CHECKED"
                message = (f"已取得 {completed} / {len(checks)} 项检查结论，完成后再处理材料。"
                           if running and checks else "正在完成本批次的前置检查，完成后再处理材料。"
                           if running else "前置检查尚未完成，请先按执行记录中的原因恢复审核。")
            elif block is not None:
                status, message = block["status"], quality_block_message(block)
            elif not checks:
                status, message = "NOT_CONFIGURED", "当前材料没有需在处理前执行的检查；如有正文、印章等检查，将在后续执行。"
            elif completed != len(checks):
                status, message = "NOT_CHECKED", "当前文件的前置检查记录不完整，不能显示为通过。"
            elif any(item["status"] in {"ABNORMAL", "EVIDENCE_INSUFFICIENT"} for item in checks):
                status, message = "EVIDENCE_INSUFFICIENT", "检查结果与放行记录不一致，请查看检查明细和执行记录。"
            elif all(item["status"] == "NOT_APPLICABLE" for item in checks):
                status, message = "NOT_APPLICABLE", "已检查，当前文件不适用这些前置要求。"
            else:
                status, message = "PASSED", "适用的前置检查已通过；后续仍按规则检查材料内容。"
            views[document.material_key] = {
                "status": status, "message": message, "checks": checks,
                "completed_checks": completed, "total_checks": len(checks),
                "blocked": block is not None,
                "processing_allowed": upload is not None and ready and block is None,
                "reasons": reasons,
            }
        return views

    def material_artifacts_summary(self, upload_ids: list[int]
    ) -> dict[int, dict[str, dict[str, Any]]]:
        """按当前上传批量汇总四项处理产物状态，供任务详情一次返回。

        只读数据库，不触发外部请求；同一上传同一类型取最新一条记录。
        """
        if not upload_ids:
            return {}
        rows = AuditArtifactCRUD(self.db).rows_by_uploads(list(upload_ids), MATERIAL_ARTIFACT_TYPES)
        latest: dict[tuple[int, str], Any] = {}
        for row in rows:
            latest[(row.document_upload_id, row.artifact_type)] = row
        summary: dict[int, dict[str, dict[str, Any]]] = {}
        for (upload_id, artifact_type), row in latest.items():
            metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            entry: dict[str, Any] = {
                "status": row.status,
                "artifact_no": row.artifact_no,
                "message": row.failure_message or self._artifact_status_message(row.status),
                # 统一用 ISO 文本，前端按字符串展示，不依赖响应模型的日期序列化。
                "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
            }
            if artifact_type == "OCR":
                entry["job_id"] = row.external_job_id
                # 供应商侧状态用于区分排队与识别中；只返回已保存的真实值。
                entry["provider_state"] = metadata.get("provider_state")
                entry["extract_progress"] = metadata.get("extract_progress")
                entry["progress_updated_at"] = metadata.get("progress_updated_at")
            summary.setdefault(upload_id, {})[artifact_type] = entry
        return summary


    def _artifact_status_message(self, status: str) -> str:
        return {
            "READY": "已就绪",
            "PENDING": "作业已提交，等待供应商完成",
            "SUBMITTING": "尚未取得提交结果",
            "PROCESSING": "尚未取得处理结果",
            "FAILED": "处理失败，可在处理状态中重试此项",
            "INVALIDATED": "已随材料替换失效",
        }.get(status, f"处理产物状态：{status}")


    def external_artifact_status_view(self, task_no: str, document_no: str, artifact_type: str
    ) -> dict[str, Any]:
        """查询当前上传版本的 8018/8010/8003 产物状态，不触发外部请求。"""
        if artifact_type not in {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL"}:
            raise BusinessError("ARTIFACT_TYPE_INVALID", "仅支持查询材料校验、去章或印章识别产物", status_code=400)
        task = AuditTaskLookupService(self.db).task_or_404( task_no)
        document = MaterialDocumentCRUD(self.db).by_task_and_no(task.id, document_no)
        if document is None:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
        base = {
            "document_no": document_no,
            "artifact_type": artifact_type,
            "artifact_no": None,
        }
        upload = current_upload(self.db, document)
        if upload is None:
            return {**base, "status": "NOT_SUBMITTED", "message": "该材料位尚未上传文件"}
        block = self._quality_block(task, upload)
        if block is not None:
            return {**base, "status": "SKIPPED", "message": quality_block_message(block)}
        artifact = AuditArtifactCRUD(self.db).by_upload_and_type(upload.id, artifact_type)
        if artifact is None:
            return {**base, "status": "NOT_SUBMITTED", "message": "当前上传文件尚无该处理产物"}
        return {
            **base,
            "artifact_no": artifact.artifact_no,
            "status": artifact.status,
            "message": artifact.failure_message or self._artifact_status_message(artifact.status),
        }


    def ocr_status_view(self, task_no: str, document_no: str) -> dict[str, Any]:
        """当前上传文件对应的 OCR 产物只读状态。

        只返回真实记录的产物状态；没有产物时明确说明“尚未提交”，不编造作业号或截止时间。
        job_id / deadline_at 直接取自产物元数据，submit_attempts 缺失时保持 None。
        """
        task = AuditTaskLookupService(self.db).task_or_404( task_no)
        document = MaterialDocumentCRUD(self.db).by_task_and_no(task.id, document_no)
        if not document:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
        base = {
            "document_no": document.document_no,
            "artifact_no": None,
            "job_id": None,
            "deadline_at": None,
            "submit_attempts": None,
        }
        upload = current_upload(self.db, document)
        if upload is None:
            return {**base, "status": "NOT_SUBMITTED", "message": "该材料位尚未上传文件，没有 OCR 产物"}
        block = self._quality_block(task, upload)
        if block is not None:
            return {**base, "status": "SKIPPED", "message": quality_block_message(block)}
        # OCR 正文较大，先取最新记录 ID，再按主键读取，避免 MySQL 对整行 JSON 排序。
        artifact_id = AuditArtifactCRUD(self.db).latest_id_by_upload_type(upload.id, "OCR")
        artifact = AuditArtifactCRUD(self.db).by_id(artifact_id) if artifact_id is not None else None
        if artifact is None:
            return {**base, "status": "NOT_SUBMITTED", "message": "当前上传文件尚未提交 OCR 作业"}
        metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        job_id = artifact.external_job_id
        deadline_at = artifact.deadline_at
        attempts = metadata.get("submit_attempts")
        message = {
            "SUBMITTING": artifact.failure_message or "尚未取得 OCR 提交结果",
            "PENDING": "OCR 作业已提交，等待供应商完成",
            "READY": "OCR 产物已就绪",
            "FAILED": str(artifact.failure_message or "OCR 作业失败"),
            "INVALIDATED": "OCR 产物已随材料替换失效",
        }.get(artifact.status, f"OCR 产物状态：{artifact.status}")
        return {
            "document_no": document.document_no,
            "artifact_no": artifact.artifact_no,
            "status": artifact.status,
            "job_id": job_id if isinstance(job_id, str) else None,
            "deadline_at": deadline_at,
            "submit_attempts": len(attempts) if isinstance(attempts, list) else attempts if isinstance(attempts, int) else None,
            "provider_state": metadata.get("provider_state"),
            "extract_progress": metadata.get("extract_progress"),
            "progress_updated_at": metadata.get("progress_updated_at"),
            "message": message,
        }
