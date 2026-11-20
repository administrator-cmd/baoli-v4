"""后台操作进度查询接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.system.operation.schema import OperationDetailResponse
from app.modules.system.operation.service.operation_types import OBJECT_IMPORT_JOB
from app.modules.system.operation.service.background import OperationBackgroundService

router = APIRouter(prefix="/operations", tags=["后台操作"])


@router.get("/{operation_no}", response_model=ResponseSchema[OperationDetailResponse])
def get_operation(operation_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[OperationDetailResponse]:
    """查询后台长操作的真实状态。

    返回的是受理与执行状态；只有 status=SUCCEEDED 才代表业务动作完成，
    result 里保存的是该动作的真实结果。
    """
    service = OperationBackgroundService(db)
    operation = service.operation_by_no(operation_no)
    files = service.response_files(operation.id) if operation.object_type == OBJECT_IMPORT_JOB else []
    return ok(OperationDetailResponse(**service.operation_view(operation), response_files=files), request)
