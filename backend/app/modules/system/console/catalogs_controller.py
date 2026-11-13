"""上传策略、材料类型、工具与审核状态词表接口。"""

from fastapi import APIRouter, Request

from app.common.response import ResponseSchema, ok
from app.constants.status import STATUS_CATEGORY_LABELS
from app.core.config import get_settings
from app.domain.audit import audit_vocab
from app.domain.tools import tool_catalog
from app.integrations.documents.material_classify import list_material_types
from app.modules.system.console.schema import (
    MaterialTypeCatalogResponse,
    MaterialTypeItem,
    StatusVocabularyDomain,
    StatusVocabularyResponse,
    StatusVocabularyTerm,
    ToolCatalogItem,
    ToolCatalogResponse,
    UploadKindPolicy,
    UploadPolicyResponse,
)
from app.services.shared.storage import DOCUMENT_EXTENSIONS, RULEBOOK_EXTENSIONS

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/upload-policy", response_model=ResponseSchema[UploadPolicyResponse])
def upload_policy(request: Request) -> ResponseSchema[UploadPolicyResponse]:
    """上传策略：规则库与业务材料各自的大小上限与允许扩展名。

    这是上传限制的唯一真相源，前端从这里读取并展示，不再复制常量——复制的那一份
    会在配置变更后与后端实际校验不一致。实际写入时的最终校验仍在
    services.shared.storage.save_upload，超限显式抛 FILE_TOO_LARGE。

    该上限只覆盖本系统上传入口。8018 / 8010 / OCR 与部署入口各自的体量上限
    是独立的外部契约，本接口不声明、也未经验证。
    """
    settings = get_settings()
    return ok(UploadPolicyResponse(
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
    ), request)


@router.get("/material-types", response_model=ResponseSchema[MaterialTypeCatalogResponse])
def material_types(request: Request) -> ResponseSchema[MaterialTypeCatalogResponse]:
    """8018 支持的材料识别类型目录。

    材料上传页的文件类型下拉从这里取值，不能自由输入：8018 的 doc_type 是严格枚举，
    自由输入的值会在服务端被直接拒绝。上传入口与材料检查节点前端交给 8018 的
    `doc_type` 都取自这里。

    目录原样转发自 8018，本系统不做缓存、不内置默认清单，也不把 8018 的取值与
    业务材料名称做映射（例如不把「结算协议」认作「结算报告」）。服务未配置或不可用时
    明确报错，不返回一份编造的目录。
    """
    items = list_material_types()
    return ok(MaterialTypeCatalogResponse(
        items=[MaterialTypeItem(id=item["id"], name=item["name"]) for item in items],
    ), request)


@router.get("/tools", response_model=ResponseSchema[ToolCatalogResponse])
def tools_catalog(request: Request) -> ResponseSchema[ToolCatalogResponse]:
    """供模型按需选择的真实工具、参数和实现信息；不决定规则执行路径。"""
    return ok(ToolCatalogResponse(
        tools=[ToolCatalogItem.model_validate(item) for item in tool_catalog.catalog_payload()],
    ), request)


@router.get("/status-vocabulary", response_model=ResponseSchema[StatusVocabularyResponse])
def status_vocabulary(request: Request) -> ResponseSchema[StatusVocabularyResponse]:
    """审核状态词表：按语境给出每个状态码的唯一中文文案、分档与说明。

    页面只按语境取文案，不再各自维护状态表。词表里没有的码必须显示原始码并
    标注「未识别」，不能补一个看起来正常的中文；分档之外的结果也不能静默消失。
    """
    return ok(StatusVocabularyResponse(
        domains=[
            StatusVocabularyDomain(
                domain=domain,
                statuses=[
                    StatusVocabularyTerm(**term)
                    for term in audit_vocab.domain_status_terms(domain)
                ],
            )
            for domain in audit_vocab.STATUS_DOMAINS
        ],
        categories=[
            {"category": category, "label": label}
            for category, label in STATUS_CATEGORY_LABELS.items()
        ],
    ), request)
