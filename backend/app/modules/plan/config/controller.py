"""方案配置接口；任务反向生成方案的入口明确拒绝。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.plan.config.dto import PlanConfigUpdateRequest
from app.modules.plan.config.schema import PlanConfigResponse
from app.modules.plan.config.service.configuration import PlanConfigConfigurationService
from app.modules.plan.config.service.views import PlanConfigViewsService
from app.modules.plan.plan.dto import PlanFromTaskRequest
from app.modules.plan.plan.schema import PlanSelectionResponse
from app.modules.plan.plan.service.lifecycle import PlanLifecycleService
from app.modules.plan.plan.service.lookup import PlanLookupService
from app.modules.plan.plan.service.query import PlanQueryService

router = APIRouter(prefix="/plans", tags=["审核方案"])


@router.get("/{plan_code}/config", response_model=ResponseSchema[PlanConfigResponse])
def get_plan_config(plan_code: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[PlanConfigResponse]:
    """方案的可编辑配置：材料 / 字段 / 审核规则 / 报告章节 / 完整计算定义。

    方案页各页签的数据源。节点与选用 Sheet 一并返回，但它们是场景级执行结构，
    方案里只读。
    """
    plan = PlanLookupService(db).plan_or_404( plan_code)
    return ok(PlanConfigResponse.model_validate(PlanConfigViewsService(db).plan_config_view( plan)), request)


@router.put("/{plan_code}/config", response_model=ResponseSchema[PlanConfigResponse])
def put_plan_config(
    plan_code: str, payload: PlanConfigUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[PlanConfigResponse]:
    """整体替换方案的配置并立即重新编译。

    提交的是五份完整集合，因此「这次提交之后方案配了什么」不由历史操作顺序决定。
    编译不通过时保存照常成功，原因写在方案的 sync_error 里（页面显示「待核对」）。
    """
    result = PlanConfigConfigurationService(db).update_plan_config( plan_code, payload.model_dump())
    return ok(PlanConfigResponse.model_validate(result), request)


@router.post("/from-task", response_model=ResponseSchema[PlanSelectionResponse], status_code=201)
def plan_from_task(payload: PlanFromTaskRequest, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[PlanSelectionResponse]:
    """拒绝用任务反向生成方案。"""
    plan = PlanLifecycleService().save_task_range_as_plan(payload.model_dump())
    return ok(PlanSelectionResponse.model_validate(PlanQueryService(db).plan_selection_view(plan)), request)
