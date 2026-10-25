"""方案配置导入、重新编译与系统规则同步。"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain import manifest as manifest_module
from ...domain import node_profile
from ...domain.calculation_config import calculation_store
from ...domain.manifest import build_task_schema, compile_form_manifest
from ...domain.plan_calculations import selected_calculations
from ...domain.rule_library import active
from ...entity.plans import AuditPlan, PlanField, PlanMaterial, PlanRule, PlanSection
from ...entity.scenarios import ScenarioVersion
from ..shared import configuration as config_store
from ..system import operation_log as operation_log_service
from .views import plan_config_rows


def _load_manifest_into_plan(db: Session, plan: AuditPlan, manifest: dict[str, Any]) -> None:
    """把钉住的场景版本整份复制成方案自己的配置。

    与草稿初始化走同一份映射口径（`editable_config_of`）。
    系统派生规则也进入方案，供用户逐条启停；保存时仅使用其启停值，规则定义由编译器重建。
    """
    config = manifest_module.editable_config_of(manifest, namespace=plan.plan_code)
    if "import_proposals" in manifest:
        plan.config_json = {**plan.config_json, "import_proposals": deepcopy(manifest["import_proposals"])}
    for index, item in enumerate(config["materials"], start=1):
        config_store.add_mapping(db, PlanMaterial, plan.id, item, index)
    for index, item in enumerate(config["fields"], start=1):
        config_store.add_mapping(db, PlanField, plan.id, item, index)
    for index, item in enumerate(config["rules"], start=1):
        if not item["rule_json"].get("calculation_ref"):
            config_store.add_mapping(db, PlanRule, plan.id, {key: value for key, value in item.items() if key != "is_system"}, index)
    for index, item in enumerate(config["sections"], start=1):
        config_store.add_mapping(db, PlanSection, plan.id, item, index)
    # 节点是场景级执行结构：方案里只读，随方案冻结一份供编译使用。
    # 计算定义沿用场景版本，方案单独保存逐条启停选择。
    calculation_candidates = calculation_store(config["calculation_json"])
    calculation_source = calculation_candidates
    plan.context_json = {
        "nodes": config["node_instances"],
        "profile_version": manifest.get("profile_version"),
        "scenario_schema": manifest.get("scenario_schema"),
        "source_kind": manifest.get("source_kind"),
        "calculation_json": config["calculation_json"],
        "calculation_enabled": {
            str(item["rule_code"]): active(item)
            for item in calculation_source
        },
    }


def _recompile_plan(db: Session, plan: AuditPlan) -> None:
    """用方案自己的配置重新编译，并算出任务范围。

    编译失败照常保存：配置过程中必然不完整，页面要能存下来继续改。原因写进
    sync_error，这样的方案显示为「待核对」且不能被任务引用。
    失败时清空产物，不保留上一份——那会让页面显示的范围与用户刚改的配置不一致。

    与场景发布共用同一份映射口径（`compile_inputs_of_config`），两条路径编译出的产物
    必然同形，不会出现「同一份配置在两个入口编译出不同结果」。
    """
    materials, fields, rules, sections = plan_config_rows(db, plan.id)
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    try:
        inputs = manifest_module.compile_inputs_of_config(
            materials=materials,
            fields=fields,
            rules=rules,
            sections=sections,
            nodes=list(context.get("nodes") or []),
            profile_version=context.get("profile_version") or node_profile.NODE_PROFILE_VERSION,
            scenario_schema_config=context.get("scenario_schema"),
        )
        system_rule_enabled: dict[str, bool] = {}
        business_rules: list[dict[str, Any]] = []
        for rule in inputs["rules"]:
            if rule.get("calculation_ref"):
                continue
            if not manifest_module.is_system_rule(rule):
                business_rules.append(rule)
                continue
            rule_code = str(rule.get("rule_code") or "")
            if not rule_code.startswith(manifest_module.SYSTEM_RULE_CODE_PREFIX):
                raise BusinessError("PLAN_SYSTEM_RULE_CODE_MISSING", "方案系统规则缺少派生编号")
            if rule_code in system_rule_enabled:
                raise BusinessError("PLAN_SYSTEM_RULE_DUPLICATE", "方案系统规则编号重复", details={"rule_code": rule_code})
            system_rule_enabled[rule_code] = bool(rule["enabled"])
            if rule.get("generated_from") == "REPORT":
                system_rule_enabled[f"report:{rule['report_ref']}"] = bool(rule["enabled"])
        inputs["rules"] = business_rules
        calculation_candidates = selected_calculations(context)
        source_version = db.get(ScenarioVersion, plan.scenario_version_id)
        if source_version is None:
            raise BusinessError("SCENARIO_VERSION_MISSING", "方案关联的场景版本不存在，无法编译方案")
        manifest = compile_form_manifest(
            scene_code=plan.scenario_code,
            scene_name=source_version.scenario_name,
            # 方案用自己的命名空间编译：与场景版本的键空间分开，两者不互相引用。
            namespace=plan.plan_code,
            **inputs,
            compiled_at=datetime.now(UTC),
            calculation_candidates=calculation_candidates,
            system_rule_enabled=system_rule_enabled,
            import_proposals=plan.config_json.get("import_proposals"),
            source_kind=str(context.get("source_kind") or "FORM"),
        )
        # None = 全部启用：方案的启停已经编译进 manifest 的规则。
        schema = build_task_schema(manifest, None, allow_empty=True)
    except BusinessError as exc:
        plan.manifest_json = None
        plan.scope_hash = None
        plan.sync_error = {"code": exc.code, "message": exc.message}
        return
    _sync_plan_system_rules(db, plan, rules, manifest)
    plan.manifest_json = manifest
    plan.scope_hash = operation_log_service.normalized_request_hash(
        {
            "plan_code": plan.plan_code,
            "manifest_scope_hash": manifest_module.manifest_scope_hash(manifest),
            "enabled_rule_keys": schema["enabled_rule_keys"],
        }
    )
    plan.sync_error = None


def _sync_plan_system_rules(
    db: Session, plan: AuditPlan, stored_rules: list[PlanRule], manifest: dict[str, Any]
) -> None:
    """派生结构以编译结果为准，方案只保留每条系统规则的启停选择。"""
    compiled_config = manifest_module.editable_config_of(manifest, namespace=plan.plan_code)
    derived = {
        item["rule_json"]["rule_code"]: item
        for item in compiled_config["rules"]
        if item["is_system"]
    }
    existing = {
        str((row.rule_json or {}).get("rule_code")): row
        for row in stored_rules
        if manifest_module.is_system_rule({**(row.rule_json or {}), "operator": row.operator_code, "source_ref": row.source_ref})
    }
    for rule_code, row in existing.items():
        if rule_code not in derived:
            config_store.delete(row)
    next_sort_no = max((row.sort_no for row in stored_rules), default=0)
    for rule_code, item in derived.items():
        row = existing.get(rule_code)
        if row is None:
            next_sort_no += 1
            row = PlanRule(plan_id=plan.id, sort_no=next_sort_no)
            row = config_store.add(db, row)
        row.rule_key = item["rule_key"]
        row.name = item["name"]
        row.node_code = item["node_code"]
        row.operator_code = item["operator_code"]
        row.enabled = item["enabled"]
        row.rule_json = item["rule_json"]
        row.source_ref = item["source_ref"]
        row.origin_json = item["origin_json"]
    db.flush()
