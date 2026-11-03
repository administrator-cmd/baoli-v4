"""后台操作的阶段事务、线程执行与中断恢复。"""

import logging
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError, exception_summary, safe_error_details
from app.core.database import SessionLocal
from app.core.transactions import commit_transaction, rollback_transaction
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.background import (
    ACTIVE_STATUSES, PHASE_BUSINESS_COMMITTED, CommandHandle,
)

logger = logging.getLogger(__name__)


class OperationExecution:
    """命令认领、业务工作单元与最终状态分别持久化。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _update_operation(self, operation_id: int,
        *,
        phase: str | None = None,
    ) -> None:
        """用独立 Session 写命令的阶段与中间结果，不干扰执行中的业务事务。"""
        session = SessionLocal()
        try:
            operation = SystemOperationCRUD(session).get_of(BackgroundOperation, operation_id)
            if operation is None:
                return
            if phase is not None:
                operation.phase = phase
            commit_transaction(session)
        finally:
            session.close()

    def _run_in_background(self, operation_id: int, runner: Callable[[Session, CommandHandle], dict[str, Any]]) -> None:
        """后台线程入口。必须使用独立 Session，禁止复用已结束请求的 Session。"""
        session = SessionLocal()
        try:
            operation = SystemOperationCRUD(session).lock_for_run(operation_id)
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
            commit_transaction(session)
            try:
                result = runner(session, command)
            except BusinessError as exc:
                rollback_transaction(session)
                self._finish(session, operation_id, "FAILED", None, exc.code, exc.message)
                logger.warning(
                    "后台操作失败 operation_no=%s code=%s message=%s details=%s",
                    operation.operation_no, exc.code, exc.message, safe_error_details(exc.details),
                )
            except Exception as exc:  # noqa: BLE001 - 未处理异常必须显式落库，不能静默
                rollback_transaction(session)
                # 摘要带上异常类型与出错位置：只写「请查看服务日志」时，日志一旦丢失，
                # 这次失败就再也查不出原因。完整堆栈仍由 logger.exception 记录。
                self._finish(
                    session,
                    operation_id,
                    "FAILED",
                    None,
                    "OPERATION_UNEXPECTED_ERROR",
                    f"后台操作发生未处理异常：{exception_summary(exc)}",
                )
                logger.exception("后台操作发生未处理异常 operation_id=%s", operation_id, exc_info=exc)
            else:
                self._finish(session, operation_id, "SUCCEEDED", result, None, None)
        finally:
            session.close()

    def _finish(self, session: Session,
        operation_id: int,
        status: str,
        result: dict[str, Any] | None,
        failure_code: str | None,
        failure_message: str | None,
    ) -> None:
        operation = SystemOperationCRUD(session).lock_by_id(operation_id)
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
        commit_transaction(session)

    def recover_interrupted_operations(self) -> dict[str, int]:
        """启动时把上次遗留的活跃操作标记为中断。

        等待中的任务不是遗留执行：只有 background_operation 里仍处于
        PENDING/RUNNING 的记录才会被标记，业务等待点不会被误判。
        已推进到 BUSINESS_COMMITTED 的命令保留 result_json 与 phase：它的本地业务结果已经
        提交，重启后只能按同一命令补齐图推进，不能被当成“什么都没做”重跑。
        """
        stale = SystemOperationCRUD(self.db).active_all(ACTIVE_STATUSES)
        for operation in stale:
            operation.status = "INTERRUPTED"
            operation.failure_code = operation.failure_code or "OPERATION_INTERRUPTED"
            if operation.phase == PHASE_BUSINESS_COMMITTED:
                operation.failure_message = "服务重启导致图推进中断；该命令的本地业务结果已提交，请按同一命令补齐图推进"
            else:
                operation.failure_message = "服务重启导致该操作中断，请重新发起"
            operation.finished_at = datetime.now(UTC)
        SystemOperationCRUD(self.db).flush()
        return {"interrupted_operations": len(stale)}


    def dispatch(self, operation_id: int, operation_no: str, runner: Callable[[Session, CommandHandle], dict[str, Any]]) -> None:
        """受理事务提交后启动后台线程。"""
        try:
            thread = threading.Thread(
                target=self._run_in_background, args=(operation_id, runner),
                name=f"baoli-op-{operation_no}", daemon=True,
            )
            thread.start()
        except RuntimeError as exc:
            # 受理记录已经提交；调度失败用独立工作单元保存，明确向请求返回失败。
            with SessionLocal() as session:
                self._finish(
                    session, operation_id, "FAILED", None,
                    "OPERATION_DISPATCH_FAILED", "后台执行线程启动失败",
                )
            raise BusinessError("OPERATION_DISPATCH_FAILED", "后台执行线程启动失败", status_code=500) from exc
