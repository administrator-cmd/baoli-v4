from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...common.api import ApiResponse


class ExternalServiceConfiguration(BaseModel):
    status: Literal["UNCONFIGURED", "CONFIGURATION_INCOMPLETE", "CONFIGURED_UNVERIFIED"]
    missing: list[str] = Field(default_factory=list)

class SystemStatusResponse(ApiResponse):
    service: str
    database: Literal["CONNECTED", "FAILED"]
    external_services: dict[str, ExternalServiceConfiguration]

class UploadKindPolicy(BaseModel):
    """一种上传入口的体量上限与允许扩展名。"""

    max_bytes: int
    allowed_extensions: list[str]

class UploadPolicyResponse(ApiResponse):
    """上传策略的唯一真相源。

    前端只读该接口，不得自行复制大小常量或扩展名清单；服务端 storage_service.save_upload
    仍是最终校验者，超限仍显式抛 FILE_TOO_LARGE。
    """

    rulebook: UploadKindPolicy
    document: UploadKindPolicy
    # 明确声明该上限只覆盖本系统上传入口，不构成对外部服务与部署入口的承诺。
    scope_note: str

class MaterialTypeItem(BaseModel):
    """8018 支持目录中的一项材料识别类型。"""

    id: int
    name: str

class MaterialTypeCatalogResponse(ApiResponse):
    """8018 材料识别类型目录。原样转发，不内置默认清单、不做名称映射。"""

    items: list[MaterialTypeItem]

class CapabilityItem(BaseModel):
    """能力目录中的一项能力。

    页面据此渲染检查方式下拉、规则输入形态与参数提示，不再本地维护一份名单——
    本地名单与后端漂移过一次（后端 12 个算子，页面只有 7 个），
    结果是页面上打开一条已导入的规则，下拉里找不到它当前的检查方式。
    """

    capability_id: str
    label: str
    kind: str
    parent: str | None = None
    nodes: list[str] = Field(default_factory=list)
    new_rule_nodes: list[str]
    input_kind: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    output_keys: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    supports_evidence: bool = False
    supports_comparison: bool = False
    implemented: bool = False
    unimplemented_reason: str | None = None
    execution: str = ""
    evidence: str | None = None
    judge: str | None = None
    extension_namespace: str | None = None
    version: str = ""
    note: str | None = None

class ToolCatalogItem(BaseModel):
    """工具目录中的一项工具。

    与 `CapabilityItem` 的分工：能力项回答「这条规则由哪项能力判定」，
    工具项回答「模型可以调用什么、参数长什么样、当前能不能真跑」。
    两者都由 `domain/tool_catalog.py` 从能力目录派生，不各自维护一份名单。
    """

    tool_id: str
    version: str
    label: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    handler_ref: dict[str, str] | None = None
    allowed_nodes: list[str] = Field(default_factory=list)
    allowed_scenes: list[str] | None = None
    required_evidence: list[str] = Field(default_factory=list)
    side_effect: bool = False
    idempotent: bool = True
    supports_query: bool = False
    # 四个口径分开返回，页面据此区分「已注册 / 节点级可调用 / Agent 可选择 / 已实现」。
    # 只有一个笼统的「可调用」会让「注册了但模型调不到」显示成模型能用。
    registered: bool = True
    implemented: bool = False
    agent_selectable: bool = False
    node_level_only: bool = False
    agent_unselectable_reason: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    unimplemented_reason: str | None = None
    input_count: str | None = None
    runtime_keys: list[str] = Field(default_factory=list)

class CapabilityCatalogResponse(ApiResponse):
    """审核能力目录。

    后端能力的单一事实来源：前端从这里取算子选项、节点约束与参数口径。implemented=false
    的能力可以配置，但发布前必须逐条确认，运行时明确返回不可判定。`tools` 是同一份目录的
    工具视图，只有 `implemented=true` 的工具才会进模型请求的 `tools`，不可调用的工具仍如实
    列出并写明原因。
    """

    capability_version: str
    rule_statuses: list[str] = Field(default_factory=list)
    items: list[CapabilityItem] = Field(default_factory=list)
    tool_catalog_version: str = ""
    # 工具目录的内容指纹：随已发布配置冻结，用于对账「模型当时看到的是哪一版工具」。
    tool_catalog_fingerprint: str = ""
    tools: list[ToolCatalogItem] = Field(default_factory=list)

class RuleCapabilityStatusResponse(BaseModel):
    """一个规则结果状态的标准解释。

    页面显示状态时用它区分「已判定 / 尚未判定 / 不适用 / 未识别」，
    而不是把不认识的码显示成「未开始」。
    """

    status: str | None = None
    known: bool = False
    label: str | None = None
    category: str = "UNKNOWN"
    category_label: str | None = None
    note: str | None = None

class RuleStatusCatalogResponse(ApiResponse):
    """全部审核结论状态的标准口径与分档。"""

    statuses: list[RuleCapabilityStatusResponse] = Field(default_factory=list)
    categories: list[dict[str, str]] = Field(default_factory=list)

class SystemServiceItem(BaseModel):
    name: str
    capability: str
    configuration_status: str
    connection_status: str
    last_checked_at: datetime | None = None
    missing: list[str] = Field(default_factory=list)
    note: str | None = None

class SystemServicesResponse(ApiResponse):
    database: Literal["CONNECTED", "FAILED"]
    services: list[SystemServiceItem] = Field(default_factory=list)

__all__ = [
    "ExternalServiceConfiguration",
    "SystemStatusResponse",
    "UploadKindPolicy",
    "UploadPolicyResponse",
    "MaterialTypeItem",
    "MaterialTypeCatalogResponse",
    "CapabilityItem",
    "ToolCatalogItem",
    "CapabilityCatalogResponse",
    "RuleCapabilityStatusResponse",
    "RuleStatusCatalogResponse",
    "SystemServiceItem",
    "SystemServicesResponse",
]

