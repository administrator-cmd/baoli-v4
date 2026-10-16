"""审核方案管理：以已发布场景版本为基准的个性化配置。

创建方案时把钉住的那一版场景的配置（材料 / 字段 / 规则 / 报告章节）整份复制到方案自己的
`config_json`；节点与选用 Sheet 存在同一 JSON 的 context 内，随方案冻结。之后由用户增删改。规则不再分「系统派生 / 业务」：
用户在方案里看到的就是全部规则，逐条可勾可取消，取消一条抽取规则就是那个字段不抽。

每次保存都用方案自己的配置重新编译（`compile_form_manifest`）并写 `manifest_json`，再用
`build_task_schema` 算任务范围写入 `scope_json`；算不出来时保存照常成功，原因记在 `sync_error`，
页面显示「待核对」，且不能被任务引用。

场景后来发布新版本不会改变本方案（要换基准请另建方案）；已建任务把方案的编译产物整份抓进
`audit_task.manifest_json`，执行期只读它。
"""

from copy import deepcopy
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.calculation_config import (
    calculation_store,
    calculations_referencing_field,
    remap_calculation_field_codes,
)

from ..repositories import configuration_store as config_store
from ..repositories.scenario_repository import find_latest_published_version

from ..domain import field_strategy, manifest as manifest_module
from ..domain.plan_calculations import plan_calculation_config, selected_calculations
from ..domain.audit_vocab import SCOPE_NOT_APPLICABLE
from ..domain.report_data import report_data_refs
from ..domain.rule_library import active
from ..domain import node_profile
from ..domain.manifest import FORM_VALUE_TYPES, build_task_schema, compile_form_manifest
from ..common.exceptions import BusinessError
from ..entity import (
    AuditPlan,
    PlanField,
    PlanMaterial,
    PlanRule,
    PlanSection,
    ScenarioTemplate,
    ScenarioVersion,
)
from ..domain.identity import public_no
from .operation_log_service import record_operation
from .operation_types import OBJECT_PLAN
from . import operation_log_service

logger = logging.getLogger(__name__)


def plan_or_404(db: Session, plan_code: str, *, lock: bool = False, include_deleted: bool = False) -> AuditPlan:
    """按方案编码取方案；取不到即 404。lock 供写路径先锁行，include_deleted 供恢复与历史查看。"""
    statement = select(AuditPlan).where(AuditPlan.plan_code == plan_code)
    if not include_deleted:
        statement = statement.where(AuditPlan.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update()
    plan = db.scalar(statement)
    if not plan:
        raise BusinessError(
            "PLAN_NOT_FOUND",
            "审核方案不存在或已被删除",
            status_code=404,
            details={"plan_code": plan_code},
        )
    return plan


def _scenario_or_404(db: Session, scenario_code: str) -> ScenarioTemplate:
    """取场景身份；不存在或已删除时 404。

    只做「存在性」判断。能不能用来建方案由 `_published_scenario_or_fail` 判断：
    存在但未发布、已停用的场景同样不能建方案，那是业务状态问题而不是找不到。
    """
    template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))
    if not template:
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "审核场景不存在或已被删除",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    return template


def _published_scenario_or_fail(db: Session, scenario_code: str) -> ScenarioVersion:
    """业务用户可用的场景版本：场景必须未删除、已启用、且已有已发布版本。

    业务用户只能从「已发布且启用」的场景建方案。三种不可用状态
    分别明确报出，不合并成一个「场景不可用」——它们对管理员的处理动作不同：
    草稿还没发布要去发布，场景停用要去启用，场景删除是另一回事。

    没有已发布版本时明确失败，不先建一个没有版本的半成品方案再等后台补绑定：
    那会让业务用户拿到一个看似存在、实际无法创建任务的方案。
    """
    template = _scenario_or_404(db, scenario_code)
    if not template.enabled:
        raise BusinessError(
            "SCENARIO_DISABLED",
            f"场景“{template.scenario_name}”已停用，不能用于创建方案",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    version = find_latest_published_version(db, scenario_code)
    if version is None:
        raise BusinessError(
            "SCENARIO_NOT_PUBLISHED",
            f"场景“{template.scenario_name}”还没有已发布的版本，不能创建方案：请先由管理员发布场景",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    return version


def _published_scenario_version_or_fail(
    db: Session, scenario_code: str, version_no: int
) -> ScenarioVersion:
    """按页面预览的明确版本号取可建方案的已发布版本。

    版本号与当前允许建方案的版本不一致时返回 `PLAN_VERSION_CHANGED`，由页面刷新后重试：
    静默改取「当前最新版本」会让用户看着一版预览、实际建出另一版的方案。
    """
    latest = _published_scenario_or_fail(db, scenario_code)
    if latest.version_no != version_no:
        raise BusinessError(
            "PLAN_VERSION_CHANGED",
            f"场景版本已变化（预览的是第 {version_no} 版，当前可用的最新已发布版本是第 {latest.version_no} 版）。"
            "请刷新页面重新核对范围后再创建方案",
            status_code=409,
            details={
                "scenario_code": scenario_code,
                "previewed_version_no": version_no,
                "current_version_no": latest.version_no,
            },
        )
    return latest


def create_plan(db: Session, payload: dict[str, Any]) -> AuditPlan:
    """创建方案：把钉住的那一版场景配置整份复制成方案自己的配置，并立即编译一次。

    版本号与当前可用的最新已发布版本不一致时明确返回 `PLAN_VERSION_CHANGED`，
    不静默改取「当前最新版本」。复制与编译在同一次提交里完成，不会先建一个空方案、
    失败后留下没有配置的半成品。

    过去返回的第二项是「指向不存在规则、已被移除的条目名」——方案现在持有自己的配置，
    不存在「引用了不存在的规则」这条路，因此不再返回它。
    """
    name = str(payload.get("plan_name") or "").strip()
    if not name:
        raise BusinessError("PLAN_NAME_REQUIRED", "方案名称不能为空", status_code=400)
    scenario_code = str(payload.get("scenario_code") or "")
    version_no = payload.get("scenario_version_no")
    if not isinstance(version_no, int):
        raise BusinessError(
            "PLAN_VERSION_REQUIRED",
            "创建方案必须携带页面预览的场景版本号，不能按场景编码改取最新版本",
            status_code=400,
            details={"scenario_code": scenario_code},
        )
    # 复制的是页面预览的那一版：它不是最新版或场景不可用时明确失败，
    # 不先建一个半成品方案再等后台补复制。
    published = _published_scenario_version_or_fail(db, scenario_code, version_no)
    plan = AuditPlan(
        plan_code=public_no("PLAN"),
        plan_name=name,
        scenario_code=scenario_code,
        scenario_version_id=published.id,
        description=(str(payload.get("description")).strip() or None) if payload.get("description") else None,
        enabled=bool(payload.get("enabled", True)),
    )
    db.add(plan)
    db.flush()
    # 场景版本 → 方案配置：与缺少草稿时从最新发布版本初始化共用映射口径。
    _load_manifest_into_plan(db, plan, published.manifest_json)
    db.flush()
    # 立即编译一次：方案的 manifest 与范围在同一次提交里产生，页面直接可用。
    _recompile_plan(db, plan)
    if plan.sync_error is not None:
        raise BusinessError(
            str(plan.sync_error["code"]),
            str(plan.sync_error["message"]),
            status_code=409,
        )
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan.plan_code,
        object_name=name,
        operation_type="CREATE",
        detail={
            "plan_code": plan.plan_code,
            "scenario_code": scenario_code,
            "scenario_version_no": version_no,
            "effective": plan.sync_error is None,
        },
    )
    db.commit()
    db.refresh(plan)
    return plan


def update_plan(db: Session, plan_code: str, payload: dict[str, Any]) -> AuditPlan:
    """编辑方案基本资料（只覆盖提交的字段）；不修改规则范围。"""
    plan = plan_or_404(db, plan_code, lock=True)
    if "plan_name" in payload and payload["plan_name"] is not None:
        name = str(payload["plan_name"]).strip()
        if not name:
            raise BusinessError("PLAN_NAME_REQUIRED", "方案名称不能为空", status_code=400)
        plan.plan_name = name
    if "description" in payload:
        description = payload.get("description")
        plan.description = (str(description).strip() or None) if description is not None else None
    if "enabled" in payload and payload["enabled"] is not None:
        plan.enabled = bool(payload["enabled"])
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="UPDATE",
        detail={"plan_code": plan_code},
    )
    db.commit()
    db.refresh(plan)
    return plan


def set_plan_enabled(db: Session, plan_code: str, enabled: bool) -> AuditPlan:
    """启用 / 停用方案。"""
    plan = plan_or_404(db, plan_code, lock=True)
    plan.enabled = enabled
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="ENABLE" if enabled else "DISABLE",
        detail={"plan_code": plan_code},
    )
    db.commit()
    db.refresh(plan)
    return plan


def delete_plan(db: Session, plan_code: str) -> AuditPlan:
    """逻辑删除方案。"""
    plan = plan_or_404(db, plan_code, lock=True)
    if plan.deleted_at is not None:
        raise BusinessError("PLAN_ALREADY_DELETED", "该方案已在已删除列表中", status_code=409)
    plan.deleted_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="DELETE",
        detail={"plan_code": plan_code},
    )
    db.commit()
    db.refresh(plan)
    return plan


def restore_plan(db: Session, plan_code: str) -> AuditPlan:
    """恢复已逻辑删除的方案。"""
    plan = plan_or_404(db, plan_code, lock=True, include_deleted=True)
    if plan.deleted_at is None:
        raise BusinessError("PLAN_NOT_DELETED", "该方案未被删除，无需恢复", status_code=409)
    plan.deleted_at = None
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="RESTORE",
        detail={"plan_code": plan_code},
    )
    db.commit()
    db.refresh(plan)
    return plan


def copy_plan(db: Session, plan_code: str, payload: dict[str, Any]) -> AuditPlan:
    """复制方案为新方案：沿用原方案钉住的场景版本与规则选择，不升到最新版。

    场景门禁与新建方案相同：已删除或已停用的场景不能复制出启用的新方案。
    源方案没有版本，或所钉版本不是已发布状态时，复制失败。
    """
    source = plan_or_404(db, plan_code)
    _published_scenario_or_fail(db, source.scenario_code)
    pinned = db.get(ScenarioVersion, source.scenario_version_id) if source.scenario_version_id else None
    if pinned is None or pinned.status != "PUBLISHED":
        raise BusinessError(
            "PLAN_SCENARIO_VERSION_MISSING",
            "源方案没有可用的已发布场景版本，不能复制",
            status_code=409,
            details={"plan_code": plan_code, "scenario_version_id": source.scenario_version_id},
        )
    name = str(payload.get("plan_name") or "").strip() or f"{source.plan_name} 副本"
    plan = AuditPlan(
        plan_code=public_no("PLAN"),
        plan_name=name,
        scenario_code=source.scenario_code,
        # 溯源：复制出来的方案基于同一版场景。
        scenario_version_id=source.scenario_version_id,
        description=source.description,
        enabled=True,
    # 节点/Sheet/协议版本与计算配置、启停选择随方案一起复制。
        context_json=source.context_json,
    )
    db.add(plan)
    db.flush()
    # 方案自己的配置整份复制：材料 / 字段 / 规则 / 报告章节。
    # **不复制 manifest_json / scope_* / sync_error**：它们是编译产物与结果，
    # 复制过来会指向源方案的产物；下面重新编译一次，产物必然与新方案自己的配置一致。
    plan.config_json = deepcopy(source.config_json)
    plan.context_json = deepcopy(source.context_json)
    for section in ("materials", "fields", "rules", "sections"):
        for item in plan.config_json[section]:
            item["plan_id"] = plan.id
    db.flush()
    _recompile_plan(db, plan)
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan.plan_code,
        object_name=name,
        operation_type="COPY",
        detail={"plan_code": plan.plan_code, "source_plan_code": plan_code},
    )
    db.commit()
    db.refresh(plan)
    return plan


def _plan_config_rows(
    db: Session, plan_id: int
) -> tuple[list[PlanMaterial], list[PlanField], list[PlanRule], list[PlanSection]]:
    """方案自己的配置行，按用户可见的顺序。"""
    return (
        list(sorted(config_store.rows(db, PlanMaterial, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanField, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanRule, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanSection, plan_id), key=lambda item: (item.sort_no, item.id,))),
    )


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
    materials, fields, rules, sections = _plan_config_rows(db, plan.id)
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
        plan.scope_json = None
        plan.scope_hash = None
        plan.sync_error = {"code": exc.code, "message": exc.message}
        return
    _sync_plan_system_rules(db, plan, rules, manifest)
    plan.manifest_json = manifest
    plan.scope_json = {
        **schema,
        "scenario_code": plan.scenario_code,
        "scenario_version_id": plan.scenario_version_id,
        # 材料与字段数量随范围一起固化，页面与任务创建直接读它，不重新解释选择。
        "material_count": len(manifest.get("materials", [])),
        "field_count": len(schema.get("fields", [])),
    }
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


def plan_config_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    """方案的可编辑配置：材料 / 字段 / 全部规则 / 报告章节 / 完整计算定义。

    这是方案页各页签的数据源。节点与选用 Sheet 不在这里 —— 它们是场景级执行结构，
    方案里只读（随 `context_json` 冻结），单独返回供页面展示。
    """
    materials, fields, rules, sections = _plan_config_rows(db, plan.id)
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    calculation_candidates, calculation_enabled = plan_calculation_config(context)
    calculation_source = calculation_candidates
    return {
        "plan_code": plan.plan_code,
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


_ADJUSTMENT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "材料位": (("name", "名称"), ("required", "是否必传"), ("description", "业务说明"), ("source_ref", "来源编号")),
    "字段": (
        ("name", "名称"), ("material_key", "所属材料"), ("value_type", "数据类型"),
        ("extraction_strategy", "抽取策略"), ("structure_schema", "结构 Schema"),
        ("unit", "单位"), ("currency", "币种"), ("artifact_source", "产物来源"),
        ("period_scope", "期间口径"), ("statement_scope", "报表口径"),
        ("required", "是否必抽"), ("instruction", "提取要求"), ("source_ref", "来源编号"),
    ),
    "规则": (
        ("name", "名称"), ("node_code", "所属节点"), ("operator_code", "算子"),
        ("enabled", "是否启用"), ("source_ref", "来源编号"),
    ),
    "报告章节": (
        ("title", "名称"), ("content_instruction", "内容要求"), ("data_refs", "业务数据引用"),
        ("report_code", "报告编号"), ("report_name", "报告名称"),
        ("material_keys", "材料范围"), ("formats", "输出格式"),
        ("section_order", "章节顺序"), ("content_type", "内容类型"),
        ("risk_level_order", "等级顺序"), ("result_sort", "结果排序"), ("config_status", "配置状态"),
    ),
    "计算规则": (
        ("rule_name", "名称"), ("formula", "计算方法"), ("requirement", "业务要求"),
        ("result_type", "结果类型"), ("unit", "单位"), ("currency", "币种"),
        ("parameters", "计算参数"), ("depends_on_result_codes", "依赖计算结果"),
        ("period_policy", "期间要求"), ("statement_scope", "报表口径"),
        ("business_context", "业务补充"), ("remark", "备注"),
        ("status", "配置状态"), ("enabled", "是否启用"), ("source_id", "业务编号"),
    ),
}

_RULE_DEFINITION_LABELS = {
    "inputs": "输入字段",
    "materials": "适用材料",
    "parameters": "判定参数",
    "requirement": "审核要求",
    "applicability": "适用条件",
    "source_rule_key": "来源规则",
    "source_rule_keys": "来源规则集合",
    "source_rule_codes": "来源结果编号",
    "description": "业务说明",
    "check_level": "检查等级",
    "check_operator": "检查方式",
    "risk_level_value": "风险等级",
    "risk_condition": "风险成立条件",
    "rule_role": "规则角色",
    "config_status": "配置状态",
}


def _adjustment_index(
    rows: list[dict[str, Any]], identity: Callable[[dict[str, Any]], Any], category: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(identity(row) or "")
        if not code or code in indexed:
            raise BusinessError("PLAN_ADJUSTMENT_ID_INVALID", f"{category}缺少唯一身份，无法列出旧方案调整", details={"code": code})
        indexed[code] = row
    return indexed


def _rule_adjustment_identity(row: dict[str, Any]) -> str:
    return str((row.get("rule_json") or {}).get("rule_code") or row.get("rule_key") or "")


def _adjustment_items(
    category: str,
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    identity: Callable[[dict[str, Any]], Any],
    name_key: str,
) -> list[dict[str, Any]]:
    baseline = _adjustment_index(baseline_rows, identity, category)
    current = _adjustment_index(current_rows, identity, category)
    items: list[dict[str, Any]] = []
    for code in dict.fromkeys([*baseline, *current]):
        before = baseline.get(code)
        after = current.get(code)
        if before is None or after is None:
            row = after if after is not None else before
            items.append({
                "category": category,
                "code": code,
                "name": row.get(name_key) or code,
                "change": "ADDED" if before is None else "REMOVED",
                "fields": [],
            })
            continue
        system_rule = category == "规则" and bool(before.get("is_system") or after.get("is_system"))
        compared_fields = (("enabled", "是否启用"),) if system_rule else _ADJUSTMENT_FIELDS[category]
        fields = [
            {"label": label, "before": before.get(key), "after": after.get(key)}
            for key, label in compared_fields
            if before.get(key) != after.get(key)
        ]
        if category == "规则" and not system_rule:
            old_definition = dict(before.get("rule_json") or {})
            new_definition = dict(after.get("rule_json") or {})
            for duplicate in ("name", "node", "operator", "enabled"):
                old_definition.pop(duplicate, None)
                new_definition.pop(duplicate, None)
            for key in dict.fromkeys([*old_definition, *new_definition]):
                if old_definition.get(key) != new_definition.get(key):
                    fields.append({
                        "label": _RULE_DEFINITION_LABELS.get(key, f"规则定义 · {key}"),
                        "before": old_definition.get(key),
                        "after": new_definition.get(key),
                    })
        if fields:
            items.append({"category": category, "code": code, "name": after.get(name_key) or code, "change": "MODIFIED", "fields": fields})
    return items


def plan_adjustments_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    """列出旧方案相对其来源场景版本的调整，仅供重新生成时参考。"""
    version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
    if version is None:
        raise BusinessError("PLAN_SCENARIO_VERSION_MISSING", "旧方案的来源场景版本不存在，无法比较调整", status_code=409)
    baseline = manifest_module.editable_config_of(version.manifest_json, namespace=plan.plan_code)
    current = plan_config_view(db, plan)
    groups = (
        ("材料位", baseline["materials"], current["materials"], lambda row: row.get("material_key"), "name"),
        ("字段", baseline["fields"], current["fields"], lambda row: row.get("field_key"), "name"),
        ("规则", baseline["rules"], current["rules"], _rule_adjustment_identity, "name"),
        ("报告章节", baseline["sections"], current["sections"], lambda row: row.get("section_key"), "title"),
    )
    items = [
        item
        for category, original, edited, identity, name_key in groups
        for item in _adjustment_items(category, original, edited, identity, name_key)
    ]
    candidates = calculation_store(baseline["calculation_json"])
    original_calculations = candidates
    items.extend(_adjustment_items(
        "计算规则",
        [
            {"rule_code": str(row["rule_code"]), "rule_name": row.get("rule_name") or row["rule_code"], "enabled": active(row)}
            for row in original_calculations
        ],
        current["calculation_rules"],
        lambda row: row.get("rule_code"),
        "rule_name",
    ))
    return {
        "plan_code": plan.plan_code,
        "plan_name": plan.plan_name,
        "scenario_code": plan.scenario_code,
        "base_scenario_version_no": version.version_no,
        "items": items,
    }


def update_plan_config(db: Session, plan_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """整体替换方案的配置并重新编译。

    整体提交而不是逐条增删：一次提交的是一份完整集合，因此「这次提交之后这个方案到底配了哪些
    材料 / 字段 / 规则 / 章节 / 计算规则」不取决于历史操作顺序。

    归属键（`material_key` / `field_key` / `rule_key` / `section_key`）由调用方给出并原样保留：
    字段与规则的互相引用就是靠它们，重新生成会让已有引用断掉。

    配置写完之后立刻重新编译：方案的范围与「能不能用」在同一次提交里算出来，不会留下一个配置
    已改、范围还是旧的中间态。
    """
    plan = plan_or_404(db, plan_code, lock=True)
    materials = _require_list(payload, "materials")
    fields = _require_list(payload, "fields")
    rules = _require_list(payload, "rules")
    sections = _require_list(payload, "sections")
    calculation_rules = _require_list(payload, "calculation_rules")
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    previous_calculations, previous_calculation_enabled = plan_calculation_config(context)
    calculation_by_code = {str(item["rule_code"]): item for item in previous_calculations}
    calculation_definitions = []
    calculation_enabled: dict[str, bool] = {}
    for index, item in enumerate(calculation_rules, start=1):
        code = _required_key(item, "rule_code", f"第 {index} 条计算规则")
        if code in calculation_enabled or code not in previous_calculation_enabled or type(item.get("enabled")) is not bool:
            raise BusinessError(
                "PLAN_CALC_SELECTION_INVALID",
                f"第 {index} 条计算规则的编号或启停值无效",
                details={"rule_code": code},
            )
        calculation_enabled[code] = item["enabled"]
        definition = deepcopy(calculation_by_code[code])
        for key in (
            "rule_name", "formula", "requirement", "result_code", "result_type", "unit", "currency",
            "period_policy", "statement_scope", "status", "remark", "parameters",
            "depends_on_result_codes", "business_context", "source_id",
        ):
            definition[key] = deepcopy(item[key])
        definition["rule_name"] = _required_key(definition, "rule_name", f"第 {index} 条计算规则")
        definition["parameters"] = _require_list(definition, "parameters")
        calculation_definitions.append(definition)
    if set(calculation_enabled) != set(previous_calculation_enabled):
        raise BusinessError(
            "PLAN_CALC_SELECTION_INVALID",
            "方案必须提交完整的计算规则启停选择",
            details={"missing_rule_codes": sorted(set(previous_calculation_enabled) - set(calculation_enabled))},
        )

    # 来源是已存在配置行的审计事实，整体提交的页面表单不能改写它。
    previous = _plan_config_rows(db, plan.id)
    previous_rules = {item.rule_key: item for item in previous[2]}
    submitted_fields = {item.get("field_key"): item for item in fields}
    for index, item in enumerate(rules, start=1):
        rule_json = item.get("rule_json")
        if not isinstance(rule_json, dict):
            raise BusinessError(
                "PLAN_RULE_JSON_INVALID",
                f"第 {index} 条规则缺少业务定义（rule_json）",
                details={"rule_key": item.get("rule_key")},
            )
        previous_rule = previous_rules.get(item.get("rule_key"))
        if rule_json.get("generated_from") == "FIELD":
            output_fields = [submitted_fields[key] for key in rule_json.get("outputs") or [] if key in submitted_fields]
            item["enabled"] = any(active(field) for field in output_fields)
            if output_fields:
                rule_json["config_status"] = output_fields[0].get("config_status")
        elif rule_json.get("generated_from") == "REPORT":
            item["enabled"] = any(
                active(section) for section in sections
                if str(section.get("report_code") or section["section_key"]) == str(rule_json["report_ref"])
            )
        if item.get("enabled", True) and previous_rule is not None and not active({
            **(previous_rule.rule_json or {}), "enabled": previous_rule.enabled,
        }):
            # 用户明确打开开关即确认启用；仅保存其他内容不改变待确认状态。
            rule_json["config_status"] = "CLEAR"
            if rule_json.get("generated_from") == "FIELD":
                for field_key in rule_json.get("outputs") or []:
                    if field_key in submitted_fields:
                        submitted_fields[field_key]["enabled"] = True
                        submitted_fields[field_key]["config_status"] = "CLEAR"
    previous_fields = {item.field_key: item for item in previous[1]}
    previous_code_owners: dict[str, set[str]] = {}
    for item in previous[1]:
        code = str(item.source_ref or "").strip() or item.field_key
        previous_code_owners.setdefault(code, set()).add(item.field_key)
    field_code_changes: dict[str, str] = {}
    for index, item in enumerate(fields, start=1):
        field_key = _required_key(item, "field_key", f"第 {index} 个字段")
        previous_field = previous_fields.get(field_key)
        if previous_field is None:
            continue
        previous_code = str(previous_field.source_ref or "").strip() or field_key
        next_code = str(item.get("source_ref") or "").strip() or field_key
        if previous_code == next_code:
            continue
        if len(previous_code_owners[previous_code]) > 1 and calculations_referencing_field(
            context.get("calculation_json"), previous_code,
        ):
            raise BusinessError(
                "PLAN_CALC_FIELD_CODE_AMBIGUOUS",
                "原配置的多个字段共用同一编号，无法确定计算引用对应哪个字段，不能自动改写引用",
                details={"field_code": previous_code, "field_keys": sorted(previous_code_owners[previous_code])},
            )
        if previous_code != field_key:
            # 计算按内部字段键引用时，修改业务编号不能改写执行身份。
            field_code_changes[previous_code] = next_code
    origins = [
        {getattr(row, key): row.origin_json for row in rows}
        for rows, key in zip(previous, ("material_key", "field_key", "rule_key", "section_key"), strict=True)
    ]
    extensions = {}
    for rows, model, identity in zip(previous, (PlanMaterial, PlanField, PlanRule, PlanSection),
                                     ("material_key", "field_key", "rule_key", "section_key"), strict=True):
        columns = {column.key for column in model.__table__.columns}
        extensions[model.__name__] = {getattr(row, identity): deepcopy({key: value for key, value in row.data.items() if key not in columns}) for row in rows}
    for items, model, identity in ((materials, PlanMaterial, "material_key"), (fields, PlanField, "field_key"),
                                    (sections, PlanSection, "section_key")):
        for item in items:
            retained = extensions[model.__name__].setdefault(item[identity], {})
            for key in ("business_context", "enabled", "config_status"):
                if key in item:
                    retained[key] = deepcopy(item[key])
    for rows in previous:
        for row in rows:
            config_store.delete(row)
    db.flush()

    for index, item in enumerate(materials, start=1):
        config_store.add(
            db, PlanMaterial(
                plan_id=plan.id,
                material_key=_required_key(item, "material_key", f"第 {index} 个材料"),
                name=_required_key(item, "name", f"第 {index} 个材料"),
                required=bool(item.get("required")),
                description=str(item.get("description") or ""),
                source_ref=item.get("source_ref"),
                origin_json=origins[0].get(item.get("material_key")),
                sort_no=index,
            )
        )
    for index, item in enumerate(fields, start=1):
        field_key = _required_key(item, "field_key", f"第 {index} 个字段")
        if "node_code" in item:
            node_code = item["node_code"]
            if node_code is not None and (not isinstance(node_code, str) or not node_code.strip()):
                raise BusinessError("PLAN_FIELD_NODE_INVALID", "字段抽取节点必须是节点编码或空值", details={"field_key": field_key})
            extensions[PlanField.__name__].setdefault(field_key, {})["node_code"] = node_code
        value_type = _required_key(item, "value_type", f"第 {index} 个字段")
        if value_type not in FORM_VALUE_TYPES:
            raise BusinessError(
                "PLAN_FIELD_TYPE_INVALID",
                f"字段的数据类型“{value_type}”不在允许取值内",
                details={"allowed": sorted(FORM_VALUE_TYPES), "value_type": value_type},
            )
        field_name = _required_key(item, "name", f"第 {index} 个字段")
        field_state = extensions[PlanField.__name__].get(field_key, {})
        producers = [rule for rule in rules if field_key in (rule.get("rule_json") or {}).get("outputs", [])]
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=value_type,
            extraction_strategy=item.get("extraction_strategy"),
            structure_schema=item.get("structure_schema"),
            field_name=field_name,
            enabled=active(field_state)
                    and (not producers or any(active({**(rule.get("rule_json") or {}), "enabled": rule.get("enabled", True)}) for rule in producers)),
        )
        config_store.add(
            db, PlanField(
                plan_id=plan.id,
                field_key=field_key,
                material_key=_required_key(item, "material_key", f"第 {index} 个字段"),
                name=field_name,
                value_type=value_type,
                extraction_strategy=extraction_strategy,
                structure_schema_json=structure_schema,
                unit=item.get("unit") or None,
                currency=item.get("currency") or None,
                artifact_source=item.get("artifact_source") or None,
                # 期间与报表口径只在业务涉及时填写；空值统一落成不适用，满足事实表的非空定位键。
                period_scope=str(item.get("period_scope") or "").strip() or SCOPE_NOT_APPLICABLE,
                statement_scope=str(item.get("statement_scope") or "").strip() or SCOPE_NOT_APPLICABLE,
                required=bool(item.get("required")),
                instruction=str(item.get("instruction") or ""),
                source_ref=item.get("source_ref"),
                origin_json=origins[1].get(item.get("field_key")),
                sort_no=index,
            )
        )
    for index, item in enumerate(rules, start=1):
        rule_json = item.get("rule_json")
        config_store.add(
            db, PlanRule(
                plan_id=plan.id,
                rule_key=_required_key(item, "rule_key", f"第 {index} 条规则"),
                name=_required_key(item, "name", f"第 {index} 条规则"),
                node_code=_required_key(item, "node_code", f"第 {index} 条规则"),
                operator_code=_required_key(item, "operator_code", f"第 {index} 条规则"),
                enabled=bool(item.get("enabled", True)),
                rule_json=rule_json,
                source_ref=item.get("source_ref"),
                origin_json=origins[2].get(item.get("rule_key")),
                sort_no=index,
            )
        )
    for index, item in enumerate(sections, start=1):
        config_store.add(
            db, PlanSection(
                plan_id=plan.id,
                section_key=_required_key(item, "section_key", f"第 {index} 个章节"),
                title=_required_key(item, "title", f"第 {index} 个章节"),
                content_instruction=str(item.get("content_instruction") or ""),
                field_keys=[ref for ref in report_data_refs(item)
                            if ref in {field["field_key"] for field in fields}],
                data_refs=report_data_refs(item),
                report_code=item.get("report_code"),
                report_name=item.get("report_name"),
                material_keys=[str(key) for key in (item.get("material_keys") or [])] or None,
                formats=[str(key) for key in (item.get("formats") or [])] or None,
                section_order=item.get("section_order"),
                content_type=item.get("content_type"),
                result_sort=item.get("result_sort"),
                risk_level_order=list(item.get("risk_level_order") or []),
                config_status=item.get("config_status"),
                origin_json=origins[3].get(item.get("section_key")),
                sort_no=index,
            )
        )
    db.flush()
    for model, identity in ((PlanMaterial, "material_key"), (PlanField, "field_key"), (PlanRule, "rule_key"), (PlanSection, "section_key")):
        for row in config_store.rows(db, model, plan.id):
            for key, value in extensions[model.__name__].get(getattr(row, identity), {}).items():
                setattr(row, key, value)
    calculation_json = remap_calculation_field_codes(calculation_definitions, field_code_changes)
    for calculation in calculation_store(calculation_json):
        code = str(calculation["rule_code"])
        if calculation_enabled[code] and not previous_calculation_enabled[code] and calculation.get("status") == calculation_by_code[code].get("status"):
            calculation["status"] = "CLEAR"
    calculation_json = manifest_module.refresh_calculation_references(calculation_json)
    plan.context_json = {
        **context,
        "calculation_enabled": calculation_enabled,
        "calculation_json": calculation_json,
    }
    # 配置改完立刻重算：范围与「能不能用」在同一次提交里确定。
    _recompile_plan(db, plan)
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="SAVE",
        detail={
            "plan_code": plan_code,
            "material_count": len(materials),
            "field_count": len(fields),
            "rule_count": len(rules),
            "calculation_rule_count": len(calculation_rules),
            "enabled_calculation_rule_count": sum(calculation_enabled.values()),
            "section_count": len(sections),
            "effective": plan.sync_error is None,
        },
    )
    db.commit()
    db.refresh(plan)
    return plan_config_view(db, plan)


def _require_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise BusinessError(
            "PLAN_CONFIG_INVALID",
            f"方案配置的「{key}」必须是一个列表",
            details={"column": key},
        )
    return list(value)


def _required_key(item: dict[str, Any], key: str, where: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise BusinessError(
            "PLAN_CONFIG_INVALID",
            f"{where}缺少必填项「{key}」",
            details={"column": key},
        )
    return value


def save_task_range_as_plan(payload: dict[str, Any]) -> AuditPlan:
    """任务不能反向生成方案。

    方案必须先基于已发布场景版本建立，任务再从方案创建。
    用任务结果补造方案会把执行阶段的数据写回上一阶段。
    """
    raise BusinessError(
        "PLAN_FROM_TASK_FORBIDDEN",
        "不能把审核任务另存为方案。请先选择已发布场景建立方案，再从方案创建任务",
        status_code=409,
        details={"task_no": payload.get("task_no")},
    )
