from typing import Any

from sqlalchemy.orm import Session

from ..domain.identity import public_no
from ..common.exceptions import BusinessError
from ..entities import AuditTask
from ..repositories import plan_repository, scenario_repository, task_repository
from .audit.interrupt import resume_task_creation_interrupt, start_audit_interrupt
from .operation_log_service import require_idempotent_replay
from .operation_types import OBJECT_PLAN
from .plan_service import plan_or_404
from .task_service import (
    claim_task_command,
    create_task_from_plan,
    finish_task_command,
    record_failed_task_command,
    task_of_command,
)


def create_from_plan(db: Session, plan_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """从方案创建任务的应用服务：统一处理幂等、门禁、事务和冻结快照。"""
    request_payload = {
        "plan_code": plan_code,
        "task_name": payload["task_name"],
        "remark": payload.get("remark"),
        "expected_scope_hash": payload["expected_scope_hash"],
    }
    existing, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_PLAN,
        operation_type="TASK_CREATE",
        idempotency_key=payload["idempotency_key"],
        request=request_payload,
    )
    if existing is not None:
        return _replay_view(db, existing)

    task_no = public_no("TASK")
    command, reused = claim_task_command(
        db,
        object_type=OBJECT_PLAN,
        operation_type="TASK_CREATE",
        idempotency_key=payload["idempotency_key"],
        request=request_payload,
        request_hash=request_hash,
        task_no=task_no,
    )
    if reused:
        task = task_of_command(db, command)
        detail = {"task_no": task.task_no, "plan_code": plan_code}
        plan_name = task.task_schema_json["plan_name"]
        if command.status == "FAILED":
            record_failed_task_command(
                db, command, object_no=plan_code, object_name=plan_name, detail=detail
            )
            raise BusinessError(
                "TASK_CREATE_PREVIOUSLY_FAILED",
                f"该幂等标识对应的创建任务命令此前执行失败：{command.failure_message or '原因未记录'}。",
                status_code=409,
                details={
                    "operation_no": command.operation_no,
                    "task_no": task.task_no,
                    "failure_code": command.failure_code,
                },
            )
        if command.status == "PENDING":
            try:
                resume_task_creation_interrupt(db, task, command.id)
            except BusinessError:
                record_failed_task_command(
                    db, command, object_no=plan_code, object_name=plan_name, detail=detail
                )
                raise
            detail.update(
                {
                    "plan_scope_hash": task.task_schema_json.get("plan_scope_hash"),
                    "scenario_version_no": task.task_schema_json.get("scenario_version_no"),
                }
            )
            finish_task_command(
                db, command, object_no=plan_code, object_name=plan_name, detail=detail
            )
        elif command.status != "SUCCEEDED":
            raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务创建命令状态无效", status_code=500)
        return frozen_task_view(db, task, reused=True)

    plan = plan_or_404(db, plan_code)
    scenario = scenario_repository.find_template_by_code(db, plan.scenario_code)
    if scenario is None or scenario.deleted_at is not None:
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "方案所属场景不存在或已被删除",
            status_code=409,
            details={"scenario_code": plan.scenario_code},
        )
    if not plan.enabled:
        raise BusinessError(
            "PLAN_DISABLED", "该方案已停用，不能用于创建任务", status_code=409, details={"plan_code": plan_code}
        )
    if not scenario.enabled:
        raise BusinessError(
            "SCENARIO_DISABLED",
            "该场景已停用，不能用于创建任务",
            status_code=409,
            details={"scenario_code": plan.scenario_code},
        )

    task = create_task_from_plan(
        db,
        plan,
        task_name=payload["task_name"],
        remark=payload.get("remark"),
        task_no=task_no,
        expected_scope_hash=payload["expected_scope_hash"],
    )
    try:
        start_audit_interrupt(db, task, creation_operation_id=command.id)
    except BusinessError:
        db.rollback()
        failed = task_repository.find_by_no(db, task_no)
        if failed is not None and failed.status == "FAILED":
            record_failed_task_command(
                db,
                command,
                object_no=plan_code,
                object_name=plan.plan_name,
                detail={"task_no": task_no, "plan_code": plan_code},
            )
        raise
    was_reused = finish_task_command(
        db,
        command,
        object_no=plan_code,
        object_name=plan.plan_name,
        detail={
            "task_no": task_no,
            "plan_code": plan_code,
            "plan_scope_hash": plan.scope_hash,
            "scenario_version_no": task.task_schema_json.get("scenario_version_no"),
        },
    )
    return frozen_task_view(db, task_repository.find_by_no(db, task_no) or task, reused=was_reused)


def _replay_view(db: Session, log: Any) -> dict[str, Any]:
    if str(log.result or "") != "SUCCEEDED":
        raise BusinessError(
            "TASK_CREATE_PREVIOUSLY_FAILED",
            f"该幂等标识对应的创建任务命令此前执行失败：{log.failure_message or '原因未记录'}。"
            "请更换幂等标识后重新创建",
            status_code=409,
            details={"log_no": log.log_no, "failure_code": log.failure_code},
        )
    detail = log.detail_json if isinstance(log.detail_json, dict) else {}
    task = task_repository.find_by_no(db, detail.get("task_no"))
    if task is None:
        raise BusinessError(
            "TASK_CREATE_REPLAY_UNAVAILABLE",
            "该幂等标识对应的任务记录不存在，请更换标识后重新创建",
            status_code=409,
            details={"log_no": log.log_no},
        )
    return frozen_task_view(db, task, reused=True)


def frozen_task_view(db: Session, task: AuditTask, *, reused: bool) -> dict[str, Any]:
    """只从任务冻结快照组装创建结果。"""
    schema = task.task_schema_json if isinstance(task.task_schema_json, dict) else {}
    frozen = scenario_repository.find_version_by_id(db, task.scenario_version_id)
    if not isinstance(schema.get("enabled_rule_keys"), list) or not isinstance(schema.get("fields"), list):
        raise BusinessError(
            "LEGACY_TASK_SCHEMA_INCOMPLETE",
            "该历史任务没有冻结的规则范围，无法确认它当时执行的是哪些检查；请从已发布的方案重新创建任务",
            status_code=409,
            details={"task_no": task.task_no},
        )
    version_no = schema.get("scenario_version_no")
    if not isinstance(version_no, int):
        version_no = frozen.version_no if frozen is not None else None
    if not isinstance(version_no, int):
        raise BusinessError(
            "LEGACY_TASK_SCHEMA_INCOMPLETE",
            "该历史任务没有记录冻结的场景版本号，无法确认它的执行依据",
            status_code=409,
            details={"task_no": task.task_no},
        )
    scenario_code = schema.get("scenario_code")
    if not isinstance(scenario_code, str) or not scenario_code:
        scenario_code = frozen.scenario_code if frozen is not None else None
    if not isinstance(scenario_code, str) or not scenario_code:
        raise BusinessError(
            "LEGACY_TASK_SCHEMA_INCOMPLETE",
            "该历史任务没有记录所属场景，无法确认它的执行依据",
            status_code=409,
            details={"task_no": task.task_no},
        )
    scenario_name = schema.get("scenario_name")
    if not isinstance(scenario_name, str) or not scenario_name:
        scenario_name = frozen.scenario_name if frozen is not None else scenario_code
    plan_name = schema.get("plan_name")
    if not isinstance(plan_name, str) or not plan_name:
        plan = plan_repository.find_by_id(db, task.plan_id)
        plan_name = plan.plan_name if plan is not None else "历史任务配置"
    material_count = schema.get("material_count")
    if not isinstance(material_count, int):
        materials = schema.get("materials")
        material_count = len(materials) if isinstance(materials, list) else 0
    field_count = schema.get("field_count")
    if not isinstance(field_count, int):
        fields = schema.get("fields")
        field_count = len(fields) if isinstance(fields, list) else 0
    return {
        "task_no": task.task_no,
        "task_name": task.task_name,
        "task_status": task.status,
        "scenario_code": scenario_code,
        "scenario_name": scenario_name,
        "scenario_version_no": version_no,
        "plan_name": plan_name,
        "current_node_name": task.current_node,
        "material_count": material_count,
        "field_count": field_count,
        "reused": reused,
    }

