"""审核任务列表、详情、复制与维护接口。"""


from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.tasks import TaskCopyRequest, TaskUpdateRequest
from ...schema.query.pagination import validate_page_params
from ...schema.query.timestamps import FilterDateTime
from ...schema.vo.common import PagedResponse
from ...schema.vo.tasks import AuditTaskResponse
from ...services.audit.tasks import copying as task_command_service
from ...services.audit.tasks import lifecycle as tasks_lifecycle
from ...services.audit.tasks import query as tasks_query
from ...services.audit.tasks.detail import task_view
from ...services.audit.tasks.lookup import task_or_404

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("", response_model=PagedResponse)
def list_tasks(
    request: Request,
    name: str | None = None,
    scenario_code: str | None = None,
    plan_code: str | None = None,
    status: str | None = None,
    current_node: str | None = None,
    created_from: FilterDateTime | None = None,
    created_to: FilterDateTime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核任务分页列表。"""
    page, page_size = validate_page_params(page, page_size)
    result = tasks_query.list_tasks_page(
        db,
        name=name,
        scenario_code=scenario_code,
        plan_code=plan_code,
        status=status,
        current_node=current_node,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/options")
def task_options(request: Request, keyword: str | None = None, db: Session = Depends(get_db)) -> dict:
    """任务下拉选项：按名称或编号模糊匹配，最多 50 条，不含已删除任务。"""
    return {"request_id": request.state.request_id, "items": tasks_query.list_task_options(db, keyword=keyword)}


@router.get("/{task_no}", response_model=AuditTaskResponse)
def get_task(task_no: str, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """任务详情：当前节点、材料位、字段、规则、待办与最近一次后台操作。"""
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, task_no, values=True)), "request_id": request.state.request_id})


@router.patch("/{task_no}", response_model=AuditTaskResponse)
def update_task(task_no: str, payload: TaskUpdateRequest, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """编辑任务基本资料（名称、备注）；执行快照不随重命名与方案升级变化。"""
    tasks_lifecycle.update_task(db, task_no, payload.model_dump(exclude_unset=True))
    # 名称修改已提交；结束服务末尾 refresh 建立的只读快照，再读取同版数据库与 CSV。
    db.rollback()
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, task_no, values=True)), "request_id": request.state.request_id})


@router.post("/{task_no}/copy", response_model=AuditTaskResponse, status_code=201)
def copy_task_endpoint(task_no: str, payload: TaskCopyRequest, request: Request, db: Session = Depends(get_db)) -> AuditTaskResponse:
    """按源任务所属方案的当前范围重新创建任务。

    明确不是「复制原任务的执行快照」：新任务冻结的是方案此刻的范围。没有方案或
    方案、场景已失效时拒绝。同一幂等标识 + 同一请求内容的重发返回原任务。
    命令占位与任务在首次提交时一起落库；等待点确认后追加成功操作日志。
    """
    copied = task_command_service.copy_audit_task(
        db,
        task_no,
        task_name=payload.task_name,
        idempotency_key=payload.idempotency_key,
    )
    return AuditTaskResponse.model_validate({**task_view(db, task_or_404(db, copied.task_no, values=True)), "request_id": request.state.request_id})


@router.delete("/{task_no}", status_code=204, response_class=Response)
def delete_task(task_no: str, db: Session = Depends(get_db)) -> Response:
    """永久删除任务及关联记录、文件；执行中的任务拒绝删除。"""
    tasks_lifecycle.delete_task(db, task_no)
    return Response(status_code=204)
