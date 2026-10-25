"""业务操作日志查询接口。"""


from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.query.pagination import validate_page_params
from ...schema.query.timestamps import FilterDateTime
from ...schema.vo.common import OperationLogPageResponse
from ...services.system import operation_log as operation_log_service

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/operations", response_model=OperationLogPageResponse)
def list_operations(
    request: Request,
    object_type: str | None = None,
    operation_type: str | None = None,
    result: str | None = None,
    keyword: str | None = None,
    created_from: FilterDateTime | None = None,
    created_to: FilterDateTime | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> OperationLogPageResponse:
    """操作日志：业务写操作与后台命令并列展示，各自的编号独立可查。

    - 业务操作日志只记录已成功提交的写操作；失败尝试不写入，不把“尝试”显示成“已发生”。
    - 后台命令的 PENDING/RUNNING 显示为“执行中”，不等同成功也不等同失败。
    """
    page, page_size = validate_page_params(page, page_size)
    payload = operation_log_service.list_operations_page(
        db,
        object_type=object_type,
        operation_type=operation_type,
        result=result,
        keyword=keyword,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )
    return OperationLogPageResponse.model_validate({**payload, "request_id": request.state.request_id})
