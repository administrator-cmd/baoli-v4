"""字段纠错与字段核对接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.audit.execution.service.commands import AuditExecutionCommandsService
from app.modules.audit.execution.service.operations import AuditExecutionOperationsService
from app.modules.audit.field.dto import FieldCorrectionRequest, FieldReviewConfirmRequest, FieldReviewSaveRequest
from app.modules.audit.field.schema import FieldCorrectionResponse, FieldReviewSaveResponse
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.system.operation.schema import OperationAcceptedResponse
from app.modules.system.operation.service.background import OperationBackgroundService

router = APIRouter(prefix="/audit-tasks", tags=["审核任务"])


@router.post("/{task_no}/field-corrections", response_model=ResponseSchema[FieldCorrectionResponse])
def correct_field(
    task_no: str,
    payload: FieldCorrectionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[FieldCorrectionResponse]:
    """对错值/漏值字段发起主动纠错；生成新的字段事实版本并失效下游结果。"""
    result = AuditExecutionCommandsService(db).correct_field_value( task_no, payload)
    return ok(FieldCorrectionResponse.model_validate(result), request)


@router.post("/{task_no}/field-review/save", response_model=ResponseSchema[FieldReviewSaveResponse])
def save_field_review_endpoint(
    task_no: str,
    payload: FieldReviewSaveRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[FieldReviewSaveResponse]:
    """只保存等待核对期间的字段修改；任务仍停在等待核对，不触发后续规则。"""
    result = AuditExecutionCommandsService(db).save_field_review( task_no, payload)
    return ok(FieldReviewSaveResponse.model_validate(result), request)


@router.post("/{task_no}/field-review/confirm", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def confirm_field_review_endpoint(
    task_no: str,
    payload: FieldReviewConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResponseSchema[OperationAcceptedResponse]:
    """保存本次剩余修改并确认当前字段版本，随后在后台继续审核。

    返回 202 只代表操作已受理：字段修改与确认会在后台命令里先提交再做模型调用，
    真实结果用 operation_no 查询，或刷新任务详情查看。
    """
    task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
    operation, reused = AuditExecutionOperationsService(db).submit_field_review_confirm( task, payload)
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=reused)), request)
