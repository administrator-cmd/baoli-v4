"""从方案创建任务的幂等命令与事务编排。"""

from typing import Any
from sqlalchemy.orm import Session
from ...domain.identity import public_no
from ...common.exceptions import BusinessError
from ...entity import AuditTask
from ...repositories.audit import task as audit_task
from ..system.operation_log import normalized_request_hash
from ..system.operation_types import OBJECT_PLAN
from .lookup import plan_or_404
from ..audit.tasks.command_records import (
    claim_task_command,
    finish_task_command,
    record_failed_task_command,
    task_of_command,
)
from ..audit.tasks.creation import create_task_from_plan


def create_from_plan(db: Session, plan_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """从方案创建任务的应用服务：统一处理幂等、门禁、事务和冻结快照。"""
    request_payload = {
        "plan_code": plan_code,
        "task_name": payload["task_name"],
        "remark": payload.get("remark"),
        "expected_scope_hash": payload["expected_scope_hash"],
    }
    request_hash = normalized_request_hash(request_payload)

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
            # 任务创建不再需要建立图等待点：提交命令记录即可。
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
    task = create_task_from_plan(
        db,
        plan,
        task_name=payload["task_name"],
        remark=payload.get("remark"),
        task_no=task_no,
        expected_scope_hash=payload["expected_scope_hash"],
    )
    try:
        db.flush()
    except BusinessError:
        db.rollback()
        failed = audit_task.find_by_no(db, task_no)
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
    return frozen_task_view(db, audit_task.find_by_no(db, task_no) or task, reused=was_reused)


def frozen_task_view(db: Session, task: AuditTask, *, reused: bool) -> dict[str, Any]:
    """只读取任务创建时冻结的配置，不用当前场景或方案补写旧快照。"""
    schema = task.task_schema_json
    return {
        "task_no": task.task_no,
        "task_name": task.task_name,
        "task_status": task.status,
        "scenario_code": schema["scenario_code"],
        "scenario_name": schema["scenario_name"],
        "scenario_version_no": schema["scenario_version_no"],
        "plan_name": schema["plan_name"],
        "current_node_name": task.current_node,
        "material_count": schema["material_count"],
        "field_count": schema["field_count"],
        "reused": reused,
    }
