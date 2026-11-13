"""审核任务列表、详情、复制与维护接口。"""


from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import ItemsResponse, PagedResponse
from app.core.database import get_db
from app.modules.audit.task.dto import TaskCopyRequest, TaskUpdateRequest
from app.modules.audit.task.schema import AuditTaskResponse, TaskOptionItem, TaskRowResponse
from app.modules.audit.task.service import copying as task_command_service
from app.modules.audit.task.service import lifecycle as tasks_lifecycle
from app.modules.audit.task.service import query as tasks_query
from app.modules.audit.task.service.detail import AuditTaskDetailService
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.schema.query.lists import TaskListQueryParam
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.get("", response_model=ResponseSchema[PagedResponse[TaskRowResponse]])
def list_tasks(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[TaskListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse[TaskRowResponse]]:
    """审核任务分页列表。"""
    result = tasks_query.AuditTaskQueryService(db).list_tasks_page(

        name=search.name,
        scenario_code=search.scenario_code,
        plan_code=search.plan_code,
        status=search.status,
        current_node=search.current_node,
        created_from=search.created_from,
        created_to=search.created_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse[TaskRowResponse].model_validate(result), request)


@router.get("/options", response_model=ResponseSchema[ItemsResponse[TaskOptionItem]])
def task_options(request: Request, keyword: str | None = None, db: Session = Depends(get_db)) -> ResponseSchema[ItemsResponse[TaskOptionItem]]:
    """任务下拉选项：按名称或编号模糊匹配，最多 50 条，不含已删除任务。"""
    return ok(ItemsResponse[TaskOptionItem].model_validate({"items": tasks_query.AuditTaskQueryService(db).list_task_options( keyword=keyword)}), request)


@router.get("/{task_no}", response_model=ResponseSchema[AuditTaskResponse])
def get_task(task_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[AuditTaskResponse]:
    """任务详情：当前节点、材料位、字段、规则、待办与最近一次后台操作。"""
    return ok(AuditTaskResponse.model_validate(AuditTaskDetailService(db).task_view( AuditTaskLookupService(db).task_or_404( task_no, values=True))), request)


@router.patch("/{task_no}", response_model=ResponseSchema[AuditTaskResponse])
def update_task(task_no: str, payload: TaskUpdateRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[AuditTaskResponse]:
    """编辑任务基本资料（名称、备注）；执行快照不随重命名与方案升级变化。"""
    tasks_lifecycle.AuditTaskLifecycleService(db).update_task( task_no, payload.model_dump(exclude_unset=True))
    return ok(AuditTaskResponse.model_validate(AuditTaskDetailService(db).task_view( AuditTaskLookupService(db).task_or_404( task_no, values=True))), request)


@router.post("/{task_no}/copy", response_model=ResponseSchema[AuditTaskResponse], status_code=201)
def copy_task_endpoint(task_no: str, payload: TaskCopyRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[AuditTaskResponse]:
    """按源任务所属方案的当前范围重新创建任务。

    明确不是「复制原任务的执行快照」：新任务冻结的是方案此刻的范围。没有方案或
    方案、场景已失效时拒绝。同一幂等标识 + 同一请求内容的重发返回原任务。
    任务、命令成功状态与操作日志在同一次提交落库。
    """
    copied = task_command_service.AuditTaskCopyingService(db).copy_audit_task(

        task_no,
        task_name=payload.task_name,
        idempotency_key=payload.idempotency_key,
    )
    return ok(AuditTaskResponse.model_validate(AuditTaskDetailService(db).task_view( AuditTaskLookupService(db).task_or_404( copied.task_no, values=True))), request)


@router.delete("/{task_no}", status_code=204, response_class=Response)
def delete_task(task_no: str, db: Session = Depends(get_db)) -> Response:
    """永久删除任务及关联记录、文件；执行中的任务拒绝删除。"""
    tasks_lifecycle.AuditTaskLifecycleService(db).delete_task( task_no)
    return Response(status_code=204)
