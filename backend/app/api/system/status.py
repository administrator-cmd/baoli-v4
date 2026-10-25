"""系统状态与外部服务配置接口。"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ...core.config import get_settings
from ...schema.vo.system import SystemServiceItem, SystemServicesResponse, SystemStatusResponse
from ...services.system import status as system_status_service

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/status", response_model=SystemStatusResponse)
def system_status(request: Request) -> SystemStatusResponse:
    """系统总体状态：数据库为本次真实探测结果，外部服务只报告配置状态与“未验证”。"""
    return SystemStatusResponse.model_validate(
        {**system_status_service.status_view(), "request_id": request.state.request_id}
    )


SERVICE_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("大模型", "规则转换、字段抽取与语义判断"),
    ("材料识别", "材料类型识别与校验（8018）"),
    ("文件解析", "文件处理与版面解析（8010）"),
    ("文字识别", "扫描件文字识别（OCR）"),
    ("印章识别", "印章检测"),
)


def _service_rows(configuration: dict[str, dict]) -> list[SystemServiceItem]:
    rows: list[SystemServiceItem] = []
    for name, capability in SERVICE_CAPABILITIES:
        status = configuration[name]
        if status["status"] == "CONFIGURED_UNVERIFIED":
            connection = "UNVERIFIED"
            note = "必需配置项已填写，未做真实连通验证；执行相关操作时若失败会返回明确错误"
        else:
            connection = "NOT_CONFIGURED"
            note = "缺少必需配置项，相关操作会明确返回未配置错误，不会静默降级"
        rows.append(
            SystemServiceItem(
                name=name,
                capability=capability,
                configuration_status=status["status"],
                connection_status=connection,
                last_checked_at=None,
                missing=status["missing"],
                note=note,
            )
        )
    return rows


@router.get("/services", response_model=SystemServicesResponse)
def system_services(request: Request) -> SystemServicesResponse:
    """服务状态：配置状态与连接状态分开，不伪造“服务就绪”。

    数据库连接是本请求真实探测的结果；外部服务只报告配置状态与“未验证”，
    不在状态接口里代替业务操作做供应商探测。
    """
    settings = get_settings()
    status = system_status_service.status_view()
    database_status = status["database"]
    services = _service_rows(status["external_services"])
    services.insert(
        0,
        SystemServiceItem(
            name="数据库",
            capability="业务数据与流程检查点存储（baoli_v4）",
            configuration_status="CONFIGURED" if settings.database_url else "UNCONFIGURED",
            connection_status=database_status,
            last_checked_at=datetime.now(UTC) if database_status == "CONNECTED" else None,
            missing=[] if settings.database_url else ["BAOLI_DATABASE_URL"],
            note=None if database_status == "CONNECTED" else "本次探测未连上数据库，请检查连接配置、网络与账号权限",
        ),
    )
    return SystemServicesResponse(
        request_id=request.state.request_id,
        database=database_status,
        services=services,
    )
