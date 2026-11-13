"""配置编辑允许修改的固定字段；业务扩展元数据由独立入口保存。"""

from typing import Any, TypedDict


class MaterialChanges(TypedDict, total=False):
    name: str
    required: bool
    description: str
    source_ref: str | None


class FieldChanges(TypedDict, total=False):
    name: str
    material_key: str
    value_type: str
    extraction_strategy: str
    structure_schema_json: dict[str, Any] | None
    node_code: str | None
    unit: str | None
    currency: str | None
    artifact_source: str | None
    period_scope: str
    statement_scope: str
    entity_scope: str
    required: bool
    instruction: str
    source_ref: str | None


class RuleChanges(TypedDict, total=False):
    name: str
    node_code: str
    operator_code: str
    enabled: bool
    rule_json: dict[str, Any]
    source_ref: str | None


class GeneratedRuleChanges(RuleChanges, total=False):
    """编译生成的规则允许同步派生身份与来源，普通编辑不开放这些字段。"""

    rule_key: str
    origin_json: dict[str, Any] | None


class SectionChanges(TypedDict, total=False):
    title: str
    content_instruction: str
    field_keys: list[str]
    data_refs: list[str]
    report_code: str | None
    report_name: str | None
    material_keys: list[str] | None
    formats: list[str] | None
    section_order: int | None
    content_type: str | None
    result_sort: str | None
    risk_level_order: list[str] | None
    config_status: str | None
