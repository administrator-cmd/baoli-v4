"""上传策略、材料类型、工具与规则状态目录接口。"""

from fastapi import APIRouter, Request
from ...config import get_settings
from ...domain import audit_vocab, tool_catalog
from ...schema.vo import (
    MaterialTypeCatalogResponse,
    MaterialTypeItem,
    RuleCapabilityStatusResponse,
    RuleStatusCatalogResponse,
    ToolCatalogItem,
    UploadKindPolicy,
    UploadPolicyResponse,
    ToolCatalogResponse,
)
from ...integrations.http_services import list_material_types
from ...services.shared.storage import DOCUMENT_EXTENSIONS, RULEBOOK_EXTENSIONS


router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/upload-policy", response_model=UploadPolicyResponse)
def upload_policy(request: Request) -> UploadPolicyResponse:
    """上传策略：规则库与业务材料各自的大小上限与允许扩展名。

    这是上传限制的唯一真相源，前端从这里读取并展示，不再复制常量——复制的那一份
    会在配置变更后与后端实际校验不一致。实际写入时的最终校验仍在
    services.shared.storage.save_upload，超限显式抛 FILE_TOO_LARGE。

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
