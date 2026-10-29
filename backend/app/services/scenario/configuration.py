"""场景草稿与发布配置的只读视图组装。"""

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain import manifest as manifest_module
from ...domain.calculation_config import calculation_store
from ...domain.report_data import report_data_refs
from ...domain.rule_library import active, is_risk_rule
from ...entity.scenario_drafts import ScenarioDraft, ScenarioDraftField, ScenarioDraftRule
from ...entity.scenarios import ScenarioTemplate
from ..shared import configuration as config_store
from .common import DRAFT_SOURCE_LABELS, _node_type_label
from .draft import draft_rows
from .lookup import find_latest_published_version, template_or_404
from .publishing import compile_draft_manifest


def draft_config_view(db: Session, template: ScenarioTemplate) -> dict[str, Any]:
    """只读已有草稿；查看操作不创建草稿。"""
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
    if draft is None:
        raise BusinessError("SCENARIO_DRAFT_NOT_FOUND", "该场景没有未发布草稿", status_code=404)
    return draft_config_payload(db, draft)


def configuration_view(db: Session, template: ScenarioTemplate, view: str | None = None) -> dict[str, Any]:
    """编辑页读取当前配置：有草稿就读草稿，否则读发布版本；不创建草稿。"""
    if view == "DRAFT":
        return draft_config_view(db, template)
    if view is None:
        draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
        if draft is not None:
            return draft_config_payload(db, draft)
    version = find_latest_published_version(db, template.scenario_code)
    if version is None:
        if view == "PUBLISHED":
            raise BusinessError("SCENARIO_NOT_PUBLISHED", "该场景尚无已发布配置", status_code=404)
        return draft_config_view(db, template)
    config = manifest_module.editable_config_of(version.manifest_json, namespace=template.scenario_code)
    collections = []
    for section, config_key in (
        ("nodes", "node_instances"),
        ("materials", "materials"),
        ("fields", "fields"),
        ("rules", "rules"),
        ("sections", "sections"),
    ):
        items = config[config_key]
        if section == "rules":
            items = [item for item in items if not item["is_system"] and not item["rule_json"].get("calculation_ref")]
        collections.append([config_store.ConfigItem(version, section, {**item, "sort_no": index})
                            for index, item in enumerate(items, start=1)])
    return {
        "scenario_code": template.scenario_code, "configuration_kind": "PUBLISHED",
        "version_no": version.version_no, "revision_no": None,
        "source": version.source, "source_label": "已发布配置", "base_version_no": None,
        "schema_key": version.schema_key, "schema_version": version.schema_version,
        "schema_json": version.schema_json, "profile_version": version.manifest_json.get("profile_version"),
        "sync_error": None, "updated_at": version.published_at,
        **_configuration_items(*collections, compiled=version.manifest_json),
        "calculations": _calculation_view_rows(config["calculation_json"]),
    }


def draft_config_payload(db: Session, draft: ScenarioDraft) -> dict[str, Any]:
    nodes, materials, fields, rules, sections = draft_rows(db, draft.id)
    template = template_or_404(db, draft.scenario_code)
    try:
        compiled = compile_draft_manifest(db, template, draft)
        sync_error = None
    except BusinessError as exc:
        # 草稿允许保存不完整的配置。编译失败写进视图，页面据此说明原因并继续可编辑；
        # 不能因为一份还没配完的草稿就让整页读取失败。
        compiled = {"rules": [], "calculations": []}
        sync_error = {"code": exc.code, "message": exc.message, "details": exc.details}
    return {
        "scenario_code": draft.scenario_code, "configuration_kind": "DRAFT", "version_no": None,
        "revision_no": draft.revision_no, "source": draft.source,
        "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
        "base_version_no": draft.base_version_no, "schema_key": draft.schema_key,
        "schema_version": draft.schema_version, "schema_json": draft.schema_json,
        "profile_version": draft.profile_version, "sync_error": sync_error, "updated_at": draft.updated_at,
        **_configuration_items(nodes, materials, fields, rules, sections, compiled=compiled),
        "calculations": _calculation_view_rows(draft.calculation_json),
    }


def _configuration_items(nodes, materials, fields, rules, sections, *, compiled: dict[str, Any]) -> dict[str, Any]:
    """草稿与发布快照共用展示映射，发布内容直接使用其保存的编译结果。"""
    material_names = {item.material_key: item.name for item in materials}
    field_map = {item.field_key: item for item in fields}
    rule_names = {item.rule_key: item.name for item in rules}
    # 技术容器用于执行记录关联，不是用户配置的业务阶段。
    node_items = [
        {
            "node_code": item.node_code,
            "node_type": item.node_type,
            "node_type_label": _node_type_label(item.node_type),
            "name": item.name,
            "sequence_no": item.sequence_no,
            "depends_on": list(item.depends_on or []),
            "config_json": item.config_json,
            "enabled": item.enabled,
            "updated_at": item.updated_at,
        }
        for item in nodes
    ]
    material_items = [
        {
            "material_key": item.material_key,
            "name": item.name,
            "required": item.required,
            "description": item.description,
            "source_ref": item.source_ref,
            "origin": item.origin_json,
            "sort_no": item.sort_no,
            "updated_at": item.updated_at,
        }
        for item in materials
    ]
    field_items = [
        {
            "field_key": item.field_key,
            "material_key": item.material_key,
            "material_name": material_names.get(item.material_key),
            "name": item.name,
            "value_type": item.value_type,
            "node_code": getattr(item, "node_code", None),
            "extraction_strategy": item.extraction_strategy,
            "structure_schema": item.structure_schema_json,
            "unit": item.unit,
            # 币种与产物来源是金额口径与主体核对的配置事实，草稿视图必须一并返回，
            # 否则页面看不到、也改不了它们。
            "currency": item.currency,
            "artifact_source": item.artifact_source,
            "period_scope": item.period_scope,
            "statement_scope": item.statement_scope,
            "required": item.required,
            "instruction": item.instruction,
            "source_ref": item.source_ref,
            "origin": item.origin_json,
            "sort_no": item.sort_no,
            "updated_at": item.updated_at,
        }
        for item in fields
    ]
    rule_map = {rule.rule_key: rule for rule in rules}
    rule_items = [_draft_rule_view(item, material_names, field_map, rule_names, rule_map) for item in rules]
    # 派生项直接取实际编译结果，草稿展示与发布使用同一身份和启停。
    stored_keys = {item.rule_key for item in rules}
    for rule in compiled["rules"]:
        if rule["rule_key"] in stored_keys or rule.get("calculation_ref"):
            continue
        rule_items.append({**rule, "system": True,
                           "materials": [{"material_key": key, "material_name": material_names.get(key)} for key in rule.get("materials") or []],
                           "inputs": [{**entry, "field_name": field_map[entry["field_key"]].name if entry["field_key"] in field_map else None} for entry in rule.get("inputs") or []],
                           "outputs": [{"field_key": key, "field_name": field_map[key].name if key in field_map else None} for key in rule.get("outputs") or []],
                           "updated_at": None})
    data_names = {key: item.name for key, item in field_map.items()}
    data_names.update({item["rule_key"]: item["name"] for item in compiled["rules"]})
    data_names.update({item["result_code"]: item.get("rule_name") or item["rule_code"]
                       for item in compiled["calculations"]})
    section_items = [
        {
            "section_key": item.section_key,
            "title": item.title,
            "content_instruction": item.content_instruction,
            "field_keys": list(item.field_keys or []),
            "field_names": [field_map[key].name for key in (item.field_keys or []) if key in field_map],
            "data_refs": report_data_refs(item.data),
            "data_names": [data_names.get(ref, f"未明确的引用：{ref}")
                           for ref in report_data_refs(item.data)],
            "sort_no": item.sort_no,
            # 报告分组属性：页面据此按报告展示与维护，而不是把章节当成
            # 无归属的附加章节。
            "report_code": item.report_code,
            "report_name": item.report_name,
            "material_keys": list(item.material_keys or []),
            "material_names": [
                material_names.get(key, key) for key in (item.material_keys or [])
            ],
            "formats": list(item.formats or []),
            "section_order": item.section_order,
            "content_type": item.content_type,
            "result_sort": item.result_sort,
            "risk_level_order": list(getattr(item, "risk_level_order", None) or []),
            "config_status": item.config_status,
            "origin": item.origin_json,
            "updated_at": item.updated_at,
        }
        for item in sections
    ]
    return {
        # 规则执行记录的技术容器。
        "nodes": node_items,
        "materials": material_items,
        "fields": field_items,
        "rules": rule_items,
        "sections": section_items,
        "business_rule_count": len(rules),
        "system_rule_count": sum(bool(item.get("system")) for item in rule_items),
    }


def _draft_rule_view(
    item: ScenarioDraftRule,
    material_names: dict[str, str],
    field_map: dict[str, ScenarioDraftField],
    rule_names: dict[str, str],
    rule_map: dict[str, ScenarioDraftRule],
) -> dict[str, Any]:
    definition = item.rule_json if isinstance(item.rule_json, dict) else {}
    parameters = definition.get("parameters") if isinstance(definition.get("parameters"), dict) else {}
    inputs = []
    for entry in definition.get("inputs", []):
        field = field_map.get(entry.get("field_key"))
        inputs.append(
            {
                "field_key": entry.get("field_key"),
                "material_key": entry.get("material_key"),
                "field_name": field.name if field else None,
                "material_name": material_names.get(entry.get("material_key")),
                "value_type": field.value_type if field else None,
                "unit": field.unit if field else None,
            }
        )
    applicability = definition.get("applicability") if isinstance(definition.get("applicability"), dict) else {}
    material_keys = list(dict.fromkeys(str(key) for key in (definition.get("materials") or []) if str(key)))
    if is_risk_rule({**definition, "operator": item.operator_code}):
        source_keys = list(dict.fromkeys([
            *(definition.get("source_rule_keys") or []),
            *([definition["source_rule_key"]] if definition.get("source_rule_key") else []),
        ]))
        for source_key in source_keys:
            source = rule_map.get(source_key)
            if source is None or not isinstance(source.rule_json, dict):
                continue
            source_definition = source.rule_json
            for material_key in source_definition.get("materials") or []:
                if material_key not in material_keys:
                    material_keys.append(material_key)
            for source_input in source_definition.get("inputs") or []:
                material_key = source_input.get("material_key")
                if material_key and material_key not in material_keys:
                    material_keys.append(material_key)
            for field_key in source_definition.get("outputs") or []:
                field = field_map.get(field_key)
                if field is not None and field.material_key not in material_keys:
                    material_keys.append(field.material_key)
    return {
        "rule_key": item.rule_key,
        "name": item.name,
        "node": item.node_code,
        "operator": item.operator_code,
        "execution_mode": definition.get("execution_mode") or "UNKNOWN",
        "enabled": active({**definition, "enabled": item.enabled}),
        "system": False,
        "inputs": inputs,
        "materials": [
            {"material_key": key, "material_name": material_names.get(key)}
            for key in material_keys
        ],
        "outputs": [
            {"field_key": key, "field_name": field_map[key].name, "material_key": field_map[key].material_key}
            for key in definition.get("outputs", []) if key in field_map
        ],
        "parameters": parameters,
        "requirement": str(definition.get("requirement") or ""),
        "applicability": {"kind": applicability.get("kind", "ALWAYS")},
        "source_rule_key": definition.get("source_rule_key"),
        "source_rule_name": rule_names.get(definition.get("source_rule_key")) if definition.get("source_rule_key") else None,
        "risk_level": parameters.get("risk_level"),
        "risk_action": parameters.get("action"),
        "source_ref": item.source_ref,
        "origin": item.origin_json,
        # 业务配置事实：页面要能显示检查等级、合格要求原文、风险等级原文、
        # 成立条件、多来源与配置状态，不能只给执行参数。
        "check_level": definition.get("check_level"),
        "check_operator": definition.get("check_operator"),
        "risk_level_value": definition.get("risk_level_value"),
        "risk_condition": definition.get("risk_condition"),
        "rule_role": "RISK" if is_risk_rule({**definition, "operator": item.operator_code}) else "CHECK",
        "source_rule_keys": list(definition.get("source_rule_keys") or []),
        "source_rule_codes": list(definition.get("source_rule_codes") or []),
        "config_status": definition.get("config_status"),
        "updated_at": item.updated_at,
    }


def _calculation_view_rows(raw: Any) -> list[dict[str, Any]]:
    """按原始计算定义展示，不读取旧编译产物。"""
    candidates = calculation_store(raw)
    source = candidates
    return [
        {
            "rule_code": item.get("rule_code"),
            "name": item.get("rule_name") or item.get("name"),
            "node_code": item.get("node_code") or item.get("node_instance_code"),
            "formula": item.get("formula"),
            "requirement": item.get("requirement", ""),
            "business_context": item.get("business_context", {}),
            "result_code": item.get("result_code"),
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "status": item.get("status"),
            "origin": item.get("origin"),
            "definition": deepcopy(item),
        }
        for item in source
        if isinstance(item, dict)
    ]
