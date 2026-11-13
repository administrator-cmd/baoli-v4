"""从方案创建任务的幂等命令与事务编排。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.shared.identity import public_no
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.command_records import AuditTaskCommandRecordsService
from app.modules.audit.task.service.creation import AuditTaskCreationService
from app.modules.plan.plan.service.lookup import PlanLookupService
from app.modules.scenario.template.model import ScenarioVersion
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_PLAN


class PlanTasksService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def create_from_plan(self, plan_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """从方案创建任务的应用服务：统一处理幂等、门禁、事务和冻结快照。"""
        request_payload = {
            "plan_code": plan_code,
            "task_name": payload["task_name"],
            "remark": payload.get("remark"),
            "expected_scope_hash": payload["expected_scope_hash"],
            "report_generation_mode": payload.get("report_generation_mode", "RULES"),
            "report_template_no": payload.get("report_template_no"),
        }
        request_hash = OperationLogService().normalized_request_hash(request_payload)

        task_no = public_no("TASK")
        command, reused = AuditTaskCommandRecordsService(self.db).claim_task_command(

            object_type=OBJECT_PLAN,
            operation_type="TASK_CREATE",
            idempotency_key=payload["idempotency_key"],
            request=request_payload,
            request_hash=request_hash,
            task_no=task_no,
        )
        if reused:
            task = AuditTaskCommandRecordsService(self.db).task_of_command( command)
            detail = {"task_no": task.task_no, "plan_code": plan_code}
            plan_name = task.task_schema_json["plan_name"]
            if command.status == "FAILED":
                AuditTaskCommandRecordsService(self.db).record_failed_task_command(
                     command, object_no=plan_code, object_name=plan_name, detail=detail
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
                        "scenario_version_id": task.scenario_version_id,
                    }
                )
                AuditTaskCommandRecordsService(self.db).finish_task_command(
                     command, object_no=plan_code, object_name=plan_name, detail=detail
                )
            elif command.status != "SUCCEEDED":
                raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务创建命令状态无效", status_code=500)
            return self.frozen_task_view( task, reused=True)

        plan = PlanLookupService(self.db).plan_or_404( plan_code)
        task = AuditTaskCreationService(self.db).create_task_from_plan(

            plan,
            task_name=payload["task_name"],
            remark=payload.get("remark"),
            task_no=task_no,
            expected_scope_hash=payload["expected_scope_hash"],
            report_generation_mode=payload.get("report_generation_mode", "RULES"),
            report_template_no=payload.get("report_template_no"),
        )
        AuditTaskCRUD(self.db).flush()
        was_reused = AuditTaskCommandRecordsService(self.db).finish_task_command(

            command,
            object_no=plan_code,
            object_name=plan.plan_name,
            detail={
                "task_no": task_no,
                "plan_code": plan_code,
                "plan_scope_hash": plan.scope_hash,
                "scenario_version_id": task.scenario_version_id,
            },
        )
        found = AuditTaskCRUD(self.db).by_no(task_no)
        return self.frozen_task_view( found or task, reused=was_reused)


    def frozen_task_view(self, task: AuditTask, *, reused: bool) -> dict[str, Any]:
        """只读取任务创建时冻结的配置，不用当前场景或方案补写旧快照。"""
        schema = task.task_schema_json
        scenario = ScenarioTemplateCRUD(self.db).get_of(ScenarioVersion, task.scenario_version_id)
        if scenario is None:
            raise BusinessError(
                "SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500,
                details={"task_no": task.task_no, "scenario_version_id": task.scenario_version_id},
            )
        return {
            "task_no": task.task_no,
            "task_name": task.task_name,
            "task_status": task.status,
            "scenario_code": scenario.scenario_code,
            "scenario_name": scenario.scenario_name,
            "scenario_version_no": scenario.version_no,
            "plan_name": schema.get("plan_name"),
            "current_node_name": task.current_node,
            "material_count": len(task.manifest_json.get("materials") or []),
            "field_count": len(schema.get("fields") or []),
            "reused": reused,
        }
