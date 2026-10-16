from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..domain import audit_vocab, tool_catalog
from ..schema.query import validate_page_params
from ..schema.vo import (
    MaterialTypeCatalogResponse,
    MaterialTypeItem,
    OperationLogPageResponse,
    RuleCapabilityStatusResponse,
    RuleStatusCatalogResponse,
    SystemServiceItem,
    SystemServicesResponse,
    SystemStatusResponse,
    ToolCatalogItem,
    UploadKindPolicy,
    UploadPolicyResponse,
    ToolCatalogResponse,
)
from ..integrations.http_services import list_material_types
from ..services import operation_log_service, system_status_service
from ..services.storage_service import DOCUMENT_EXTENSIONS, RULEBOOK_EXTENSIONS


router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/status", response_model=SystemStatusResponse)
def system_status(request: Request) -> SystemStatusResponse:
    """系统总体状态：数据库为本次真实探测结果，外部服务只报告配置状态与“未验证”。"""
    return SystemStatusResponse.model_validate(
        {**system_status_service.status_view(), "request_id": request.state.request_id}
    )


@router.get("/upload-policy", response_model=UploadPolicyResponse)
def upload_policy(request: Request) -> UploadPolicyResponse:
    """上传策略：规则库与业务材料各自的大小上限与允许扩展名。

    这是上传限制的唯一真相源，前端从这里读取并展示，不再复制常量——复制的那一份
    会在配置变更后与后端实际校验不一致。实际写入时的最终校验仍在
    storage_service.save_upload，超限显式抛 FILE_TOO_LARGE。

    该上限只覆盖本系统上传入口。8018 / 8010 / OCR 与部署入口各自的体量上限
    是独立的外部契约，本接口不声明、也未经验证。
    """
    settings = get_settings()
    return UploadPolicyResponse(
        rulebook=UploadKindPolicy(
            max_bytes=settings.max_rulebook_bytes,
            allowed_extensions=sorted(RULEBOOK_EXTENSIONS),
        ),
        document=UploadKindPolicy(
            max_bytes=settings.max_document_bytes,
            allowed_extensions=sorted(DOCUMENT_EXTENSIONS),
        ),
        scope_note=(
            "以上上限只约束本系统的上传入口，不表示 8018、8010、OCR 或部署入口支持同样的体量；"
            "这些外部上限未经验证，超出时会在对应环节返回明确错误。"
        ),
        request_id=request.state.request_id,
    )


@router.get("/material-types", response_model=MaterialTypeCatalogResponse)
def material_types(request: Request) -> MaterialTypeCatalogResponse:
    """8018 支持的材料识别类型目录。

    材料上传页的文件类型下拉从这里取值，不能自由输入：8018 的 doc_type 是严格枚举，
    自由输入的值会在服务端被直接拒绝。上传入口与材料检查节点前端交给 8018 的
    `doc_type` 都取自这里。

    目录原样转发自 8018，本系统不做缓存、不内置默认清单，也不把 8018 的取值与
    业务材料名称做映射（例如不把「结算协议」认作「结算报告」）。服务未配置或不可用时
    明确报错，不返回一份编造的目录。
    """
    items = list_material_types()
    return MaterialTypeCatalogResponse(
        items=[MaterialTypeItem(id=item["id"], name=item["name"]) for item in items],
        request_id=request.state.request_id,
    )


@router.get("/tools", response_model=ToolCatalogResponse)
def tools_catalog(request: Request) -> ToolCatalogResponse:
    """供模型按需选择的真实工具、参数和实现信息；不决定规则执行路径。"""
    return ToolCatalogResponse(
        tools=[ToolCatalogItem.model_validate(item) for item in tool_catalog.catalog_payload()],
        request_id=request.state.request_id,
    )


@router.get("/rule-statuses", response_model=RuleStatusCatalogResponse)
def rule_status_catalog(request: Request) -> RuleStatusCatalogResponse:
    """审核结论状态目录：每个状态码的标准中文含义与分档。

    页面用它区分「已判定 / 已完成产出 / 尚未判定 / 不适用」，未知码单独显示。
    分档之外的状态不进入任何分档：页面必须把原始码单独计数并显示出来，
    不能静默消失，也不能按兜底文案显示成正常状态。
    """
    return RuleStatusCatalogResponse(
        statuses=[
            RuleCapabilityStatusResponse(**audit_vocab.status_view(status))
            for status in audit_vocab.RULE_STATUSES
        ],
        categories=[
            {"category": category, "label": label}
            for category, label in audit_vocab.STATUS_CATEGORY_LABELS.items()
        ],
        request_id=request.state.request_id,
    )


# 服务能力说明：只描述系统真实使用的能力，不承诺供应商未提供的字段。
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


@router.get("/operations", response_model=OperationLogPageResponse)
def list_operations(
    request: Request,
    object_type: str | None = None,
    operation_type: str | None = None,
    result: str | None = None,
    keyword: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
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
