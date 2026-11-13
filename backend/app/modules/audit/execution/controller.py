"""审核任务执行命令与执行记录接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import PagedResponse
from app.core.database import get_db
from app.modules.audit.execution.dto import ExecuteNodeRequest, ResumeExecutionRequest
from app.modules.audit.execution.schema import ExecutionRowResponse
from app.modules.audit.execution.service import executions as audit_executions
from app.modules.audit.execution.service.operations import AuditExecutionOperationsService
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.system.operation.schema import OperationAcceptedResponse
from app.modules.system.operation.service.background import OperationBackgroundService
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("/{task_no}/executions", response_model=ResponseSchema[PagedResponse[ExecutionRowResponse]])
def get_task_executions(
    task_no: str,
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse[ExecutionRowResponse]]:
    """该任务的节点执行记录分页。"""
    result = audit_executions.AuditExecutionExecutionsService(db).list_executions_page(
        
        task_no=task_no,
        node_code=None,
        status=None,
        started_from=None,
        started_to=None,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse[ExecutionRowResponse].model_validate(result), request)


@router.post("/{task_no}/execute", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def execute(task_no: str, payload: ExecuteNodeRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[OperationAcceptedResponse]:
    """提交当前节点执行或失败后的手动重试为后台操作。

    重试沿用任务材料和冻结配置，新幂等键创建新批次，同一键只重放原受理记录。
    返回 202 只代表操作已受理，不代表节点执行成功；真实结果用 operation_no
    查询 GET /api/operations/{operation_no}，或刷新任务详情查看。
    """
    task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
    operation, reused = AuditExecutionOperationsService(db).submit_node_execution(
        
        task,
        node_code=payload.node_code,
        idempotency_key=payload.idempotency_key,
        input_revision=payload.input_revision,
    )
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=reused)), request)


@router.post("/{task_no}/resume", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def resume(task_no: str, payload: ResumeExecutionRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[OperationAcceptedResponse]:
    """从未完成的执行批次继续，只重跑未完成规则和受影响的下游。

    与 ``/execute`` 的区别：这里复用原批次，不受影响的已提交结果不重跑。
    同一幂等键的重发返回原受理记录（含原目标批次），不因任务已进入执行或已完成而变成不可恢复；
    批次确实没有可继续项时由受理校验明确 409。
    """
    task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
    operation, reused = AuditExecutionOperationsService(db).submit_node_resume(
        
        task,
        idempotency_key=payload.idempotency_key,
        input_revision=payload.input_revision,
    )
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=reused)), request)
