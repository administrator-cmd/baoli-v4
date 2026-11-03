"""业务操作日志查询接口。"""


from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import OperationLogPageResponse
from app.core.database import get_db
from app.modules.system.operation.service import operation_log as operation_log_service
from app.schema.query.lists import OperationLogListQueryParam
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/operations", response_model=ResponseSchema[OperationLogPageResponse])
def list_operations(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[OperationLogListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[OperationLogPageResponse]:
    """操作日志：业务写操作与后台命令并列展示，各自的编号独立可查。

    - 业务操作日志只记录已成功提交的写操作；失败尝试不写入，不把“尝试”显示成“已发生”。
    - 后台命令的 PENDING/RUNNING 显示为“执行中”，不等同成功也不等同失败。
    """
    payload = operation_log_service.OperationLogService(db).list_operations_page(
        
        object_type=search.object_type,
        operation_type=search.operation_type,
        result=search.result,
        keyword=search.keyword,
        created_from=search.created_from,
        created_to=search.created_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(OperationLogPageResponse.model_validate(payload), request)
