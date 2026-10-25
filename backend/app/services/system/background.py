"""后台长操作：先认领记录，再用独立 Session 执行。

请求只返回受理状态（202 语义），真正的结果写回 background_operation。
同一对象的执行、恢复、重跑、改绑定、裁决、替换文件共用同一套互斥规则：
只要该对象存在活跃操作（PENDING/RUNNING），新的不同请求明确返回冲突；
相同幂等标识的重复请求返回已有记录，不重复执行。

后台执行失败不会静默：状态写 FAILED 并保留错误码与中文原因。"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError, exception_summary, safe_error_details
from ...core.database import SessionLocal
from ...domain.identity import public_no
from ...entity.operations import BackgroundOperation
from .operation_log import normalized_request_hash

logger = logging.getLogger(__name__)


ACTIVE_STATUSES = ("PENDING", "RUNNING")


PHASE_COMMAND_ACCEPTED = "COMMAND_ACCEPTED"


PHASE_LLM_REQUEST = "LLM_REQUEST"


PHASE_BUSINESS_COMMITTED = "BUSINESS_COMMITTED"


PHASE_GRAPH_ADVANCED = "GRAPH_ADVANCED"


PHASE_INTERRUPT_WAITING = "INTERRUPT_WAITING"


PHASE_RESULT_READY = "RESULT_READY"


def _request_diff(existing: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
    keys = sorted(set(existing) | set(incoming))
    return [key for key in keys if existing.get(key) != incoming.get(key)]


def _active_operation(db: Session, object_type: str, object_no: str) -> BackgroundOperation | None:
    return db.scalar(
        select(BackgroundOperation).where(
            BackgroundOperation.object_type == object_type,
            BackgroundOperation.object_no == object_no,
            BackgroundOperation.status.in_(ACTIVE_STATUSES),
        )
    )


def assert_no_active_operation(db: Session, object_type: str, object_no: str) -> None:
    """改绑定、裁决、替换文件等入口共用同一互斥规则。

    这些入口本身不必异步，但必须与后台执行互斥，避免在节点正在执行时改动输入。
    """
    active = _active_operation(db, object_type, object_no)
    if active is not None:
        raise BusinessError(
            "OPERATION_ALREADY_ACTIVE",
            "该对象已有正在进行的操作，请等待完成或查询其状态",
            status_code=409,
            details={"operation_no": active.operation_no, "status": active.status},
        )


def operation_by_no(db: Session, operation_no: str) -> BackgroundOperation:
    """按操作编号取后台命令；取不到即 404。"""
    operation = db.scalar(select(BackgroundOperation).where(BackgroundOperation.operation_no == operation_no))
    if not operation:
        raise BusinessError(
            "OPERATION_NOT_FOUND",
            "后台操作不存在",
            status_code=404,
            details={"operation_no": operation_no},
        )
    return operation


def latest_operation(db: Session, object_type: str, object_no: str) -> BackgroundOperation | None:
    """该对象最近一次后台命令，页面刷新后凭它继续查询真实状态。"""
    return db.scalar(
        select(BackgroundOperation)
        .where(BackgroundOperation.object_type == object_type, BackgroundOperation.object_no == object_no)
        .order_by(BackgroundOperation.id.desc())
    )


def operation_view(operation: BackgroundOperation, *, reused: bool = False) -> dict[str, Any]:
    """后台命令的对外视图：状态、阶段、结果，以及是否复用了原命令。"""
    return {
        "operation_no": operation.operation_no,
        "object_no": operation.object_no,
        "object_type": operation.object_type,
        "operation_type": operation.operation_type,
        "target_node": operation.target_node,
        "input_version": operation.input_version,
        "status": operation.status,
        "phase": operation.phase,
        "external_job_ref": operation.external_job_ref,
        "message": _status_message(operation),
        "reused": reused,
        "result": operation.result_json,
        "raw_response_path": operation.raw_response_path,
        "failure_code": operation.failure_code,
        "failure_message": operation.failure_message,
        "created_at": operation.created_at,
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
    }


def _status_message(operation: BackgroundOperation) -> str:
    return {
        "PENDING": "操作已受理，等待后台执行",
        "RUNNING": "操作已受理，正在后台执行",
        "SUCCEEDED": "操作已完成",
        "FAILED": "操作失败",
        "INTERRUPTED": "操作被中断，可重新发起",
    }.get(operation.status, "状态未知")


def _update_operation(
    operation_id: int,
    *,
    phase: str | None = None,
) -> None:
    """用独立 Session 写命令的阶段与中间结果，不干扰执行中的业务事务。"""
    session = SessionLocal()
    try:
        operation = session.get(BackgroundOperation, operation_id)
        if operation is None:
            return
        if phase is not None:
            operation.phase = phase
        session.commit()
    finally:
        session.close()


@dataclass
class CommandHandle:
    """已认领命令的执行上下文。

    只暴露已持久化的命令内容（request_json）与阶段推进，runner 不得用当前请求对象重算。
    resume_committed=True 表示同一命令此前已提交本地业务结果，本次只应补齐图推进。
    """

    operation_id: int
    operation_no: str
    request: dict[str, Any]
    resume_committed: bool

    def phase(self, phase: str | None) -> None:
        """把命令推进到明确阶段；取不到阶段时传 None，绝不编造。"""
        if phase is not None:
            _update_operation(self.operation_id, phase=phase)


def _run_in_background(operation_id: int, runner: Callable[[Session, CommandHandle], dict[str, Any]]) -> None:
    """后台线程入口。必须使用独立 Session，禁止复用已结束请求的 Session。"""
    session = SessionLocal()
    try:
        operation = session.scalar(
            select(BackgroundOperation).where(BackgroundOperation.id == operation_id).with_for_update()
        )
        if operation is None or operation.status not in ACTIVE_STATUSES:
            # 含已被中止（INTERRUPTED）的操作：线程可能在受理后才拿到执行权，
            # 此时必须退出，不能把中止过的命令再执行一遍。
            return
        operation.status = "RUNNING"
        operation.started_at = datetime.now(UTC)
        command = CommandHandle(
            operation_id=operation_id,
            operation_no=operation.operation_no,
            request=dict(operation.request_json or {}),
            resume_committed=operation.phase == PHASE_BUSINESS_COMMITTED,
        )
        session.commit()
        try:
            result = runner(session, command)
        except BusinessError as exc:
            session.rollback()
            _finish(session, operation_id, "FAILED", None, exc.code, exc.message)
            logger.warning(
                "后台操作失败 operation_no=%s code=%s message=%s details=%s",
                operation.operation_no, exc.code, exc.message, safe_error_details(exc.details),
            )
        except Exception as exc:  # noqa: BLE001 - 未处理异常必须显式落库，不能静默
            session.rollback()
            # 摘要带上异常类型与出错位置：只写「请查看服务日志」时，日志一旦丢失，
            # 这次失败就再也查不出原因。完整堆栈仍由 logger.exception 记录。
            _finish(
                session,
                operation_id,
                "FAILED",
                None,
                "OPERATION_UNEXPECTED_ERROR",
                f"后台操作发生未处理异常：{exception_summary(exc)}",
            )
            logger.exception("后台操作发生未处理异常 operation_id=%s", operation_id, exc_info=exc)
        else:
            _finish(session, operation_id, "SUCCEEDED", result, None, None)
    finally:
        session.close()


def _finish(
    session: Session,
    operation_id: int,
    status: str,
    result: dict[str, Any] | None,
    failure_code: str | None,
    failure_message: str | None,
) -> None:
    operation = session.scalar(
        select(BackgroundOperation).where(BackgroundOperation.id == operation_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if operation is None:
        return
    if operation.status == "INTERRUPTED":
        logger.info("后台操作已中止，保留中止状态 operation_no=%s", operation.operation_no)
        return
    operation.status = status
    operation.result_json = result
    operation.failure_code = failure_code
    operation.failure_message = failure_message
    operation.finished_at = datetime.now(UTC)
    session.commit()


def submit_operation(
    db: Session,
    *,
    object_type: str,
    object_no: str,
    operation_type: str,
    idempotency_key: str,
    request: dict[str, Any],
    runner: Callable[[Session, CommandHandle], dict[str, Any]],
    target_node: str | None = None,
    input_version: str | None = None,
    validate: Callable[[], None] | None = None,
) -> tuple[BackgroundOperation, bool]:
    """认领并调度一个后台操作。

    返回 (operation, reused)。reused=True 表示这是同一幂等标识、同一规范化请求的重复提交，
    直接返回已有记录，不执行 runner、也不再校验当前业务状态——只有真正的新命令才做
    活跃操作互斥与状态校验。
    调用方必须已经锁定了对象行（任务或导入作业），避免并发提交。
    """
    if not idempotency_key:
        raise BusinessError("OPERATION_IDEMPOTENCY_REQUIRED", "后台操作缺少幂等标识", status_code=400)
    request_hash = normalized_request_hash(request)
    same_request = db.scalar(
        select(BackgroundOperation).where(
            BackgroundOperation.object_type == object_type,
            BackgroundOperation.object_no == object_no,
            BackgroundOperation.operation_type == operation_type,
            BackgroundOperation.idempotency_key == idempotency_key,
        )
    )
    if same_request is not None:
        # 历史命令优先于状态校验：成功但响应丢失后的重发必须复用原命令，不能被误报为状态错误。
        stored_hash = same_request.request_hash or normalized_request_hash(dict(same_request.request_json or {}))
        if stored_hash == request_hash:
            return same_request, True
        raise BusinessError(
            "OPERATION_IDEMPOTENCY_CONFLICT",
            "同一幂等标识已用于不同的请求内容，拒绝复用已有操作",
            status_code=409,
            details={
                "operation_no": same_request.operation_no,
                "diff_keys": _request_diff(dict(same_request.request_json or {}), request),
            },
        )
    active = _active_operation(db, object_type, object_no)
    if active is not None:
        raise BusinessError(
            "OPERATION_ALREADY_ACTIVE",
            "该对象已有正在进行的操作，请等待完成或查询其状态",
            status_code=409,
            details={"operation_no": active.operation_no, "status": active.status},
        )
    if validate is not None:
        validate()
    operation = BackgroundOperation(
        operation_no=public_no("OP"),
        object_type=object_type,
        object_no=object_no,
        operation_type=operation_type,
        idempotency_key=idempotency_key,
        target_node=target_node,
        input_version=input_version,
        status="PENDING",
        request_hash=request_hash,
        phase=PHASE_COMMAND_ACCEPTED,
        request_json=request,
    )
    db.add(operation)
    db.flush()
    operation_id = operation.id
    db.commit()
    try:
        thread = threading.Thread(
            target=_run_in_background,
            args=(operation_id, runner),
            name=f"baoli-op-{operation.operation_no}",
            daemon=True,
        )
        thread.start()
    except RuntimeError as exc:
        # 调度失败必须明确失败并解除活跃占用，不能返回假受理。
        _finish(db, operation_id, "FAILED", None, "OPERATION_DISPATCH_FAILED", "后台执行线程启动失败")
        raise BusinessError("OPERATION_DISPATCH_FAILED", "后台执行线程启动失败", status_code=500) from exc
    db.refresh(operation)
    return operation, False


def recover_interrupted_operations(db: Session) -> dict[str, int]:
    """启动时把上次遗留的活跃操作标记为中断。

    等待中的任务不是遗留执行：只有 background_operation 里仍处于
    PENDING/RUNNING 的记录才会被标记，业务等待点不会被误判。
    已推进到 BUSINESS_COMMITTED 的命令保留 result_json 与 phase：它的本地业务结果已经
    提交，重启后只能按同一命令补齐图推进，不能被当成“什么都没做”重跑。
    """
    stale = db.scalars(
        select(BackgroundOperation).where(BackgroundOperation.status.in_(ACTIVE_STATUSES))
    ).all()
    for operation in stale:
        operation.status = "INTERRUPTED"
        operation.failure_code = operation.failure_code or "OPERATION_INTERRUPTED"
        if operation.phase == PHASE_BUSINESS_COMMITTED:
            operation.failure_message = "服务重启导致图推进中断；该命令的本地业务结果已提交，请按同一命令补齐图推进"
        else:
            operation.failure_message = "服务重启导致该操作中断，请重新发起"
        operation.finished_at = datetime.now(UTC)
    db.commit()
    return {"interrupted_operations": len(stale)}
