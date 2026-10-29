from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...common.api import ApiResponse


class SystemServiceItem(BaseModel):
    """一个渠道的配置与本次探测结果。

    连接状态只来自真实探测；`latency_ms` 与 `last_checked_at` 同一次检查写入，
    短时间内重复查看会复用那一次探测并原样返回它的检查时间，不冒充刚刚探测过。
    """

    name: str
    capability: str
    configuration_status: str
    connection_status: str
    last_checked_at: datetime | None = None
    latency_ms: int | None = None
    missing: list[str] = Field(default_factory=list)
    note: str | None = None

class SystemStatusResponse(ApiResponse):
    """系统总体状态；数据库与大模型的连接结论与状态页同源。"""

    service: Literal["RUNNING", "DEGRADED"]
    database: Literal["CONNECTED", "FAILED", "NOT_CONFIGURED"]
    services: list[SystemServiceItem] = Field(default_factory=list)

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


class ToolCatalogItem(BaseModel):
    """工具的实际实现、参数和返回结果说明。"""

    tool_id: str
    version: str
    label: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    handler_ref: dict[str, str] | None = None
    allowed_scenes: list[str] | None = None
    required_evidence: list[str] = Field(default_factory=list)
    side_effect: bool = False
    idempotent: bool = True
    supports_query: bool = False
    # 四个口径分开返回，页面据此区分「已注册 / 节点级可调用 / Agent 可选择 / 已实现」。
    # 只有一个笼统的「可调用」会让「注册了但模型调不到」显示成模型能用。
    registered: bool = True
    implemented: bool = False
    judgement_supported: bool | None = None
    judgement_unsupported_reason: str | None = None
    agent_selectable: bool = False
    agent_unselectable_reason: str | None = None
    unimplemented_reason: str | None = None
    input_count: str | None = None
    runtime_keys: list[str] = Field(default_factory=list)

class ToolCatalogResponse(ApiResponse):
    """模型可调用工具的唯一目录，不声明业务节点或发布能力缺口。"""

    tools: list[ToolCatalogItem] = Field(default_factory=list)

class StatusVocabularyTerm(BaseModel):
    """一个状态码在某个语境下的标准文案。

    页面显示状态时只用这里的中文与说明；未知状态码不在词表中，页面必须显示
    原始码并标注「未识别」，不能自行补一个看起来正常的中文。
    """

    code: str
    label: str
    category: str | None = None
    category_label: str | None = None
    note: str | None = None

class StatusVocabularyDomain(BaseModel):
    """一个语境（规则、风险、字段）下的完整状态词表。"""

    domain: str
    statuses: list[StatusVocabularyTerm] = Field(default_factory=list)

class StatusVocabularyResponse(ApiResponse):
    """全部语境的审核状态词表与分档。"""

    domains: list[StatusVocabularyDomain] = Field(default_factory=list)
    categories: list[dict[str, str]] = Field(default_factory=list)

class SystemServicesResponse(ApiResponse):
    database: Literal["CONNECTED", "FAILED", "NOT_CONFIGURED"]
    services: list[SystemServiceItem] = Field(default_factory=list)
