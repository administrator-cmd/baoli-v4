"""任务材料上传、移除与输入变更失效处理。"""

import logging
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import TASK_STATUS_READY
from app.core.config import get_settings
from app.core.transactions import after_rollback
from app.domain.identity import public_no
from app.domain.rule_library import execution_container_of
from app.integrations.documents.material_classify import list_material_types
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.service.execution_history import AuditExecutionHistoryService
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.model import ManualReview
from app.modules.audit.shared import current_upload
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK
from app.services.shared.storage import DOCUMENT_EXTENSIONS, resolve_storage_path, save_upload_sync

logger = logging.getLogger(__name__)


class AuditTaskDocumentsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _verified_material_type(self, declared: str) -> str:
        """核对上传时声明的材料类型，返回可落库的取值。

        8018 的 doc_type 是严格枚举，目录是外部服务的事实：本函数不缓存、不内置默认清单、
        不按材料名称猜类型。空值与目录外的取值都在这里显式拒绝——问题要在上传时就暴露，
        不能等到材料检查节点执行时才由 8018 拒绝，那时用户已经以为文件传好了。
        """
        material_type = (declared or "").strip()
        if not material_type:
            raise BusinessError(
                "MATERIAL_TYPE_MISSING",
                "未选择材料类型，无法上传：请从 8018 支持的材料类型目录中选择",
                status_code=409,
            )
        supported = {str(item["name"]) for item in list_material_types()}
        if material_type not in supported:
            raise BusinessError(
                "MATERIAL_TYPE_UNSUPPORTED",
                "选择的材料类型不在 8018 支持目录中，无法上传",
                status_code=409,
                details={"material_type": material_type, "supported": sorted(supported)},
            )
        return material_type


    def _invalidate_for_input_change(self, task: AuditTask,
        document: TaskDocument,
        previous_upload_id: int | None,
        *,
        action_label: str,
    ) -> dict[str, Any]:
        """任务输入变化（替换 / 首次补传 / 移除当前文件）的统一失效流程。

        受影响的处理产物、字段事实、后续结果与报告一起失效；旧版本事实保留可追溯。
        失效通过“节点执行批次 + 当前上传版本 + 统一事实当前标记”表达，旧版本事实本身保留，
        因此不覆盖原始字段值和原始规则结论，历史仍能查到当时抽到了什么、判定了什么。
        调用方负责维护 is_current 与材料位状态，并在完成后重新建立审核等待点。
        """
        manifest = task.manifest_json
        if not isinstance(manifest, dict) or not manifest:
            raise BusinessError("TASK_MANIFEST_MISSING", "任务缺少已冻结的执行配置", status_code=500)
        affected_artifacts = (
            AuditArtifactCRUD(self.db).count_active_by_upload(previous_upload_id)
            if previous_upload_id else 0
        )
        superseded_field_values = AuditFieldCRUD(self.db).count_current_field_values(task.id)
        affected_rule_results = AuditExecutionCRUD(self.db).count_rule_results_for_task(task.id)
        affected_executions = AuditExecutionCRUD(self.db).count_for_task(task.id, exclude_invalidated=True)
        invalidated_task_facts = AuditFieldCRUD(self.db).count_current_facts(task.id)
        if previous_upload_id:
            AuditArtifactCRUD(self.db).invalidate_by_upload(previous_upload_id)
        invalidated_reviews = AuditFieldCRUD(self.db).invalidate_pending_reviews(task.id, ManualReview)

        AuditExecutionHistoryService(self.db).invalidate_for_task(task.id, "任务材料输入已变更")
        # 当前实现会让全部节点和全部统一事实失效并从 DAG 起点重跑，因此候选层也必须
        # 按同一范围整体退出当前位。只撤销被替换材料会留下“候选仍当前、TaskFact 已失效”
        # 的半套事实，任务详情与规则输入配对时会明确报错。
        AuditFieldCRUD(self.db).clear_current_field_values(task.id)
        # 所有节点执行都已失效，抽取与派生事实也不能继续作为后续节点的当前输入。
        AuditFieldCRUD(self.db).clear_current_facts(task.id)
        first_node_code = execution_container_of(manifest)["node_code"]
        task.current_node = first_node_code
        task.status = TASK_STATUS_READY
        # 当前有效事实与结果已失效：必须递增输入修订号，使携带旧修订的命令被明确拒绝。
        task.input_revision = (task.input_revision or 0) + 1
        logger.info(
            "材料位%s使影响范围内的结果失效 task_no=%s material_name=%s 受影响产物=%s 旧版本字段事实=%s "
            "失效统一事实=%s 失效规则结果=%s 失效节点执行=%s 失效待复核项=%s 新输入修订=%s",
            action_label,
            task.task_no,
            document.material_name,
            affected_artifacts,
            superseded_field_values,
            invalidated_task_facts,
            affected_rule_results,
            affected_executions,
            invalidated_reviews,
            task.input_revision,
        )
        return {
            "affected_artifacts": int(affected_artifacts),
            "stale_field_values": int(superseded_field_values),
            "invalidated_rule_results": int(affected_rule_results),
            "invalidated_node_executions": int(affected_executions),
            "invalidated_reviews": int(invalidated_reviews or 0),
            "reset_to_node": first_node_code,
            "description": (
                f"{action_label}「{document.material_name}」的当前文件：{affected_artifacts} 个处理产物、"
                f"{superseded_field_values} 条旧版本字段事实不再进入当前结果，"
                f"{invalidated_task_facts} 条统一事实已失效，"
                f"{affected_rule_results} 条规则结果与 {affected_executions} 个节点执行已失效，"
                "任务回到起始节点等待确认。历史事实仍可追溯。"
            ),
        }


    def upload_document(self, task_no: str,
        document_no: str,
        upload: UploadFile,
        *,
        material_type: str,
        replace: bool = False,
    ) -> tuple[TaskDocument, DocumentUpload, dict[str, Any] | None, BackgroundOperation]:
        """上传或替换材料文件：落库、失效下游结果，并返回本次输入修订的真实影响范围。

        `material_type` 是用户本次上传时从 8018 支持目录中选择的材料类型。它随本次上传
        保存（见 DocumentUpload.declared_material_type），不来自场景配置：材料位本身不声明
        类型，替换文件时可以改选。空值或不在 8018 目录内都显式拒绝，不代填、不跳过校验。
        """
        settings = get_settings()
        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        # 与后台执行互斥必须在锁定对象行之后：先取锁再查活跃操作，消除检查与修改之间的 TOCTOU。
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_AUDIT_TASK, task.task_no)
        document = MaterialDocumentCRUD(self.db).by_task_and_no(task.id, document_no)
        if not document:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
        if task.status in {"COMPLETED", "FAILED"}:
            raise BusinessError("TASK_DOCUMENT_LOCKED", "任务已结束，不能替换材料")
        if task.status == "RUNNING":
            raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能替换材料", status_code=409)
        previous_upload = current_upload(self.db, document)
        previous_upload_id = previous_upload.id if previous_upload else None
        if previous_upload_id and not replace:
            raise BusinessError(
                "TASK_DOCUMENT_REPLACE_CONFIRMATION_REQUIRED",
                "该材料位已有当前文件，请明确确认替换后再上传",
                status_code=409,
            )
        execution_count = AuditExecutionCRUD(self.db).count_for_task(task.id)
        # 输入变化不止“替换”：首次补传此前缺失的当前文件，同样改变任务已经依据的结论。
        # 只要任务已越过起始节点（移动过节点或执行过任何节点），补传就必须与替换走同一套失效流程。
        task_manifest = task.manifest_json
        first_node_code = execution_container_of(task_manifest)["node_code"]
        input_changed = (
            bool(previous_upload_id)
            or task.current_node != first_node_code
            or execution_count > 0
        )
        # 类型核对放在所有任务侧校验之后、写文件之前：请求本身不合法时不必打扰 8018，
        # 而类型不合法时必须在落盘前失败，不留下一份没有类型的孤儿文件。
        declared_material_type = self._verified_material_type(material_type)
        upload_no = public_no("UPL")
        suffix = Path(upload.filename or "").suffix.lower()
        relative_path = f"tasks/{task.task_no}/{document.document_no}/{upload_no}{suffix}"
        size, sha256 = save_upload_sync(
            upload,
            relative_path=relative_path,
            allowed_extensions=DOCUMENT_EXTENSIONS,
            max_bytes=settings.max_document_bytes,
        )
        task_id = task.id
        bind = self.db.get_bind()

        def discard_unreferenced_upload() -> None:
            with Session(bind=bind) as verification:
                AuditTaskCRUD(verification).lock_row_id(task_id)
                if MaterialDocumentCRUD(verification).upload_by_no(upload_no) is None:
                    resolve_storage_path(relative_path).unlink(missing_ok=True)

        after_rollback(self.db, discard_unreferenced_upload)
        MaterialDocumentCRUD(self.db).invalidate_current(document.id)
        record = DocumentUpload(
            upload_no=upload_no,
            task_document_id=document.id,
            audit_task_id=task.id,
            original_filename=upload.filename or f"材料{suffix}",
            relative_path=relative_path,
            content_type=upload.content_type or "application/octet-stream",
            declared_material_type=declared_material_type,
            size_bytes=size,
            sha256=sha256,
            status="UPLOADED",
            is_current=True,
        )
        MaterialDocumentCRUD(self.db).add(record)
        # 首次上传且任务尚未越过起始节点时没有失效范围；其余情况给出真实影响范围供页面展示。
        impact: dict[str, Any] | None = None
        if input_changed:
            # 补传/替换文件是明确的新版本输入：受影响的处理产物、字段事实、后续结果与报告一起失效。
            impact = self._invalidate_for_input_change(

                task,
                document,
                previous_upload_id,
                action_label="已替换" if previous_upload_id else "已补传此前缺失的",
            )
        document.status = "UPLOADED"
        # 文件保存、输入失效和分类命令一起提交；活跃命令占位消除提交后的替换/执行竞态。
        from app.modules.audit.execution.service.operations import AuditExecutionOperationsService
        operation, _ = AuditExecutionOperationsService(self.db).submit_document_classification(task, record)
        return document, record, impact, operation


    def remove_current_document_file(self, task_no: str,
        document_no: str,
    ) -> tuple[TaskDocument, dict[str, Any]]:
        """移除材料位的当前文件（输入修订：旧文件保留历史，依赖结果失效）。

        - 与后台执行互斥；任务已结束或正在执行时拒绝。
        - 没有当前文件时明确报错，不产生任何副作用。
        - 历史上传版本保留为历史，不删除文件；任务回到规则执行等待重新上传。
        """
        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_AUDIT_TASK, task.task_no)
        document = MaterialDocumentCRUD(self.db).by_task_and_no(task.id, document_no)
        if not document:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
        previous_upload = current_upload(self.db, document)
        if previous_upload is None:
            raise BusinessError("TASK_DOCUMENT_NO_CURRENT_FILE", "该材料位没有当前文件，无法移除", status_code=409)
        if task.status in {"COMPLETED", "FAILED"}:
            raise BusinessError("TASK_DOCUMENT_LOCKED", "任务已结束，不能移除材料文件", status_code=409)
        if task.status == "RUNNING":
            raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能移除材料文件", status_code=409)
        previous_upload_id = previous_upload.id
        MaterialDocumentCRUD(self.db).invalidate_upload(previous_upload_id)
        document.status = "WAITING_UPLOAD"
        impact = self._invalidate_for_input_change(

            task,
            document,
            previous_upload_id,
            action_label="已移除",
        )
        AuditTaskCRUD(self.db).flush()
        AuditTaskCRUD(self.db).refresh(document)
        return document, impact
