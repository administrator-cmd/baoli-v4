"""审核方案管理：独立持久化对象，绑定场景版本，保存即生效。

- 方案保存的是「对某个场景版本的规则选择」，不复制维护第二套字段与规则定义。
- 方案只有一份配置，没有草稿/发布之分。每次保存都用 build_task_schema 计算自动依赖
  与有效范围，把显式规则、自动补入规则、未启用规则和任务 schema 快照写进 scope_json。
  算不出来时保存照常成功，原因记在 sync_error，页面显示「待核对」。
- 方案不自动跟随场景升级：切换场景版本是一次显式的保存，被移除的规则选择
  会如实返回给页面，不静默丢弃。
- 已建任务不受方案后续改动影响：任务创建时把 task_schema_json 抓成自己的执行快照。
  旧任务（没有方案关联）显示为「历史任务配置」，可显式另存为方案，但不会被自动迁移。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain.manifest import build_task_schema
from ..errors import BusinessError
from ..models import (
    AuditPlan,
    AuditPlanRule,
    AuditTask,
    ScenarioTemplate,
    ScenarioVersion,
)
from ..domain.identity import public_no
from .operation_log_service import OBJECT_PLAN, record_operation
from . import operation_log_service

logger = logging.getLogger(__name__)

SYSTEM_OPERATORS = {"LLM_EXTRACT", "REPORT_RENDER"}


def plan_or_404(db: Session, plan_code: str, *, lock: bool = False, include_deleted: bool = False) -> AuditPlan:
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


def _scenario_or_404(db: Session, scenario_code: str, *, allow_deleted: bool = False) -> ScenarioTemplate:
    template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))
    if not template or (template.deleted_at is not None and not allow_deleted):
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "审核场景不存在或已被删除",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    return template


def _scenario_version_of(db: Session, scenario_code: str, version_no: int | None = None) -> ScenarioVersion:
    """取得场景的当前配置版本。

    页面上已没有“版本”概念（场景配置只有一份、保存即生效），因此 version_no 可以
    不传：此时取该场景最新的已发布版本，也就是用户刚保存的配置。仍保留按版本号
    精确查询的能力，供历史方案解析它自己绑定的那个版本。
    """
    statement = select(ScenarioVersion).where(
        ScenarioVersion.scenario_code == scenario_code,
        ScenarioVersion.status == "PUBLISHED",
    )
    if version_no is None:
        statement = statement.order_by(ScenarioVersion.version_no.desc()).limit(1)
    else:
        statement = statement.where(ScenarioVersion.version_no == version_no)
    version = db.scalar(statement)
    if not version:
        raise BusinessError(
            "SCENARIO_VERSION_NOT_FOUND",
            "该场景还没有可用的配置：请先在场景配置中完成材料、字段与规则并保存",
            status_code=404,
            details={"scenario_code": scenario_code, "version_no": version_no},
        )
    return version


def _business_rule_keys(manifest: dict[str, Any]) -> set[str]:
    return {
        rule["rule_key"]
        for rule in manifest.get("rules", [])
        if rule.get("operator") not in SYSTEM_OPERATORS and rule.get("enabled", True)
    }


def _selection_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    selected = db.scalars(
        select(AuditPlanRule).where(AuditPlanRule.plan_id == plan.id).order_by(AuditPlanRule.rule_key)
    ).all()
    version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
    rule_keys = [item.rule_key for item in selected]
    rule_names: dict[str, str] = {}
    rule_nodes: dict[str, str] = {}
    materials: list[dict[str, Any]] = []
    field_count = 0
    if version:
        manifest = version.manifest_json
        rule_names = {rule["rule_key"]: rule["name"] for rule in manifest.get("rules", [])}
        rule_nodes = {rule["rule_key"]: rule["node"] for rule in manifest.get("rules", [])}
        materials = [
            {"material_key": item["material_key"], "name": item["name"], "required": bool(item.get("required"))}
            for item in manifest.get("materials", [])
        ]
        field_count = len(manifest.get("fields", []))
    return {
        "plan_code": plan.plan_code,
        "plan_name": plan.plan_name,
        "scenario_code": plan.scenario_code,
        "description": plan.description,
        "enabled": plan.enabled,
        "deleted": plan.deleted_at is not None,
        # 范围是否已生效：sync_error 非空表示最近一次保存没有算出可用范围，
        # 页面据此显示「待核对 + 原因」，不把不完整配置说成已生效。
        "effective": plan.sync_error is None and plan.scope_json is not None,
        "sync_error": plan.sync_error,
        "scope_hash": plan.scope_hash,
        "base_scenario_version": (
            {
                "version_no": version.version_no,
                "status": version.status,
                "published_at": version.published_at,
                "scenario_name": version.scenario_name,
            }
            if version
            else None
        ),
        "selected_rule_keys": rule_keys,
        "selected_rules": [
            {
                "rule_key": key,
                "name": rule_names.get(key, key),
                "node": rule_nodes.get(key),
                "missing": version is not None and key not in rule_names,
            }
            for key in rule_keys
        ],
        "materials": materials,
        "field_count": field_count,
        "updated_at": plan.updated_at,
    }


def _effective_predicate():
    """「范围已生效」的 SQL 判据：sync_error 为空且有 scope_json。

    注意：sync_error 是 JSON 列，Python 的 None 会存成 **JSON 字面量 null**，
    MySQL 里 `sync_error IS NULL` 对它返回假（只有 SQL NULL 才为真），
    用它做过滤会把全部生效方案都排除掉。必须按 JSON 类型判断。
    scope_json 同理，用 JSON_TYPE 而不是 IS NOT NULL。
    """
    return (func.json_type(AuditPlan.sync_error) == "NULL") & (func.json_type(AuditPlan.scope_json) != "NULL")


def list_plans_page(
    db: Session,
    *,
    name: str | None,
    scenario_code: str | None,
    enabled: bool | None,
    effective: bool | None,
    deleted: bool,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    conditions = [AuditPlan.deleted_at.is_not(None) if deleted else AuditPlan.deleted_at.is_(None)]
    if name:
        conditions.append(AuditPlan.plan_name.like(f"%{name.strip()}%"))
    if scenario_code:
        conditions.append(AuditPlan.scenario_code == scenario_code)
    if enabled is not None:
        conditions.append(AuditPlan.enabled.is_(enabled))
    if effective is not None:
        is_effective = _effective_predicate()
        conditions.append(is_effective if effective else ~is_effective)

    total = db.scalar(select(func.count()).select_from(AuditPlan).where(*conditions)) or 0
    plans = db.scalars(
        select(AuditPlan)
        .where(*conditions)
        .order_by(AuditPlan.updated_at.desc(), AuditPlan.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    plan_ids = [item.id for item in plans]
    rule_counts: dict[int, int] = {}
    scenario_names: dict[str, str] = {}
    if plan_ids:
        for plan_id, count in db.execute(
            select(AuditPlanRule.plan_id, func.count())
            .where(AuditPlanRule.plan_id.in_(plan_ids))
            .group_by(AuditPlanRule.plan_id)
        ).all():
            rule_counts[int(plan_id)] = int(count)
        codes = {plan.scenario_code for plan in plans}
        for code, scenario_name in db.execute(
            select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name).where(ScenarioTemplate.scenario_code.in_(codes))
        ).all():
            scenario_names[code] = scenario_name

    items = []
    for plan in plans:
        scope = plan.scope_json if isinstance(plan.scope_json, dict) else {}
        base_version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
        items.append(
            {
                "plan_code": plan.plan_code,
                "plan_name": plan.plan_name,
                "scenario_code": plan.scenario_code,
                "scenario_name": scenario_names.get(plan.scenario_code, plan.scenario_code),
                "description": plan.description,
                "enabled": plan.enabled,
                "deleted": plan.deleted_at is not None,
                "base_scenario_version_no": base_version.version_no if base_version else None,
                "effective": plan.sync_error is None and plan.scope_json is not None,
                "sync_error": plan.sync_error,
                "enabled_rule_count": len(scope.get("enabled_rule_keys", [])),
                "explicit_rule_count": rule_counts.get(plan.id, 0),
                "updated_at": plan.updated_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def list_plan_options(db: Session, *, scenario_code: str | None) -> list[dict[str, Any]]:
    """下拉选项：启用且未删除、且范围已生效的方案。"""
    conditions = [
        AuditPlan.deleted_at.is_(None),
        AuditPlan.enabled.is_(True),
        _effective_predicate(),
    ]
    if scenario_code:
        conditions.append(AuditPlan.scenario_code == scenario_code)
    plans = db.scalars(select(AuditPlan).where(*conditions).order_by(AuditPlan.plan_name)).all()
    result = []
    for plan in plans:
        version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
        result.append(
            {
                "plan_code": plan.plan_code,
                "plan_name": plan.plan_name,
                "scenario_code": plan.scenario_code,
                "scenario_version_no": version.version_no if version else None,
            }
        )
    return result


def create_plan(db: Session, payload: dict[str, Any]) -> AuditPlan:
    name = str(payload.get("plan_name") or "").strip()
    if not name:
        raise BusinessError("PLAN_NAME_REQUIRED", "方案名称不能为空", status_code=400)
    scenario_code = str(payload.get("scenario_code") or "")
    _scenario_or_404(db, scenario_code)
    plan = AuditPlan(
        plan_code=public_no("PLAN"),
        plan_name=name,
        scenario_code=scenario_code,
        scenario_version_id=None,
        description=(str(payload.get("description")).strip() or None) if payload.get("description") else None,
        enabled=bool(payload.get("enabled", True)),
        sync_error={"code": "PLAN_RULE_EMPTY", "message": "还没有选择任何规则。"},
    )
    db.add(plan)
    db.flush()
    # 创建时先按场景当前生效配置绑定版本，再按空选择保存一次：
    # 与页面上「保存」走同一条路径，不另造一套初始化逻辑。
    _sync_plan_scope(db, plan, requested=None)
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan.plan_code,
        object_name=name,
        operation_type="CREATE",
        detail={"plan_code": plan.plan_code, "scenario_code": scenario_code},
    )
    db.commit()
    db.refresh(plan)
    return plan


def update_plan(db: Session, plan_code: str, payload: dict[str, Any]) -> AuditPlan:
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
    source = plan_or_404(db, plan_code)
    name = str(payload.get("plan_name") or "").strip() or f"{source.plan_name} 副本"
    plan = AuditPlan(
        plan_code=public_no("PLAN"),
        plan_name=name,
        scenario_code=source.scenario_code,
        scenario_version_id=source.scenario_version_id,
        description=source.description,
        enabled=True,
        sync_error=source.sync_error,
        scope_hash=source.scope_hash,
        scope_json=source.scope_json,
    )
    db.add(plan)
    db.flush()
    for item in db.scalars(select(AuditPlanRule).where(AuditPlanRule.plan_id == source.id)).all():
        db.add(AuditPlanRule(plan_id=plan.id, rule_key=item.rule_key))
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


def _sync_plan_scope(db: Session, plan: AuditPlan, *, requested: list[str] | None) -> list[str]:
    """按当前选择重算方案范围并写进 scope_json；返回被移除的规则名。

    保存即生效：这里既写选择也写算好的范围。范围算不出来（没选规则、场景未生效等）
    不阻断保存——配置过程中必然不完整，页面要能存下来继续改。此时 sync_error 记真实
    原因、scope_json 置空，方案显示为「待核对」，且不能被任务引用。
    """
    scenario_code = plan.scenario_code
    version_no = None
    dropped: list[str] = []
    previous_names: dict[str, str] = {}
    if plan.scenario_version_id:
        previous = db.get(ScenarioVersion, plan.scenario_version_id)
        if previous:
            previous_names = {rule["rule_key"]: rule["name"] for rule in previous.manifest_json.get("rules", [])}
    try:
        version = _scenario_version_of(db, scenario_code, version_no)
    except BusinessError as exc:
        # 场景还没有可用配置：方案可以存下来，但范围算不出来，如实记录原因。
        plan.scenario_version_id = None
        plan.scope_json = None
        plan.scope_hash = None
        plan.sync_error = {"code": exc.code, "message": exc.message}
        return dropped
    plan.scenario_version_id = version.id

    available = _business_rule_keys(version.manifest_json)
    if requested is None:
        requested = [item.rule_key for item in db.scalars(select(AuditPlanRule).where(AuditPlanRule.plan_id == plan.id)).all()]
    normalized: list[str] = []
    for key in requested:
        key = str(key)
        if key not in available:
            dropped.append(previous_names.get(key, key))
            continue
        if key not in normalized:
            normalized.append(key)
    for item in db.scalars(select(AuditPlanRule).where(AuditPlanRule.plan_id == plan.id)).all():
        db.delete(item)
    db.flush()
    for key in sorted(normalized):
        db.add(AuditPlanRule(plan_id=plan.id, rule_key=key))
    db.flush()

    try:
        schema = build_task_schema(version.manifest_json, normalized, allow_empty=False)
    except BusinessError as exc:
        plan.scope_json = None
        plan.scope_hash = None
        plan.sync_error = {"code": exc.code, "message": exc.message}
        return dropped

    scope_json = {
        **schema,
        "scenario_code": scenario_code,
        "scenario_version_id": version.id,
        "scenario_version_no": version.version_no,
        "scenario_name": version.scenario_name,
        # 材料与字段数量随范围一起固化，页面与任务创建直接读它，不重新解释选择。
        "material_count": len(version.manifest_json.get("materials", [])),
        "field_count": len(schema.get("fields", [])),
    }
    scope_hash = operation_log_service.normalized_request_hash(
        {
            "scenario_version_id": version.id,
            "enabled_rule_keys": schema["enabled_rule_keys"],
        }
    )
    plan.scope_json = scope_json
    plan.scope_hash = scope_hash
    plan.sync_error = None
    return dropped


def save_plan(db: Session, plan_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """保存方案：更新规则选择并立即重算范围（保存即生效）。

    切换场景版本时，不在新版本中的旧选择会被移除，并通过 dropped_rule_keys 如实返回；
    绝不静默地把旧选择算作仍然有效。
    """
    plan = plan_or_404(db, plan_code, lock=True)
    version_no = payload.get("scenario_version_no")
    if version_no is not None:
        version = _scenario_version_of(db, plan.scenario_code, int(version_no))
        plan.scenario_version_id = version.id
    requested = payload.get("rule_keys")
    dropped = _sync_plan_scope(db, plan, requested=None if requested is None else [str(key) for key in requested])
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan_code,
        object_name=plan.plan_name,
        operation_type="SAVE",
        detail={
            "plan_code": plan_code,
            "scenario_version_no": (db.get(ScenarioVersion, plan.scenario_version_id).version_no if plan.scenario_version_id else None),
            "rule_count": len(payload.get("rule_keys") or []),
            "dropped_rule_keys": dropped,
            "effective": plan.sync_error is None,
        },
    )
    db.commit()
    db.refresh(plan)
    view = _selection_view(db, plan)
    view["dropped_rule_keys"] = dropped
    return view


def scope_preview(db: Session, *, scenario_code: str, version_no: int | None, rule_keys: list[str] | None) -> dict[str, Any]:
    """规则范围预览：显式选择 + 自动补入依赖 + 未启用规则 + 材料与字段摘要。

    与发布使用同一个 build_task_schema，页面看到的范围就是任务将执行的范围。
    version_no 不传时按该场景当前生效的配置预览（页面已无版本选择）。
    """
    version = _scenario_version_of(db, scenario_code, int(version_no) if version_no is not None else None)
    manifest = version.manifest_json
    # 三个语义必须区分：None=未选择过（历史草稿，视为全部启用）；[]=用户显式地一条都没勾
    # （系统派生规则仍由后端补入，业务规则全部显示为未启用、可勾选）；非空=显式选择。
    # 旧实现把 [] 降级成 None，规则表全部显示为“自动依赖”且勾选框禁用，用户永远选不上第一条规则。
    schema = build_task_schema(
        manifest,
        None if rule_keys is None else list(rule_keys),
        allow_empty=rule_keys is not None and len(rule_keys) == 0,
    )
    rule_names = {rule["rule_key"]: rule["name"] for rule in manifest.get("rules", [])}
    rule_nodes = {rule["rule_key"]: rule["node"] for rule in manifest.get("rules", [])}
    auto_reasons = schema.get("auto_included_reasons", {})
    explicit = set(rule_keys or [])
    enabled = set(schema["enabled_rule_keys"])
    rows = []
    for key in sorted(enabled):
        selection = "EXPLICIT" if key in explicit else "AUTO"
        rows.append(
            {
                "rule_key": key,
                "name": rule_names.get(key, key),
                "node": rule_nodes.get(key),
                "selection": selection,
                "system": rule_nodes.get(key) in {"FIELD_EXTRACTION", "REPORT_OUTPUT"} and key not in explicit,
                # 自动纳入的真实原因：报告生成 / 风险来源 / 字段抽取；显式选择与未启用为空。
                "reason": auto_reasons.get(key) if selection == "AUTO" else None,
            }
        )
    for key in sorted(schema["unenabled_rule_keys"]):
        rows.append(
            {
                "rule_key": key,
                "name": rule_names.get(key, key),
                "node": rule_nodes.get(key),
                "selection": "UNENABLED",
                "system": False,
                "reason": None,
            }
        )
    return {
        "scenario_code": scenario_code,
        "version_no": version.version_no,
        "rows": rows,
        "explicit_rule_keys": sorted(explicit & enabled),
        "auto_included_rule_keys": list(schema["auto_included_rule_keys"]),
        "unenabled_rule_keys": list(schema["unenabled_rule_keys"]),
        "materials": [
            {"material_key": item["material_key"], "name": item["name"], "required": bool(item.get("required")), "material_type": item.get("material_type", "")}
            for item in manifest.get("materials", [])
        ],
        "field_count": len(schema["fields"]),
        "unconfigured_check_nodes": list(schema.get("unconfigured_check_nodes", [])),
    }


def save_task_range_as_plan(db: Session, payload: dict[str, Any]) -> AuditPlan:
    """把旧任务的执行范围显式另存为方案。

    显式规则 = 任务启用规则 − 后端自动补入的规则；自动依赖由保存时重新计算。
    这是用户主动动作，不迁移、不改写原任务与历史结果。
    """
    task_no = str(payload.get("task_no") or "")
    name = str(payload.get("plan_name") or "").strip()
    if not name:
        raise BusinessError("PLAN_NAME_REQUIRED", "方案名称不能为空", status_code=400)
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no))
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    schema = task.task_schema_json
    explicit = sorted(set(schema.get("enabled_rule_keys", [])) - set(schema.get("auto_included_rule_keys", [])))
    scenario_version = db.get(ScenarioVersion, task.scenario_version_id)
    if scenario_version is None:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    plan = AuditPlan(
        plan_code=public_no("PLAN"),
        plan_name=name,
        scenario_code=scenario_version.scenario_code,
        scenario_version_id=scenario_version.id,
        description=f"由历史任务 {task_no} 的执行范围另存",
        enabled=True,
        sync_error={"code": "PLAN_RULE_EMPTY", "message": "还没有选择任何规则。"},
    )
    db.add(plan)
    db.flush()
    available = _business_rule_keys(scenario_version.manifest_json)
    requested = [key for key in explicit if key in available]
    _sync_plan_scope(db, plan, requested=requested)
    record_operation(
        db,
        object_type=OBJECT_PLAN,
        object_no=plan.plan_code,
        object_name=name,
        operation_type="SAVE_FROM_TASK",
        detail={"plan_code": plan.plan_code, "task_no": task_no, "rule_count": len(requested)},
    )
    db.commit()
    db.refresh(plan)
    return plan
