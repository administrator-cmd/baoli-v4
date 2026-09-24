from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import engine, get_db
from ..domain import capabilities, tool_catalog
from ..errors import BusinessError
from ..schemas import (
    CapabilityCatalogResponse,
    CapabilityItem,
    ExternalServiceConfiguration,
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
)
from ..integrations.http_services import list_material_types
from ..services import operation_log_service
from ..services.embedding_service import embedding_status
from ..services.storage_service import DOCUMENT_EXTENSIONS, RULEBOOK_EXTENSIONS


router = APIRouter(prefix="/system", tags=["系统"])


def _database_status() -> str:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "CONNECTED"
    except SQLAlchemyError:
        return "FAILED"


def _configuration_status(required: dict[str, bool]) -> ExternalServiceConfiguration:
    missing = [name for name, configured in required.items() if not configured]
    if len(missing) == len(required):
        status = "UNCONFIGURED"
    elif missing:
        status = "CONFIGURATION_INCOMPLETE"
    else:
        status = "CONFIGURED_UNVERIFIED"
    return ExternalServiceConfiguration(status=status, missing=missing)


@router.get("/status", response_model=SystemStatusResponse)
def system_status(request: Request) -> SystemStatusResponse:
    """系统总体状态：数据库为本次真实探测结果，外部服务只报告配置状态与“未验证”。"""
    database_status = _database_status()
    settings = get_settings()
    return SystemStatusResponse(
        service="RUNNING" if database_status == "CONNECTED" else "DEGRADED",
        database=database_status,
        external_services={
            "大模型": _configuration_status(
                {
                    "BAOLI_LLM_BASEURL": bool(settings.llm_baseurl),
                    "BAOLI_LLM_APIKEY": bool(settings.llm_api_key_value),
                    "BAOLI_LLM_MODEL_NAME": bool(settings.llm_model_name),
                }
            ),
            "材料识别": _configuration_status({"BAOLI_SERVICE_8018_URL": bool(settings.service_8018_url)}),
            "文件解析": _configuration_status({"BAOLI_SERVICE_8010_URL": bool(settings.service_8010_url)}),
            "文字识别": _configuration_status(
                {
                    "BAOLI_OCR_SERVICE_URL": bool(settings.ocr_service_url),
                    "BAOLI_OCR_TOKEN": bool(settings.ocr_token_value),
                    "BAOLI_OCR_MODEL": bool(settings.ocr_model),
                }
            ),
            "印章识别": _configuration_status({"BAOLI_SEAL_SERVICE_URL": bool(settings.seal_service_url)}),
        },
        request_id=request.state.request_id,
    )


@router.get("/upload-policy", response_model=UploadPolicyResponse)
def upload_policy(request: Request) -> UploadPolicyResponse:
    """上传策略：规则库与业务材料各自的大小上限与允许扩展名。

    这是上传限制的唯一真相源，前端从这里读取并展示，不再复制常量——复制的那一份
    会在配置变更后与后端实际校验不一致。实际写入时的最终校验仍在
    storage_service.save_upload，超限显式抛 FILE_TOO_LARGE。

    该上限只覆盖**本系统上传入口**。8018 / 8010 / OCR 与部署入口各自的体量上限
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


@router.get("/capabilities", response_model=CapabilityCatalogResponse)
def capability_catalog(request: Request) -> CapabilityCatalogResponse:
    """审核能力目录：后端能力的单一事实来源。

    每项能力声明：能力标识、业务标签、可挂载节点、输入形态、参数契约（必填/可选与口径）、
    结果里可能出现的状态、是否支持证据与比较、输出键、取证与判定方式、扩展数据命名空间、
    版本，以及**当前是否已实现**。

    页面从这里取检查方式下拉与参数口径，不再在本地维护第二份名单——两份名单漂移过一次
    （后端 12 个算子、页面只有 7 个），结果是页面上打开一条已导入的规则，
    下拉框里找不到它当前的检查方式。

    `implemented=false` 的能力仍然可以被配置，但发布前必须逐条确认，
    运行时明确返回「不可判定」：不默认通过，也不伪装成已执行。

    `tools` 是同一份目录的工具视图（`domain/tool_catalog.py` 从能力目录派生）：
    模型可调用的工具、参数 Schema、权限范围与真实错误码。只有 `implemented=true`
    的工具才会进模型请求的 `tools`；不可调用的工具仍如实列出并写明原因，
    页面据此不把它们显示成可执行选项。
    """
    return CapabilityCatalogResponse(
        capability_version=capabilities.CAPABILITY_VERSION,
        rule_statuses=sorted(capabilities.RULE_STATUSES),
        items=[CapabilityItem.model_validate(item) for item in capabilities.catalog_payload()],
        tool_catalog_version=tool_catalog.TOOL_CATALOG_VERSION,
        tool_catalog_fingerprint=tool_catalog.catalog_fingerprint(),
        tools=[ToolCatalogItem.model_validate(item) for item in tool_catalog.catalog_payload()],
        request_id=request.state.request_id,
    )


@router.get("/rule-statuses", response_model=RuleStatusCatalogResponse)
def rule_status_catalog(request: Request) -> RuleStatusCatalogResponse:
    """审核结论状态目录：每个状态码的标准中文含义与分档。

    页面用它区分「已判定 / 尚未判定 / 不适用」，而不是把不认识的码显示成「未开始」。
    分档之外的状态**不进入任何分档**：页面必须把原始码单独计数并显示出来，
    不能静默消失，也不能按兜底文案显示成正常状态。
    """
    return RuleStatusCatalogResponse(
        statuses=[
            RuleCapabilityStatusResponse(**capabilities.status_view(status))
            for status in capabilities.RULE_STATUSES
        ],
        categories=[
            {"category": category, "label": label}
            for category, label in capabilities.STATUS_CATEGORY_LABELS.items()
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


def _service_rows(settings) -> list[SystemServiceItem]:
    configuration = {
        "大模型": {
            "BAOLI_LLM_BASEURL": bool(settings.llm_baseurl),
            "BAOLI_LLM_APIKEY": bool(settings.llm_api_key_value),
            "BAOLI_LLM_MODEL_NAME": bool(settings.llm_model_name),
        },
        "材料识别": {"BAOLI_SERVICE_8018_URL": bool(settings.service_8018_url)},
        "文件解析": {"BAOLI_SERVICE_8010_URL": bool(settings.service_8010_url)},
        "文字识别": {
            "BAOLI_OCR_SERVICE_URL": bool(settings.ocr_service_url),
            "BAOLI_OCR_TOKEN": bool(settings.ocr_token_value),
            "BAOLI_OCR_MODEL": bool(settings.ocr_model),
        },
        "印章识别": {"BAOLI_SEAL_SERVICE_URL": bool(settings.seal_service_url)},
    }
    rows: list[SystemServiceItem] = []
    for name, capability in SERVICE_CAPABILITIES:
        status = _configuration_status(configuration[name])
        if status.status == "CONFIGURED_UNVERIFIED":
            connection = "UNVERIFIED"
            note = "必需配置项已填写，未做真实连通验证；执行相关操作时若失败会返回明确错误"
        else:
            connection = "NOT_CONFIGURED"
            note = "缺少必需配置项，相关操作会明确返回未配置错误，不会静默降级"
        rows.append(
            SystemServiceItem(
                name=name,
                capability=capability,
                configuration_status=status.status,
                connection_status=connection,
                last_checked_at=None,
                missing=status.missing,
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
    database_status = _database_status()
    services = _service_rows(settings)
    # 本地嵌入模型是审核任务检索取证的基础。它只报告配置状态与“未验证”，
    # 不在状态接口里加载模型或做推理；缺模型时相关规则会明确报错而不是静默降级。
    embedding = embedding_status()
    services.append(
        SystemServiceItem(
            name="本地嵌入模型",
            capability="审核任务材料证据索引与检索取证",
            configuration_status=embedding["configuration_status"],
            connection_status="UNVERIFIED" if embedding["configuration_status"] == "CONFIGURED_UNVERIFIED" else "NOT_CONFIGURED",
            last_checked_at=None,
            missing=list(embedding.get("missing") or []),
            note=(
                "模型文件已就位，未在本接口加载或推理；缺依赖或权重不完整时索引与检索会返回明确错误"
                if embedding["configuration_status"] == "CONFIGURED_UNVERIFIED"
                else "缺少本地模型配置或文件，需要检索取证的规则会明确报错，系统不会自动下载模型或改用远程模型"
            ),
        )
    )
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
    if page < 1 or page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_INVALID", "分页参数不合法：page 从 1 开始，每页 1 到 200 条", status_code=400)
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
