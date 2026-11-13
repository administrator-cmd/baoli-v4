"""审核命令编排：后台执行按阶段提交，HTTP 字段编辑由请求层提交。"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError, exception_summary
from app.constants.status import (
    AUDIT_LLM_CHANNEL_UNAVAILABLE,
    FIELD_STATUS_NOT_PRESENT,
    FIELD_STATUS_REVIEWED,
    INTERRUPTED_EXECUTION_FAILURE_CODE,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_READY,
    TASK_STATUS_WAITING_FIELD_REVIEW,
)
from app.core.config import get_settings
from app.core.transactions import commit_transaction
from app.domain.field_review import reviewed_extraction_keys
from app.domain.identity import public_no
from app.graphs.audit_graph import run_audit_node
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.artifact.execution import ArtifactExecution
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.dto import ExecuteNodeRequest
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.execution.service.flow.errors import fail_running_execution, raise_graph_error
from app.modules.audit.execution.service.flow.runner import audit_runner
from app.modules.audit.execution.service.material_precheck import AuditMaterialPrecheckService
from app.modules.audit.execution.service.state import _scenario_nodes
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.dto import FieldCorrectionRequest, FieldReviewCorrection, FieldReviewSaveRequest
from app.modules.audit.field.model import ManualReview
from app.modules.audit.field.service import field_review as field_review_service
from app.modules.audit.field.service import value_store as task_value_service
from app.modules.audit.field.service.facts import AuditFieldFactsService
from app.modules.audit.field.service.reviews import AuditFieldReviewsService
from app.modules.audit.shared import _normalize_field_value, _task_field_definitions, _task_manifest, current_upload
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK

logger = logging.getLogger(__name__)


class AuditExecutionCommandsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def recover_interrupted_executions(self) -> dict[str, int]:
        """服务启动时清理上次运行中断留下的 RUNNING 记录。

        只做「显式标记中断 + 保留重试入口」：外部服务调用可能已被供应商接受，
        自动重放会制造重复作业，因此既不自动重发，也不把中断伪装成恢复成功。
        仅适用于单进程部署（当前设计即单 FastAPI 进程）。
        """
        interrupted = AuditExecutionCRUD(self.db).interrupted_running()
        for execution in interrupted:
            execution.status = "FAILED"
            execution.finished_at = datetime.now(UTC)
            execution.failure_code = INTERRUPTED_EXECUTION_FAILURE_CODE
            execution.failure_message = "服务重启导致该节点执行中断，已有结果已保留，请重新执行当前节点"
        # 只处理真正停在执行中的任务。等待节点确认或人工复核的任务必须保持等待，
        # 不能一律置 FAILED；它们的图 checkpoint 仍可用于恢复。
        running_tasks = AuditTaskCRUD(self.db).running()
        for task in running_tasks:
            task.status = TASK_STATUS_BLOCKED
        interrupted_ocr_retries = 0
        for artifact in AuditArtifactCRUD(self.db).submitting_ocr():
            metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
            attempts = list(metadata.get("submit_attempts") or [])
            if not attempts or attempts[-1].get("state") != "SUBMITTING":
                continue
            attempts[-1] = {**attempts[-1], "state": "UNKNOWN", "failure_code": "OCR_RETRY_INTERRUPTED"}
            artifact.metadata_json = {**metadata, "submit_attempts": attempts}
            artifact.failure_code = "OCR_RETRY_INTERRUPTED"
            artifact.failure_message = "服务重启导致 OCR 重新提交结果未知，请确认后重试"
            interrupted_ocr_retries += 1
        AuditExecutionCRUD(self.db).flush()
        return {
            "interrupted_executions": len(interrupted),
            "blocked_tasks": len(running_tasks),
            "interrupted_ocr_retries": interrupted_ocr_retries,
        }


    def execute_current_node(self, task_no: str, request: ExecuteNodeRequest) -> dict[str, Any]:
        """节点执行与手动重试入口：先做并发与状态校验，再跑完当前节点的全部规则。

        执行不再有等待点，也没有「恢复」这一段：一次后台操作就是一次完整执行。
        """
        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        if task.status == "COMPLETED":
            raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
        if task.status == TASK_STATUS_WAITING_FIELD_REVIEW:
            # 等待核对是正常等待点：另建批次会重新抽取全部字段，等于绕过核对。
            raise BusinessError(
                "AUDIT_TASK_WAITING_FIELD_REVIEW",
                "任务正在等待字段核对，请在字段核对中确认后继续审核；不能另建批次绕过核对",
                status_code=409,
            )
        # 必须在进入图之前就拦住并发，否则第二个请求会与正在执行的图互相覆盖。
        if task.status == "RUNNING":
            raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能并发执行或重跑", status_code=409)
        if request.node_code != task.current_node:
            raise BusinessError("AUDIT_NODE_MISMATCH", "提交节点与任务当前节点不一致", status_code=409)
        running_execution = AuditExecutionCRUD(self.db).running_for_task(task.id)
        if running_execution:
            raise BusinessError("AUDIT_NODE_RUNNING", "当前任务已有执行中的节点，不能并发执行或重跑", status_code=409)
        if task.status in {"BLOCKED", "FAILED"}:
            # 只在本次执行事务内恢复可执行状态，不在后台命令受理时单独提交复位。
            # _begin_node_execution 会失效旧模型结果并建立新批次；失败原因保留供审计。
            _, nodes, current_index = _scenario_nodes(self.db, task)
            task.status = TASK_STATUS_BLOCKED
            task.current_node = nodes[current_index]["node_code"]
            AuditExecutionCRUD(self.db).flush()
        # 图节点多次提交，最终推进也可能失败；用本次执行身份收尾，不能误伤其他批次。
        attempt = {"task_no": task_no, "execution_id": None}
        self.db.info["audit_execution_attempt"] = attempt
        try:
            state = run_audit_node(task.task_no, self.db, audit_runner)
            raise_graph_error(state)
            result = state.get("last_execution")
            if not isinstance(result, dict):
                raise BusinessError("AUDIT_GRAPH_RESULT_MISSING", "审核流程未返回当前节点执行结果", status_code=500,
                                    details={"phase": state.get("phase"), "execution_id": state.get("execution_id")})
            if result.get("failure_code") == AUDIT_LLM_CHANNEL_UNAVAILABLE:
                # 暂停不是成功：后台操作按真实失败结束，页面才会提示继续执行而不是「操作已完成」。
                # 批次保持 BLOCKED 与逐条结果，续跑沿用同一批次。
                raise BusinessError(
                    AUDIT_LLM_CHANNEL_UNAVAILABLE,
                    str(result.get("failure_message") or "远程大模型渠道不可用，本次执行已暂停"),
                    status_code=502,
                )
            return result
        except BusinessError as exc:
            if attempt["execution_id"] is not None:
                fail_running_execution(self.db, task_no, execution_id=attempt["execution_id"], code=exc.code,
                                       message=str(exc.details.get("reason") or exc.message))
            raise
        except Exception as exc:
            logger.exception("审核流程执行发生未处理异常", extra={"task_no": task_no, "node_code": request.node_code})
            if attempt["execution_id"] is not None:
                fail_running_execution(self.db, task_no, execution_id=attempt["execution_id"],
                                       code="AUDIT_GRAPH_RUNTIME_FAILED", message=exception_summary(exc))
            raise BusinessError("AUDIT_GRAPH_RUNTIME_FAILED", "审核流程执行失败，具体原因见执行记录和服务日志",
                                status_code=500, details={"reason": exception_summary(exc)}) from exc
        finally:
            self.db.info.pop("audit_execution_attempt", None)


    def _waiting_review_execution(self, task: AuditTask) -> NodeExecution:
        """任务当前停在字段核对等待点的执行批次。"""
        execution = AuditExecutionCRUD(self.db).waiting_for_node(task.id, task.current_node)
        if execution is None:
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_NOT_WAITING",
                "任务当前没有停在字段核对等待点的执行批次，请刷新任务后重试",
                status_code=409,
                details={"task_no": task.task_no, "status": task.status},
            )
        return execution


    def _assert_field_review_editable(self, task: AuditTask, input_revision: int) -> NodeExecution:
        """保存与确认共用的前置校验：只有停在该等待点、且看不到旧输入时才能改。"""
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_AUDIT_TASK, task.task_no)
        if task.status != TASK_STATUS_WAITING_FIELD_REVIEW:
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_NOT_WAITING",
                f"任务当前状态为 {task.status}，不在字段核对等待点，不能保存核对修改",
                status_code=409,
                details={"task_no": task.task_no, "status": task.status},
            )
        if int(input_revision) != int(task.input_revision):
            raise BusinessError(
                "INPUT_REVISION_CONFLICT",
                "任务输入在核对期间已被其他操作更新，本次提交基于过期页面；请刷新后重新核对",
                status_code=409,
                details={"submitted": int(input_revision), "current": int(task.input_revision)},
            )
        execution = self._waiting_review_execution(task)
        reviewed_extraction_keys(execution.result_json or {}, waiting=True)
        return execution


    def save_field_review(self, task_no: str, request: FieldReviewSaveRequest) -> dict[str, Any]:
        """只保存核对修改：任务继续停在等待核对，不触发后续执行。

        与字段纠错不同，这里不失效当前批次、不重算下游：后续规则尚未执行，
        它们读到的就是核对后的有效值。
        """
        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        execution = self._assert_field_review_editable( task, request.input_revision)
        previous_revision = int(task.input_revision)
        applied = field_review_service.AuditFieldReviewService(self.db).apply_corrections(task, list(request.corrections))
        if applied:
            AuditMaterialPrecheckService(self.db).retain_after_field_review(task, execution, previous_revision=previous_revision)
        AuditExecutionCRUD(self.db).flush()
        missing = field_review_service.AuditFieldReviewService(self.db).missing_fields(task)
        return {
            "task_no": task.task_no,
            "input_revision": int(task.input_revision),
            "applied": applied,
            "missing_field_count": len(missing),
            "missing_fields": missing,
        }


    def confirm_field_review(self, task_no: str, *, execution_id: int, corrections: list[FieldReviewCorrection],
        note: str | None, operation_no: str,
    ) -> dict[str, Any]:
        """保存本次剩余修改并确认当前字段版本，然后在同一批次里继续审核。

        修改与确认先在同一事务里提交，再调用续跑：模型调用失败或渠道暂停时，
        已保存的字段值与此前的确认不会丢，重新继续时也不会要求重复核对。
        """
        from app.modules.audit.execution.service.resume import AuditExecutionResumeService

        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        if task.status != TASK_STATUS_WAITING_FIELD_REVIEW:
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_NOT_WAITING",
                f"任务当前状态为 {task.status}，不在字段核对等待点，不能确认继续审核",
                status_code=409,
                details={"task_no": task_no, "status": task.status},
            )
        execution = AuditExecutionCRUD(self.db).by_id(execution_id)
        if (
            execution is None
            or execution.audit_task_id != task.id
            or execution.node_code != task.current_node
        ):
            raise BusinessError(
                "AUDIT_EXECUTION_MISSING", "确认对应的执行批次不属于任务当前节点",
                status_code=409, details={"task_no": task_no, "execution_id": execution_id},
            )
        if execution.status != TASK_STATUS_WAITING_FIELD_REVIEW:
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_NOT_WAITING",
                f"执行批次状态为 {execution.status}，不在字段核对等待点",
                status_code=409,
                details={"execution_id": execution.id, "status": execution.status},
            )
        reviewed_extraction_keys(execution.result_json or {}, waiting=True)
        previous_revision = int(task.input_revision)
        applied = field_review_service.AuditFieldReviewService(self.db).apply_corrections(task, corrections, operation_no=operation_no,
        )
        if applied:
            AuditMaterialPrecheckService(self.db).retain_after_field_review(task, execution, previous_revision=previous_revision)
        confirmation = field_review_service.AuditFieldReviewService(self.db).record_confirmation(task, execution, note=note, operation_no=operation_no,
        )
        commit_transaction(self.db)
        result = AuditExecutionResumeService(self.db).resume_interrupted_execution( task_no, execution_id=execution.id)
        return {
            **result,
            "field_review_confirmation_no": confirmation.confirmation_no,
            "field_review_applied": applied,
        }


    def correct_field_value(self, task_no: str, request: FieldCorrectionRequest) -> dict[str, Any]:
        """主动字段纠错入口：对 PRESENT 错值与 NOT_PRESENT 漏值统一处理。

        与人工复核共用同一套类型校验、事实版本与下游重算通道：生成新的不可变事实版本、
        把旧的退出当前位、失效依据旧事实的待办，并从实际引用该字段的节点开始重算。
        旧事实、旧复核与旧证据保持可追溯。
        """
        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_AUDIT_TASK, task.task_no)
        if task.status == TASK_STATUS_WAITING_FIELD_REVIEW:
            # 纠错会失效整个执行批次；等待核对期间改字段必须走核对保存，
            # 否则用户改一个字段就会被退回重跑全部规则，还要重新核对。
            raise BusinessError(
                "AUDIT_FIELD_REVIEW_IN_PROGRESS",
                "任务正在等待字段核对；请在字段核对区域直接修改并确认继续，纠错入口会失效整个执行批次",
                status_code=409,
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
        document = MaterialDocumentCRUD(self.db).by_task_and_material(task.id, request.material_key)
        if not document:
            raise BusinessError(
                "TASK_DOCUMENT_NOT_FOUND",
                "任务材料位不存在",
                status_code=404,
                details={"material_key": request.material_key},
            )
        upload = current_upload(self.db, document)
        if upload is None:
            raise BusinessError(
                "FIELD_FACT_MISSING",
                "该材料位尚未上传当前文件，没有可纠错的字段事实",
                status_code=404,
                details={"material_key": request.material_key},
            )
        current_pair = AuditFieldFactsService(self.db).current_field_fact_pairs( task).get((request.field_key, request.material_key))
        if current_pair is None:
            raise BusinessError(
                "FIELD_FACT_MISSING",
                "当前上传没有成对生成的字段版本与统一事实，不能纠错",
                status_code=404,
                details={"field_key": request.field_key},
            )
        fact, _ = current_pair
        # 取值在任务 CSV；表里只留版本与当前位。
        fact_value = task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_TASK_FIELD_VALUE, fact.id)
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
            new_status, new_raw = FIELD_STATUS_REVIEWED, request.field_value.strip()
        else:
            if request.field_value not in (None, ""):
                raise BusinessError("FIELD_CORRECTION_VALUE_UNEXPECTED", "漏值纠正不能填写字段值")
            new_status, new_raw = FIELD_STATUS_NOT_PRESENT, None
        system_judgment = {
            "status": fact.status,
            "raw_value": fact_value.get("raw_value"),
            "normalized_value": fact_value.get("normalized_value_json"),
            # 原判断绑定旧字段版本；引文保存在该版本 CSV 中，不再复制到数据库快照。
            "source_field_value_id": fact.id,
        }
        new_fact, previous_revision_no, invalidated_reviews = AuditFieldFactsService(self.db)._write_human_field_fact(

            task,
            field_key=request.field_key,
            material_key=request.material_key,
            document_upload_id=upload.id,
            status=new_status,
            raw_value=new_raw,
            normalized_value=normalized_value,
        )
        review = ManualReview(
            review_no=public_no("REV"),
            audit_task_id=task.id,
            field_value_id=new_fact.id,
            status="RESOLVED",
            system_judgment_json=system_judgment,
            decision=request.decision,
            business_note=request.business_note,
            before_value=fact_value.get("raw_value"),
            after_value=new_raw,
            basis_type=request.basis_type,
            decision_command_no=AuditFieldReviewsService(self.db)._decision_command_no( task),
        )
        AuditFieldCRUD(self.db).add(review)
        task.input_revision += 1
        AuditExecutionCRUD(self.db).flush()
        manifest = _task_manifest(task)
        _, nodes, _ = _scenario_nodes(self.db, task)
        recomputed_rules = []
        invalidation = AuditFieldReviewsService(self.db)._invalidate_validation_results( task, manifest, {request.field_key})
        # 事实变化后按 DAG 取第一个尚未完成的启用节点，计算必须先于依赖它的校验和风险重跑。
        next_node = AuditFieldReviewsService(self.db)._next_node_for_recalculation( task, manifest, nodes)
        if next_node is None:
            # 没有规则、计算或报告消费该字段，且 DAG 已全部完成：字段事实仍应保存，
            # 不能把“无需重算”当成纠错失败。
            task.status = TASK_STATUS_COMPLETED
            AuditExecutionCRUD(self.db).flush()
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
        task.status = TASK_STATUS_READY
        task.current_node = next_node["node_code"]
        AuditExecutionCRUD(self.db).flush()
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


    def _document_ocr_retry_target(self, task: AuditTask, document_no: str, artifact_no: str | None, *, resubmit_unknown: bool,
    ) -> tuple[TaskDocument, DocumentUpload, StoredArtifact, StoredArtifact | None]:
        """只定位当前上传版本；结果未知的作业需要用户明确确认才重新提交。"""
        if task.status in {"RUNNING", "COMPLETED"}:
            raise BusinessError("TASK_DOCUMENT_LOCKED", "任务正在执行或已结束，不能重试材料识别", status_code=409)
        document = MaterialDocumentCRUD(self.db).by_task_and_no(task.id, document_no)
        if document is None:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
        upload = current_upload(self.db, document)
        if upload is None:
            raise BusinessError("OCR_UPLOAD_MISSING", "该材料位没有当前上传文件", status_code=409)
        from app.modules.audit.execution.service.material_precheck import AuditMaterialPrecheckService
        AuditMaterialPrecheckService(self.db).assert_processing_allowed(task, upload)
        artifact = AuditArtifactArtifactsService(self.db)._artifact_for_upload( upload, "OCR")
        if (artifact.artifact_no if artifact else None) != artifact_no:
            raise BusinessError("OCR_ARTIFACT_STALE", "OCR 作业记录已变化，请刷新后重试", status_code=409)
        if artifact is not None and artifact.status == "SUBMITTING" and not resubmit_unknown:
            raise BusinessError(
                "OCR_SUBMISSION_STATE_UNKNOWN", "OCR 提交结果未知；重新提交可能创建重复作业，需要在材料处理状态中确认后操作", status_code=409,
            )
        if artifact is not None and artifact.status not in {"PENDING", "FAILED", "SUBMITTING"}:
            raise BusinessError("OCR_RETRY_STATE_INVALID", "当前 OCR 状态不支持重试，请刷新处理状态", status_code=409)
        parsed = AuditArtifactArtifactsService(self.db)._artifact_for_upload( upload, "DOCUMENT_PARSE")
        if parsed is None or parsed.status != "READY":
            raise BusinessError("PROCESSED_ARTIFACT_MISSING", "该材料尚无已完成的去章产物，请先执行材料前置处理", status_code=409)
        return document, upload, parsed, artifact


    def retry_document_ocr(self, task: AuditTask, document_no: str, artifact_no: str | None, *, operation_no: str,
        resubmit_unknown: bool = False,
    ) -> dict[str, Any]:
        """只处理一份材料：续查原作业，或由用户主动提交一次新识别。"""
        document, upload, parsed, artifact = self._document_ocr_retry_target(
             task, document_no, artifact_no, resubmit_unknown=resubmit_unknown,
        )
        if artifact is None:
            artifact = ArtifactExecution(self.db)._submit_ocr_job( task, upload, parsed)
        elif artifact.status in {"FAILED", "SUBMITTING"}:
            metadata = dict(artifact.metadata_json or {})
            attempts = list(metadata.pop("submit_attempts", []) or [])
            attempt = {
                "at": datetime.now(UTC).isoformat(),
                "reason": "用户确认重新提交结果未知的 OCR" if artifact.status == "SUBMITTING" else "用户重试当前材料的 OCR 识别",
                "idempotency_key": operation_no, "state": "SUBMITTING",
                "previous_status": artifact.status, "previous_relative_path": artifact.relative_path,
                "previous_metadata": metadata,
                "previous_external_job_id": artifact.external_job_id,
                "previous_deadline_at": artifact.deadline_at.isoformat() if artifact.deadline_at else None,
                "previous_failure_code": artifact.failure_code,
                "previous_failure_message": artifact.failure_message,
                "resubmit_unknown": resubmit_unknown,
            }
            artifact.status = "SUBMITTING"
            artifact.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.submitting"
            artifact.metadata_json = {**metadata, "submit_attempts": [*attempts, attempt]}
            commit_transaction(self.db)  # 新提交意图和旧作业记录先保存，重复点击不会再提交。
            try:
                artifact = ArtifactExecution(self.db)._submit_authorized_ocr_retry(
                     task, upload, parsed, artifact, idempotency_key=operation_no,
                )
            except Exception:
                commit_transaction(self.db)  # 保留真实的提交失败或结果未知状态，后台异常继续上抛。
                raise
        elif artifact.status == "PENDING":
            _, old_deadline = AuditArtifactArtifactsService()._ocr_job_metadata(artifact)
            resumed_at = datetime.now(UTC)
            artifact.deadline_at = resumed_at + timedelta(seconds=get_settings().ocr_max_wait_seconds)
            artifact.metadata_json = {
                **artifact.metadata_json,
                "previous_deadline_at": old_deadline.isoformat(),
                "resumed_at": resumed_at.isoformat(), "resume_operation_no": operation_no,
            }
            commit_transaction(self.db)  # 只延长这次用户主动续查的等待时间，不创建远端作业。
        artifact = ArtifactExecution(self.db)._complete_ocr_job( task, upload, artifact)
        AuditArtifactArtifactsService()._artifact_payload(artifact)
        stages = [AuditArtifactArtifactsService(self.db)._artifact_for_upload( upload, stage) for stage in ("MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL", "OCR")]
        if all(item is not None and item.status == "READY" for item in stages):
            for item in stages:
                AuditArtifactArtifactsService()._artifact_payload(item)
            document.status = "PREPARED"
        commit_transaction(self.db)
        return {
            "document_no": document.document_no, "artifact_no": artifact.artifact_no,
            "status": artifact.status, "document_status": document.status,
            "message": "当前材料的正文识别结果已保存，其他材料的处理结果已保留",
        }
