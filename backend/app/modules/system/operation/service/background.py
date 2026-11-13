"""后台长操作：先认领记录，再用独立 Session 执行。

请求只返回受理状态（202 语义），真正的结果写回 background_operation。
同一对象的执行、恢复、重跑、改绑定、裁决、替换文件共用同一套互斥规则：
只要该对象存在活跃操作（PENDING/RUNNING），新的不同请求明确返回冲突；
相同幂等标识的重复请求返回已有记录，不重复执行。

后台执行失败不会静默：状态写 FAILED 并保留错误码与中文原因。"""

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import ACTIVE_OPERATION_STATUSES
from app.domain.shared.identity import public_no
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.operation_log import OperationLogService

logger = logging.getLogger(__name__)


ACTIVE_STATUSES = tuple(sorted(ACTIVE_OPERATION_STATUSES))


PHASE_COMMAND_ACCEPTED = "COMMAND_ACCEPTED"


PHASE_LLM_REQUEST = "LLM_REQUEST"


PHASE_BUSINESS_COMMITTED = "BUSINESS_COMMITTED"


PHASE_GRAPH_ADVANCED = "GRAPH_ADVANCED"


PHASE_INTERRUPT_WAITING = "INTERRUPT_WAITING"


# 字段核对等待点：本次后台操作已正常完成它要做的事，任务停在等待人工核对。
PHASE_FIELD_REVIEW_WAITING = "FIELD_REVIEW_WAITING"


PHASE_RESULT_READY = "RESULT_READY"


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
            from app.modules.system.operation.execution import OperationExecution
            OperationExecution()._update_operation(self.operation_id, phase=phase)


class OperationBackgroundService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _request_diff(self, existing: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
        keys = sorted(set(existing) | set(incoming))
        return [key for key in keys if existing.get(key) != incoming.get(key)]


    def _active_operation(self, object_type: str, object_no: str) -> BackgroundOperation | None:
        return SystemOperationCRUD(self.db).active_for_object(
            object_type=object_type, object_no=object_no, statuses=ACTIVE_STATUSES,
        )


    def assert_no_active_operation(self, object_type: str, object_no: str) -> None:
        """改绑定、裁决、替换文件等入口共用同一互斥规则。

        这些入口本身不必异步，但必须与后台执行互斥，避免在节点正在执行时改动输入。
        """
        active = self._active_operation( object_type, object_no)
        if active is not None:
            raise BusinessError(
                "OPERATION_ALREADY_ACTIVE",
                "该对象已有正在进行的操作，请等待完成或查询其状态",
                status_code=409,
                details={"operation_no": active.operation_no, "status": active.status},
            )


    def operation_by_no(self, operation_no: str) -> BackgroundOperation:
        """按操作编号取后台命令；取不到即 404。"""
        operation = SystemOperationCRUD(self.db).by_operation_no(operation_no)
        if not operation:
            raise BusinessError(
                "OPERATION_NOT_FOUND",
                "后台操作不存在",
                status_code=404,
                details={"operation_no": operation_no},
            )
        return operation


    def latest_operation(self, object_type: str, object_no: str) -> BackgroundOperation | None:
        """该对象最近一次后台命令，页面刷新后凭它继续查询真实状态。"""
        return SystemOperationCRUD(self.db).latest_for_object(object_type=object_type, object_no=object_no)


    def operation_view(self, operation: BackgroundOperation, *, reused: bool = False) -> dict[str, Any]:
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
            "message": self._status_message(operation),
            "reused": reused,
            "result": operation.result_json,
            "raw_response_path": operation.raw_response_path,
            "failure_code": operation.failure_code,
            "failure_message": operation.failure_message,
            "created_at": operation.created_at,
            "started_at": operation.started_at,
            "finished_at": operation.finished_at,
        }


    def _status_message(self, operation: BackgroundOperation) -> str:
        if operation.status == "SUCCEEDED" and operation.phase == PHASE_FIELD_REVIEW_WAITING:
            # 本次命令要求的动作已经完成，但整批审核没有结束：必须如实说明停在等待点。
            return "字段抽取已完成，等待人工核对后继续"
        return {
            "PENDING": "操作已受理，等待后台执行",
            "RUNNING": "操作已受理，正在后台执行",
            "SUCCEEDED": "操作已完成",
            "FAILED": "操作失败",
            "INTERRUPTED": "操作被中断，可重新发起",
        }.get(operation.status, "状态未知")


    def existing_operation(self, *, object_type: str, object_no: str, operation_type: str, idempotency_key: str,
    ) -> BackgroundOperation | None:
        """同一对象、同一幂等键已受理的后台操作。

        响应丢失后的重发按它取回原记录：请求内容与目标对象都以受理时为准，
        不按当前业务状态重新推导。
        """
        return SystemOperationCRUD(self.db).background_by_operation_key(
            object_type=object_type, object_no=object_no,
            operation_type=operation_type, idempotency_key=idempotency_key,
        )


    def submit_operation(self, *,
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
        request_hash = OperationLogService().normalized_request_hash(request)
        same_request = self.existing_operation(
             object_type=object_type, object_no=object_no,
            operation_type=operation_type, idempotency_key=idempotency_key,
        )
        if same_request is not None:
            # 历史命令优先于状态校验：成功但响应丢失后的重发必须复用原命令，不能被误报为状态错误。
            stored_hash = same_request.request_hash or OperationLogService().normalized_request_hash(dict(same_request.request_json or {}))
            if stored_hash == request_hash:
                return same_request, True
            raise BusinessError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "同一幂等标识已用于不同的请求内容，拒绝复用已有操作",
                status_code=409,
                details={
                    "operation_no": same_request.operation_no,
                    "diff_keys": self._request_diff(dict(same_request.request_json or {}), request),
                },
            )
        active = self._active_operation( object_type, object_no)
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
        SystemOperationCRUD(self.db).add(operation)
        SystemOperationCRUD(self.db).flush()
        operation_id = operation.id
        from app.core.transactions import after_commit
        from app.modules.system.operation.execution import OperationExecution

        operation_no = operation.operation_no
        after_commit(self.db, lambda: OperationExecution().dispatch(operation_id, operation_no, runner))
        return operation, False
