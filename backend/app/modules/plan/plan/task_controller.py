"""从方案创建审核任务的接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.plan.plan.dto import TaskFromPlanRequest
from app.modules.plan.plan.schema import TaskCreatedResponse
from app.modules.plan.plan.service import tasks as plan_task_service

router = APIRouter(prefix="/plans", tags=["审核方案"])


@router.post("/{plan_code}/tasks", response_model=ResponseSchema[TaskCreatedResponse], status_code=201)
def create_task_from_plan_endpoint(
    plan_code: str,
    payload: TaskFromPlanRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[TaskCreatedResponse]:
    """使用方案创建审核任务。

    幂等：同一幂等标识 + 同一请求内容的重发返回原任务；内容不同则明确冲突。
    方案必须启用、范围已生效、场景必须未删除且启用；不满足时明确说明原因。

    请求携带 `expected_scope_hash`（页面预览看到的范围指纹）：与锁定后的实际指纹
    不一致时返回 `PLAN_SCOPE_CHANGED`，任务必须冻结用户刚刚确认的那一版范围。

    命令占位与任务在首次提交时一起落库；等待点确认后追加成功操作日志。
    """
    result = plan_task_service.PlanTasksService(db).create_from_plan( plan_code, payload.model_dump())
    return ok(TaskCreatedResponse.model_validate(result), request)
