"""方案配置与定制差异接口；任务反向生成方案的入口明确拒绝。"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.dto.plans import PlanConfigUpdateRequest, PlanFromTaskRequest
from ...schema.vo.plans import PlanSelectionResponse
from ...services.plan.adjustments import plan_adjustments_view
from ...services.plan.configuration import update_plan_config
from ...services.plan.lifecycle import save_task_range_as_plan
from ...services.plan.lookup import plan_or_404
from ...services.plan.query import plan_selection_view
from ...services.plan.views import plan_config_view

router = APIRouter(prefix="/plans", tags=["审核方案"])


def _selection_response(request: Request, payload: dict[str, Any]) -> PlanSelectionResponse:
    return PlanSelectionResponse.model_validate({**payload, "request_id": request.state.request_id})


@router.get("/{plan_code}/config")
def get_plan_config(plan_code: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """方案的可编辑配置：材料 / 字段 / 审核规则 / 报告章节 / 完整计算定义。

    方案页各页签的数据源。节点与选用 Sheet 一并返回，但它们是场景级执行结构，
    方案里只读。
    """
    plan = plan_or_404(db, plan_code)
    return {**plan_config_view(db, plan), "request_id": request.state.request_id}


@router.get("/{plan_code}/adjustments")
def get_plan_adjustments(plan_code: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """旧方案相对其来源场景版本的调整，供重新生成方案时参考。"""
    plan = plan_or_404(db, plan_code)
    return {**plan_adjustments_view(db, plan), "request_id": request.state.request_id}


@router.put("/{plan_code}/config")
def put_plan_config(
    plan_code: str, payload: PlanConfigUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """整体替换方案的配置并立即重新编译。

    提交的是五份完整集合，因此「这次提交之后方案配了什么」不由历史操作顺序决定。
    编译不通过时保存照常成功，原因写在方案的 sync_error 里（页面显示「待核对」）。
    """
    result = update_plan_config(db, plan_code, payload.model_dump())
    return {**result, "request_id": request.state.request_id}


@router.post("/from-task", response_model=PlanSelectionResponse, status_code=201)
def plan_from_task(payload: PlanFromTaskRequest, request: Request, db: Session = Depends(get_db)) -> PlanSelectionResponse:
    """拒绝用任务反向生成方案。"""
    plan = save_task_range_as_plan(payload.model_dump())
    return _selection_response(request, plan_selection_view(db, plan))
