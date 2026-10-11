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
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..repositories import configuration_store as config_store

from ..domain import field_strategy, manifest as manifest_module
from ..domain.audit_vocab import SCOPE_NOT_APPLICABLE
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
from ..domain.identity import public_no, stable_key
from .operation_log_service import record_operation
from .operation_types import OBJECT_PLAN
from . import operation_log_service
# 草稿与方案共用原始计算定义读取口径，不转换旧编译产物。
from .scenario_admin_service import (
    _calculation_store, _calculation_view_rows,
    _calculations_referencing_field, _remap_calculation_field_codes,
)

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
    version = _latest_published_version_of(db, scenario_code)
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
    template = _scenario_or_404(db, scenario_code)
    if not template.enabled:
        raise BusinessError(
            "SCENARIO_DISABLED",
            f"场景“{template.scenario_name}”已停用，不能用于创建方案",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    latest = _latest_published_version_of(db, scenario_code)
    if latest is None:
        raise BusinessError(
            "SCENARIO_NOT_PUBLISHED",
            f"场景“{template.scenario_name}”还没有已发布的版本，不能创建方案：请先由管理员发布场景",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
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


def _latest_published_version_of(db: Session, scenario_code: str) -> ScenarioVersion | None:
    return db.scalar(
        select(ScenarioVersion)
        .where(ScenarioVersion.scenario_code == scenario_code, ScenarioVersion.status == "PUBLISHED")
        .order_by(ScenarioVersion.version_no.desc())
        .limit(1)
    )


def _selection_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    """方案视图。

    规则的「选没选」就是 `plan_rule.enabled` —— 方案自己持有配置，规则、材料、字段都从
    方案自己的表读，不再从场景版本读，也不再有「系统派生的规则不能选」这条限制。
    """
    rules = sorted(config_store.rows(db, PlanRule, plan.id), key=lambda item: (item.sort_no, item.rule_key,))
    materials = sorted(config_store.rows(db, PlanMaterial, plan.id), key=lambda item: (item.sort_no, item.id,))
    field_count = len(plan.config_json["fields"])
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    calculation_candidates, calculation_enabled = _plan_calculation_config(context)
    calculation_source = calculation_candidates
    # 场景版本只是溯源：这份方案从哪一版复制而来，执行与范围都不读它。
    version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
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
        # 方案自己的全部规则与启停：这就是「这个方案跑哪些规则」的唯一来源。
        "rules": [
            {
                "rule_key": item.rule_key,
                "name": item.name,
                "node": item.node_code,
                "enabled": item.enabled,
            }
            for item in rules if not item.rule_json.get("calculation_ref")
        ],
        "calculation_rules": [
            {
                "rule_code": item["rule_code"],
                "name": item.get("rule_name") or item["rule_code"],
                "enabled": calculation_enabled[str(item["rule_code"])],
            }
            for item in calculation_source
        ],
        "materials": [
            {"material_key": item.material_key, "name": item.name, "required": bool(item.required)}
            for item in materials
        ],
        "field_count": int(field_count),
        "updated_at": plan.updated_at,
    }


def _effective_predicate():
    """「范围已生效」的 SQL 判据：sync_error 为空且有 scope_json。

    注意：sync_error 是 JSON 列，Python 的 None 会存成 JSON 字面量 null，
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
    """方案分页列表（真实后端筛选与 total）。"""
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
    # 先只按主键分页排序，再按主键读取完整方案。
    # AuditPlan 含 manifest/context/scope 等大 JSON 列；直接 SELECT AuditPlan
    # 并 ORDER BY updated_at 会把整行带入 MySQL filesort，方案保存后列表容易报
    # 1038 Out of sort memory。轻量主键排序后再回表，避免大 JSON 参与排序。
    ordered_plan_ids = db.scalars(
        select(AuditPlan.id)
        .where(*conditions)
        .order_by(AuditPlan.updated_at.desc(), AuditPlan.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    if not ordered_plan_ids:
        plans = []
    else:
        plans_by_id = {
            plan.id: plan
            for plan in db.scalars(select(AuditPlan).where(AuditPlan.id.in_(ordered_plan_ids))).all()
        }
        plans = [plans_by_id[plan_id] for plan_id in ordered_plan_ids]

    plan_ids = [item.id for item in plans]
    rule_counts: dict[int, int] = {}
    scenario_names: dict[str, str] = {}
    if plan_ids:
        rule_counts = {plan.id: sum(bool(item["enabled"]) for item in plan.config_json["rules"]) for plan in plans}
        codes = {plan.scenario_code for plan in plans}
        for code, scenario_name in db.execute(
            select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name).where(ScenarioTemplate.scenario_code.in_(codes))
        ).all():
            scenario_names[code] = scenario_name

    items = []
    for plan in plans:
        scope = plan.scope_json if isinstance(plan.scope_json, dict) else {}
        context = plan.context_json if isinstance(plan.context_json, dict) else {}
        _, _, calculation_enabled = _plan_calculation_config(context)
        base_version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
        items.append(
            {
                "plan_code": plan.plan_code,
                "plan_name": plan.plan_name,
                "scenario_code": plan.scenario_code,
                "scenario_name": (
                    scenario_names[plan.scenario_code] if plan.scenario_code in scenario_names
                    else base_version.scenario_name if base_version else "来源场景已删除，未记录场景名称"
                ),
                "description": plan.description,
                "enabled": plan.enabled,
                "deleted": plan.deleted_at is not None,
                "base_scenario_version_no": base_version.version_no if base_version else None,
                "effective": plan.sync_error is None and plan.scope_json is not None,
                "sync_error": plan.sync_error,
                "enabled_rule_count": len(scope.get("enabled_rule_keys", [])),
                "explicit_rule_count": rule_counts.get(plan.id, 0) + sum(calculation_enabled.values()),
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
    # 下拉只需要四个轻量字段。直接 select(AuditPlan) 会把多个大 JSON 列
    # 带入 ORDER BY plan_name，在 MySQL 小 sort buffer 下触发 1038。
    rows = db.execute(
        select(
            AuditPlan.plan_code,
            AuditPlan.plan_name,
            AuditPlan.scenario_code,
            ScenarioVersion.version_no,
        )
        .outerjoin(ScenarioVersion, ScenarioVersion.id == AuditPlan.scenario_version_id)
        .where(*conditions)
        .order_by(AuditPlan.plan_name, AuditPlan.id)
    ).all()
    return [
        {
            "plan_code": plan_code,
            "plan_name": plan_name,
            "scenario_code": scenario_code,
            "scenario_version_no": version_no,
        }
        for plan_code, plan_name, scenario_code, version_no in rows
    ]


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
    # 节点与选用 Sheet 是场景级执行结构：方案里只读，随方案冻结一份供编译使用。
    # 计算定义沿用场景版本，方案单独保存逐条启停选择。
    calculation_candidates = _calculation_store(config["calculation_json"])
    calculation_source = calculation_candidates
    plan.context_json = {
        "nodes": config["nodes"],
        "selected_sheets": config["sheets"],
        "profile_version": manifest.get("profile_version"),
        "scenario_schema": manifest.get("scenario_schema"),
        "source_kind": manifest.get("source_kind"),
        "calculation_json": config["calculation_json"],
        "calculation_enabled": {
            str(item["rule_code"]): item.get("status") != "DISABLED"
            for item in calculation_source
        },
    }


def _plan_calculation_config(
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    candidates = _calculation_store(context.get("calculation_json"))
    source = candidates
    codes = [str(item.get("rule_code") or "") for item in source]
    if not all(codes) or len(set(codes)) != len(codes):
        raise BusinessError("PLAN_CALC_CODE_INVALID", "方案的计算规则编号缺失或重复，无法确定启停范围")
    saved = context.get("calculation_enabled")
    if saved is None:
        # 旧方案尚未保存独立启停选择时，沿用它复制自场景版本的明确状态。
        enabled = {str(item["rule_code"]): item.get("status") != "DISABLED" for item in source}
    elif isinstance(saved, dict) and set(saved) == set(codes) and all(type(value) is bool for value in saved.values()):
        enabled = dict(saved)
    else:
        raise BusinessError("PLAN_CALC_SELECTION_INVALID", "方案的计算规则启停选择与规则集合不一致")
    return candidates, enabled


def _selected_calculations(
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates, enabled = _plan_calculation_config(context)
    selected_candidates = [
        {
            **item,
            "enabled": enabled[str(item["rule_code"])],
            "status": (
                item.get("status") if item.get("status") != "DISABLED" else "CLEAR"
            ) if enabled[str(item["rule_code"])] else "DISABLED",
        }
        for item in candidates
    ]
    return selected_candidates


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
            sheets=list(context.get("selected_sheets") or []),
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
        calculation_candidates = _selected_calculations(context)
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
        )
        manifest["source_kind"] = str(context.get("source_kind") or "FORM")
        canonical = json.dumps(
            {key: value for key, value in manifest.items() if key != "manifest_hash"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # None = 全部启用：方案的启停已经编译进 manifest 的规则。
        schema = build_task_schema(manifest, None, allow_empty=True)
    except BusinessError as exc:
        plan.manifest_json = None
        plan.manifest_hash = None
        plan.scope_json = None
        plan.scope_hash = None
        plan.sync_error = {"code": exc.code, "message": exc.message}
        return
    _sync_plan_system_rules(db, plan, rules, manifest)
    plan.manifest_json = manifest
    # 编译产物自带 manifest_hash（由内容算出，不由人维护）。
    plan.manifest_hash = str(manifest.get("manifest_hash") or "")
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
    """方案的可编辑配置：材料 / 字段 / 全部规则 / 报告章节 / 计算规则启停。

    这是方案页各页签的数据源。节点与选用 Sheet 不在这里 —— 它们是场景级执行结构，
    方案里只读（随 `context_json` 冻结），单独返回供页面展示。
    """
    materials, fields, rules, sections = _plan_config_rows(db, plan.id)
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    calculation_candidates, calculation_enabled = _plan_calculation_config(context)
    calculation_source = calculation_candidates
    calculation_views = {
        item["rule_code"]: item for item in _calculation_view_rows(context.get("calculation_json"))
    }
    return {
        "plan_code": plan.plan_code,
        "materials": [
            {
                "material_key": item.material_key,
                "name": item.name,
                "required": item.required,
                "description": item.description,
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
                "enabled": item.enabled,
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
        # 只读：本方案会跑哪些节点、用了哪些 Sheet。页面只展示，不改。
        "nodes": list(context.get("nodes") or []),
        "selected_sheets": list(context.get("selected_sheets") or []),
        "calculation_rules": [
            {
                "rule_code": item["rule_code"],
                "rule_name": item.get("rule_name") or item["rule_code"],
                "formula": item.get("formula", ""),
                "requirement": item.get("requirement", ""),
                "business_context": item.get("business_context", {}),
                "node_code": item.get("node_code") or item.get("node_instance_code"),
                "enabled": calculation_enabled[str(item["rule_code"])],
                "origin": calculation_views[item["rule_code"]]["origin"],
                "parameter_sources": calculation_views[item["rule_code"]]["parameter_sources"],
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
        ("title", "名称"), ("content_instruction", "内容要求"), ("field_keys", "绑定字段"),
        ("report_code", "报告编号"), ("report_name", "报告名称"),
        ("material_keys", "材料范围"), ("formats", "输出格式"),
        ("section_order", "章节顺序"), ("content_type", "内容类型"),
        ("risk_level_order", "等级顺序"), ("result_sort", "结果排序"), ("config_status", "配置状态"),
    ),
    "计算规则": (("enabled", "是否启用"),),
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
    "config_status": "配置状态",
    "quality_supported": "能力状态",
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
    candidates = _calculation_store(baseline["calculation_json"])
    original_calculations = candidates
    items.extend(_adjustment_items(
        "计算规则",
        [
            {"rule_code": str(row["rule_code"]), "rule_name": row.get("rule_name") or row["rule_code"], "enabled": row.get("status") != "DISABLED"}
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
    _, _, previous_calculation_enabled = _plan_calculation_config(context)
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
    if set(calculation_enabled) != set(previous_calculation_enabled):
        raise BusinessError(
            "PLAN_CALC_SELECTION_INVALID",
            "方案必须提交完整的计算规则启停选择",
            details={"missing_rule_codes": sorted(set(previous_calculation_enabled) - set(calculation_enabled))},
        )

    # 来源是已存在配置行的审计事实，整体提交的页面表单不能改写它。
    previous = _plan_config_rows(db, plan.id)
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
        if len(previous_code_owners[previous_code]) > 1 and _calculations_referencing_field(
            context.get("calculation_json"), previous_code,
        ):
            raise BusinessError(
                "PLAN_CALC_FIELD_CODE_AMBIGUOUS",
                "原配置的多个字段共用同一编号，无法确定计算引用对应哪个字段，不能自动改写引用",
                details={"field_code": previous_code, "field_keys": sorted(previous_code_owners[previous_code])},
            )
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
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=value_type,
            extraction_strategy=item.get("extraction_strategy"),
            structure_schema=item.get("structure_schema"),
            field_name=field_name,
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
        if not isinstance(rule_json, dict):
            raise BusinessError(
                "PLAN_RULE_JSON_INVALID",
                f"第 {index} 条规则缺少业务定义（rule_json）",
                details={"rule_key": item.get("rule_key")},
            )
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
                field_keys=[str(key) for key in (item.get("field_keys") or [])],
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
    plan.context_json = {
        **context,
        "calculation_enabled": calculation_enabled,
        "calculation_json": _remap_calculation_field_codes(context.get("calculation_json"), field_code_changes),
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
