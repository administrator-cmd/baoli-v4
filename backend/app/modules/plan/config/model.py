"""方案与方案配置的模型定义。"""

from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(kw_only=True)
class PlanMaterial:
    """materials 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "materials"
    owner_key: ClassVar[str] = "plan_id"

    id: int | None = None
    plan_id: int
    material_key: str
    name: str
    required: bool
    description: str = ''
    source_ref: str | None = None
    origin_json: dict[str, Any] | None = None
    sort_no: int = 0


@dataclass(kw_only=True)
class PlanField:
    """fields 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "fields"
    owner_key: ClassVar[str] = "plan_id"

    id: int | None = None
    plan_id: int
    field_key: str
    material_key: str
    name: str
    value_type: str
    extraction_strategy: str
    structure_schema_json: dict[str, Any] | None = None
    unit: str | None = None
    currency: str | None = None
    artifact_source: str | None = None
    period_scope: str
    statement_scope: str
    entity_scope: str = 'SELF'
    required: bool
    instruction: str = ''
    source_ref: str | None = None
    origin_json: dict[str, Any] | None = None
    sort_no: int = 0


@dataclass(kw_only=True)
class PlanRule:
    """rules 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "rules"
    owner_key: ClassVar[str] = "plan_id"

    id: int | None = None
    plan_id: int
    rule_key: str
    name: str
    node_code: str
    operator_code: str
    enabled: bool = True
    rule_json: dict[str, Any]
    source_ref: str | None = None
    origin_json: dict[str, Any] | None = None
    sort_no: int = 0


@dataclass(kw_only=True)
class PlanSection:
    """sections 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "sections"
    owner_key: ClassVar[str] = "plan_id"

    id: int | None = None
    plan_id: int
    section_key: str
    title: str
    content_instruction: str = ''
    field_keys: list[str]
    data_refs: list[str] = field(default_factory=list)
    sort_no: int = 0
    report_code: str | None = None
    report_name: str | None = None
    material_keys: list[str] | None = None
    formats: list[str] | None = None
    section_order: int | None = None
    content_type: str | None = None
    result_sort: str | None = None
    risk_level_order: list[str] | None = None
    config_status: str | None = None
    origin_json: dict[str, Any] | None = None
