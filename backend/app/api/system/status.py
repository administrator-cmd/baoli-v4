"""系统状态与大模型探测接口。"""

from fastapi import APIRouter, Request

from ...schema.vo.system import SystemServiceItem, SystemServicesResponse, SystemStatusResponse
from ...services.system import status as system_status_service

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/status", response_model=SystemStatusResponse)
def system_status(request: Request) -> SystemStatusResponse:
    """系统总体状态：数据库与大模型的连接结论都来自同一份真实探测。"""
    return SystemStatusResponse.model_validate(
        {**system_status_service.status_view(), "request_id": request.state.request_id}
    )


@router.get("/services", response_model=SystemServicesResponse)
def system_services(request: Request, refresh: bool = False) -> SystemServicesResponse:
    """服务状态：数据库与大模型各执行一次真实探测。

    配置缺失时不发请求，明确返回未配置；探测失败保留错误原因与耗时。
    探测结论在短窗口内复用，`refresh=true` 忽略该窗口立即重测。
    材料识别、文件解析、文字识别与印章识别没有独立的只读探测入口，不在状态页列出，
    它们的失败由实际业务操作显式返回。
    """
    items = system_status_service.service_items(refresh=refresh)
    database = next(item for item in items if item.name == "数据库")
    return SystemServicesResponse(
        request_id=request.state.request_id,
        database=database.connection_status,
        services=[SystemServiceItem.model_validate(item.as_service_item()) for item in items],
    )
