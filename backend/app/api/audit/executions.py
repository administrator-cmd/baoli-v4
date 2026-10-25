"""审核任务执行命令与执行记录接口。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...core.database import get_db
from ...schema.dto.tasks import ExecuteNodeRequest, ResumeExecutionRequest
from ...schema.query.pagination import validate_page_params
from ...schema.vo.common import PagedResponse
from ...schema.vo.operations import OperationAcceptedResponse
from ...services.audit import executions as audit_executions
from ...services.audit import resume as audit_resume
from ...services.audit.operations import submit_node_execution, submit_node_resume
from ...services.audit.tasks.lookup import task_or_404
from ...services.system.background import operation_view

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("/{task_no}/executions", response_model=PagedResponse)
def get_task_executions(
    task_no: str,
    request: Request,
    page: int = Query(default=1),
    page_size: int = Query(default=50),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """该任务的节点执行记录分页（默认每页 50 条）。"""
    page, page_size = validate_page_params(page, page_size)
    result = audit_executions.list_executions_page(
        db,
        task_no=task_no,
        node_code=None,
        status=None,
        started_from=None,
        started_to=None,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.post("/{task_no}/execute", response_model=OperationAcceptedResponse, status_code=202)
def execute(task_no: str, payload: ExecuteNodeRequest, request: Request, db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    """提交当前节点执行或失败后的手动重试为后台操作。

    重试沿用任务材料和冻结配置，新幂等键创建新批次，同一键只重放原受理记录。
    返回 202 只代表操作已受理，不代表节点执行成功；真实结果用 operation_no
    查询 GET /api/operations/{operation_no}，或刷新任务详情查看。
    """
    task = task_or_404(db, task_no, lock=True)
    operation, reused = submit_node_execution(
        db,
        task,
        node_code=payload.node_code,
        idempotency_key=payload.idempotency_key,
        input_revision=payload.input_revision,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )


@router.post("/{task_no}/resume", response_model=OperationAcceptedResponse, status_code=202)
def resume(task_no: str, payload: ResumeExecutionRequest, request: Request, db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    """从未完成的执行批次继续，只重跑未完成规则及其下游。

    与 ``/execute`` 的区别：这里复用原批次，不受影响的已提交结果不重跑；
    批次没有可继续的未完成项时明确 409，并提示改用重新执行。
    """
    task = task_or_404(db, task_no, lock=True)
    execution = audit_resume.resumable_execution_of_task(db, task)
    if execution is None:
        raise BusinessError(
            "AUDIT_EXECUTION_NOT_RESUMABLE",
            "当前没有可继续的未完成批次，请使用「重新尝试」重新执行",
            status_code=409,
            details={"task_no": task_no, "status": task.status},
        )
    operation, reused = submit_node_resume(
        db,
        task,
        execution_id=execution.id,
        idempotency_key=payload.idempotency_key,
        input_revision=payload.input_revision,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )
