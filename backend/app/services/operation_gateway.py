"""把长耗时入口接入统一的后台认领/执行机制。

请求事务内先锁定对象、占用活跃操作，提交后再调度后台执行；请求只返回受理状态。
复核恢复、重试、节点执行、裁决等可能触发长处理的入口都走这里，不留隐蔽的同步长请求。
命令一旦认领，其请求内容（request_json）就是唯一事实来源：runner 只从它读取参数，
阶段（phase）由 runner 按真实推进逐段上报，恢复时据此决定唯一推进动作。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import BusinessError
from ..models import AuditTask, ImportJob, ImportSheet
from ..schemas import ExecuteNodeRequest, ManualReviewRequest
from . import background_service
from .audit.state import _single_node_code_of_type
from .background_service import (
    PHASE_GRAPH_ADVANCED,
    PHASE_INTERRUPT_WAITING,
    normalized_request_hash,
)
from .operation_types import OBJECT_AUDIT_TASK, OBJECT_IMPORT_JOB


def _manifest_of_task(db: Session, task: AuditTask) -> dict[str, Any]:
    """任务创建时冻结的方案 manifest。

    节点归属必须按它解析：`task.current_node` 是**节点实例码**（用户配置的），
    拿它与能力类型比较在用户改了实例码之后恒为假，人工复核等待点会永远判定不成立。
    """
    manifest = task.manifest_json
    if not isinstance(manifest, dict) or not manifest:
        raise BusinessError("TASK_MANIFEST_MISSING", "任务缺少已冻结的执行配置", status_code=500)
    return manifest




def _idempotency_key(provided: str | None) -> str:
    """未提供幂等标识时生成一次性标识。

    这样同一对象的并发提交由活跃操作互斥拦下（明确 409），而不是被误当成同一请求复用。
    调用方提供标识时（前端每次点击生成一个），重复请求才会返回已有操作。
    """
    from ..domain.identity import public_no

    return (provided or "").strip() or public_no("IDEM")


def _import_bindings_hash(db: Session, job: ImportJob) -> str:
    """工作表绑定的规范化摘要：绑定被改动后旧命令必须被识别为过期。"""
    sheets = db.scalars(
        select(ImportSheet).where(ImportSheet.import_job_id == job.id).order_by(ImportSheet.sheet_key)
    ).all()
    return normalized_request_hash(
        {
            "sheets": [
                {
                    "sheet_key": item.sheet_key,
                    "status": item.status,
                    "bound_role": item.bound_role,
                    "header_row": item.header_row,
                }
                for item in sheets
            ]
        }
    )


def _assert_import_command_current(db: Session, job: ImportJob, content: dict[str, Any]) -> None:
    """执行前核对命令内容仍是当前输入，过期命令不按新输入重算。"""
    if (
        job.source_sha256 != content.get("source_sha256")
        or job.draft_revision != content.get("draft_revision")
        or _import_bindings_hash(db, job) != content.get("bindings_hash")
    ):
        raise BusinessError(
            "IMPORT_CONVERSION_INPUT_STALE",
            "该转换命令对应的来源文件、草稿修订或工作表绑定已被改动，拒绝按过期命令执行",
            status_code=409,
        )


def _reject_unsupported_resume(command: background_service.CommandHandle) -> None:
    """命令已提交本地业务但图未推进时，只能补齐图推进，绝不能重做业务写入。

    审核服务的业务写入与图推进目前在同一函数内完成，无法只补齐图推进；
    因此明确拒绝，而不是重放一次可能重复生效的业务写入。
    """
    if command.resume_committed:
        raise BusinessError(
            "OPERATION_RESUME_UNSUPPORTED",
            "该命令的本地业务结果已提交但图未推进，无法在不重做业务写入的前提下补齐，请重新发起",
            status_code=409,
        )


def submit_conversion(
    db: Session,
    job: ImportJob,
    *,
    idempotency_key: str | None,
) -> tuple[Any, bool]:
    """提交规则转换为后台操作。"""
    from . import import_service

    request = {
        "job_no": job.job_no,
        "source_sha256": job.source_sha256,
        "draft_revision": job.draft_revision,
        "bindings_hash": _import_bindings_hash(db, job),
    }

    def validate() -> None:
        import_service._ensure_import_mutable(db, job)
        if job.status in {"PUBLISHED", "APPLIED"}:
            raise BusinessError(
                "IMPORT_ALREADY_PUBLISHED" if job.status == "PUBLISHED" else "IMPORT_ALREADY_APPLIED",
                "该导入作业已写入场景或已产生版本，不能重新转换",
                status_code=409,
            )

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        from . import import_service

        # 导入图的推进与业务写入在同一服务函数内，无法只补齐图推进，明确拒绝而不是重做写入。
        _reject_unsupported_resume(command)
        content = command.request
        current = session.scalar(select(ImportJob).where(ImportJob.job_no == content["job_no"]))
        if not current:
            raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": content["job_no"]})
        _assert_import_command_current(session, current, content)
        converted = import_service.retry_conversion(session, content["job_no"])
        # retry_conversion 内部同时完成草稿落库与导入图转换；转换后若停在裁决等待点即处于等待 interrupt。
        command.phase(PHASE_INTERRUPT_WAITING if converted.status == "PENDING_DECISION" else PHASE_GRAPH_ADVANCED)
        return {
            "job_no": converted.job_no,
            "status": converted.status,
            "failure_code": converted.failure_code,
            "failure_message": converted.failure_message,
        }

    return background_service.submit_operation(
        db,
        object_type=OBJECT_IMPORT_JOB,
        object_no=job.job_no,
        operation_type="IMPORT_CONVERT",
        idempotency_key=_idempotency_key(idempotency_key),
        request=request,
        runner=runner,
        target_node=None,
        input_version=str(job.draft_revision),
        validate=validate,
    )


def _reject_deleted_task(task: AuditTask) -> None:
    """已删除（逻辑删除）的任务不能再接受任何写操作；恢复后才能继续。"""
    if task.deleted_at is not None:
        raise BusinessError(
            "AUDIT_TASK_DELETED",
            "任务已被删除，请先恢复后再继续操作",
            status_code=409,
            details={"task_no": task.task_no},
        )


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


def submit_manual_reviews(
    db: Session,
    task: AuditTask,
    *,
    reviews: list[dict[str, Any]],
    idempotency_key: str | None,
    input_revision: int | None = None,
) -> tuple[Any, bool]:
    """提交人工复核恢复为后台操作。

    复核恢复会重新进入审核图（写 checkpoint、推进等待点），因此与节点执行共用
    同一套受理/互斥机制，不留下隐蔽的同步长请求。复核明细整体落库，重发时逐条比对。
    """
    from .audit import commands as audit_commands

    normalized_reviews = [
        {
            "review_no": item["review_no"],
            "decision": item["decision"],
            "business_note": item["business_note"],
            "field_value": item.get("field_value"),
            "basis_type": item.get("basis_type", "HUMAN_STATEMENT"),
        }
        for item in reviews
    ]
    request = {
        "task_no": task.task_no,
        "input_revision": input_revision,
        "reviews": normalized_reviews,
    }

    def validate() -> None:
        _reject_deleted_task(task)
        # 人工复核等待点按**能力类型**解析实例码：`task.current_node` 是实例码，
        # 与能力类型比较在用户改了实例码之后恒为假，复核会被永远挡在门外。
        review_node_code = _single_node_code_of_type(
            _manifest_of_task(db, task), "MANUAL_REVIEW", purpose="人工复核等待点"
        )
        if task.status != "WAITING_REVIEW" or task.current_node != review_node_code:
            raise BusinessError("MANUAL_REVIEW_NOT_WAITING", "任务当前不在人工复核等待状态", status_code=409)
        _reject_stale_input_revision(task, input_revision)

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        _reject_unsupported_resume(command)
        content = command.request
        submitted = ManualReviewRequest.model_validate(
            {"reviews": content["reviews"], "input_revision": content.get("input_revision")}
        )
        result = audit_commands.submit_manual_reviews(session, content["task_no"], submitted)
        # 复核恢复后图推进到下一个等待点（任务完成时不再有等待点）。
        command.phase(PHASE_GRAPH_ADVANCED if result.get("status") == "COMPLETED" else PHASE_INTERRUPT_WAITING)
        return result

    return background_service.submit_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        operation_type="MANUAL_REVIEW_RESUME",
        idempotency_key=_idempotency_key(idempotency_key),
        request=request,
        runner=runner,
        # 目标节点记**实例码**：它是本次命令真实落在哪个节点上的执行事实，
        # 能力类型在不同场景里对应不同的实例码。
        target_node=_single_node_code_of_type(
            _manifest_of_task(db, task), "MANUAL_REVIEW", purpose="人工复核等待点"
        ),
        input_version=str(task.input_revision),
        validate=validate,
    )


def submit_node_execution(
    db: Session,
    task: AuditTask,
    *,
    node_code: str,
    idempotency_key: str | None,
    input_revision: int | None = None,
) -> tuple[Any, bool]:
    """提交审核节点执行为后台操作。"""
    from .audit import commands as audit_commands

    request = {"task_no": task.task_no, "node_code": node_code, "input_revision": input_revision}

    def validate() -> None:
        _reject_deleted_task(task)
        if task.status in {"COMPLETED", "FAILED"}:
            raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
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
