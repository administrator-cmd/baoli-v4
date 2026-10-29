"""方案配置与规则选择视图组装。"""

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...constants.status import SCENARIO_VERSION_STATUS_PUBLISHED
from ...domain import manifest as manifest_module
from ...domain.plan_calculations import plan_calculation_config
from ...domain.report_data import report_data_refs
from ...domain.rule_library import active
from ...entity.plans import AuditPlan, PlanField, PlanMaterial, PlanRule, PlanSection
from ...entity.scenarios import ScenarioVersion
from ..shared import configuration as config_store


def plan_config_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    """方案的可编辑配置与状态：材料 / 字段 / 全部规则 / 报告章节 / 完整计算定义。

    这是方案页各页签的数据源，同时携带方案本身的基本资料与范围状态；
    方案页不再单独读取另一份“详情”。节点与选用 Sheet 不在这里 —— 它们是场景级执行结构，
    方案里只读（随 `context_json` 冻结），单独返回供页面展示。
    """
    materials, fields, rules, sections = plan_config_rows(db, plan.id)
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    calculation_candidates, calculation_enabled = plan_calculation_config(context)
    calculation_source = calculation_candidates
    # 只取展示用的三列：场景版本行带着几百 KB 的 manifest_json，整行载入没有意义。
    version = (
        db.execute(
            select(
                ScenarioVersion.version_no,
                ScenarioVersion.published_at,
                ScenarioVersion.scenario_name,
            ).where(ScenarioVersion.id == plan.scenario_version_id)
        ).first()
        if plan.scenario_version_id
        else None
    )
    return {
        "plan_code": plan.plan_code,
        "plan_name": plan.plan_name,
        "scenario_code": plan.scenario_code,
        "description": plan.description,
        "enabled": plan.enabled,
        "deleted": plan.deleted_at is not None,
        # 与列表页同一口径：sync_error 非空表示最近一次保存没算出可用范围。
        "effective": plan.sync_error is None and plan.scope_hash is not None,
        "sync_error": plan.sync_error,
        "scope_hash": plan.scope_hash,
        "base_scenario_version": (
            {
                "version_no": version.version_no,
                "status": SCENARIO_VERSION_STATUS_PUBLISHED,
                "published_at": version.published_at,
                "scenario_name": version.scenario_name,
            }
            if version
            else None
        ),
        "materials": [
            {
                "material_key": item.material_key,
                "name": item.name,
                "required": item.required,
                "description": item.description,
                "config_status": item.data.get("config_status"),
                "business_context": deepcopy(item.data.get("business_context", {})),
                "source_ref": item.source_ref,
                "origin": item.origin_json,
                "sort_no": item.sort_no,
            }
            for item in materials
        ],
        "fields": [
            {
                "field_key": item.field_key,
                "material_key": item.material_key,
                "node_code": item.data.get("node_code"),
                "enabled": active(item.data),
                "config_status": item.data.get("config_status"),
                "business_context": deepcopy(item.data.get("business_context", {})),
                "name": item.name,
                "value_type": item.value_type,
                "extraction_strategy": item.extraction_strategy,
                "structure_schema": item.structure_schema_json,
                "unit": item.unit,
                "currency": item.currency,
                "artifact_source": item.artifact_source,
                "period_scope": item.period_scope,
                "statement_scope": item.statement_scope,
                "required": item.required,
                "instruction": item.instruction,
                "source_ref": item.source_ref,
                "origin": item.origin_json,
                "sort_no": item.sort_no,
            }
            for item in fields
        ],
        "rules": [
            {
                "rule_key": item.rule_key,
                "name": item.name,
                "node_code": item.node_code,
                "operator_code": item.operator_code,
                "enabled": active({**(item.rule_json or {}), "enabled": item.enabled}),
                "rule_code": (item.rule_json or {}).get("rule_code") or item.source_ref,
                "is_system": manifest_module.is_system_rule({**(item.rule_json or {}), "operator": item.operator_code, "source_ref": item.source_ref}),
                "rule_json": dict(item.rule_json or {}),
                "source_ref": item.source_ref,
                "origin": item.origin_json,
                "sort_no": item.sort_no,
            }
            for item in rules
        ],
        "sections": [
            {
                "section_key": item.section_key,
                "title": item.title,
                "content_instruction": item.content_instruction,
                "field_keys": list(item.field_keys or []),
                "data_refs": report_data_refs(item.data),
                "report_code": item.report_code,
                "report_name": item.report_name,
                "material_keys": list(item.material_keys or []),
                "formats": list(item.formats or []),
                "section_order": item.section_order,
                "content_type": item.content_type,
                "result_sort": item.result_sort,
                "risk_level_order": list(getattr(item, "risk_level_order", None) or []),
                "config_status": item.config_status,
                "origin": item.origin_json,
                "sort_no": item.sort_no,
            }
            for item in sections
        ],
        # 只读：本方案会跑哪些节点。页面只展示，不改。
        "nodes": list(context.get("nodes") or []),
        "calculation_rules": [
            {
                **deepcopy(item),
                "rule_code": item["rule_code"],
                "rule_name": item.get("rule_name") or item["rule_code"],
                "result_code": item["result_code"],
                "formula": item.get("formula", ""),
                "requirement": item.get("requirement", ""),
                "business_context": item.get("business_context", {}),
                "node_code": item.get("node_code") or item.get("node_instance_code"),
                "enabled": calculation_enabled[str(item["rule_code"])],
            }
            for item in calculation_source
        ],
    }


def plan_config_rows(
    db: Session, plan_id: int
) -> tuple[list[PlanMaterial], list[PlanField], list[PlanRule], list[PlanSection]]:
    """方案自己的配置行，按用户可见的顺序。"""
    return (
        list(sorted(config_store.rows(db, PlanMaterial, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanField, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanRule, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanSection, plan_id), key=lambda item: (item.sort_no, item.id,))),
    )
