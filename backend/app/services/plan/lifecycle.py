"""方案创建、复制、启停、删除与恢复；拒绝任务反向生成方案。"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain.identity import public_no
from ...entity.plans import AuditPlan
from ...entity.scenarios import ScenarioVersion
from ..system.operation_log import record_operation, require_idempotent_replay
from ..system.operation_types import OBJECT_PLAN
from .compilation import _load_manifest_into_plan, _recompile_plan
from .lookup import _published_scenario_or_fail, _published_scenario_version_or_fail, plan_or_404


def create_plan(db: Session, payload: dict[str, Any]) -> AuditPlan:
    """同内容、同幂等键返回原方案；唯一约束裁决同时到达的请求。"""
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        raise BusinessError("OPERATION_IDEMPOTENCY_REQUIRED", "新建方案必须提供幂等标识", status_code=400)
    request = {name: value for name, value in payload.items() if name != "idempotency_key"}
    request["plan_name"] = str(request.get("plan_name") or "").strip()
    request["description"] = str(request.get("description") or "").strip() or None
    replay, request_hash = require_idempotent_replay(
        db, object_type=OBJECT_PLAN, operation_type="CREATE", idempotency_key=key, request=request,
    )
    if replay is not None:
        return plan_or_404(db, replay.object_no, include_deleted=True)
    try:
        return _create_plan(db, request, idempotency_key=key, request_hash=request_hash)
    except IntegrityError:
        # 冲突事务中的方案、配置、日志一起回滚；只回放已提交的同一请求。
        db.rollback()
        replay, _ = require_idempotent_replay(
            db, object_type=OBJECT_PLAN, operation_type="CREATE", idempotency_key=key, request=request,
        )
        if replay is None:
            raise
        return plan_or_404(db, replay.object_no, include_deleted=True)


def _create_plan(db: Session, payload: dict[str, Any], *, idempotency_key: str, request_hash: str) -> AuditPlan:
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
        idempotency_key=idempotency_key,
        request_hash=request_hash,
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
