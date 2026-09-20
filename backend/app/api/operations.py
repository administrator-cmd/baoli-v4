from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import OperationAcceptedResponse
from ..services.background_service import operation_by_no, operation_view


router = APIRouter(prefix="/operations", tags=["后台操作"])


@router.get("/{operation_no}", response_model=OperationAcceptedResponse)
def get_operation(operation_no: str, request: Request, db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    """查询后台长操作的真实状态。

    返回的是受理与执行状态；只有 status=SUCCEEDED 才代表业务动作完成，
    result 里保存的是该动作的真实结果。
    """
    operation = operation_by_no(db, operation_no)
    return OperationAcceptedResponse.model_validate({**operation_view(operation), "request_id": request.state.request_id})
