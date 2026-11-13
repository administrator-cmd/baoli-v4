"""从方案创建任务及复制任务的业务写入。"""

from copy import deepcopy

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.shared.identity import public_no
from app.domain.configuration.manifest import build_task_schema
from app.domain.configuration.rule_library import execution_container_of
from app.modules.audit.task import crud
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService
from app.modules.material.document.model import TaskDocument
from app.modules.plan.plan.model import AuditPlan


class AuditTaskCreationService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def create_task_from_plan(self, plan: AuditPlan,
        *,
        task_name: str,
        remark: str | None,
        task_no: str | None = None,
        expected_scope_hash: str | None = None,
        report_generation_mode: str = "RULES",
        report_template_no: str | None = None,
    ) -> AuditTask:
        """使用审核方案创建审核任务。这是新任务的唯一创建服务。

        同一事务里先锁定方案行，再校验方案、场景和已固定版本。通过后把方案的编译产物冻结进
        `manifest_json`，并按它现算生效范围写入 `task_schema_json`（规则与字段清单，以及方案的
        编号、名称与范围指纹）。任务之后一切读取都从这里取，不再读方案当前名称或方案后来重算的范围。

        `expected_scope_hash` 是页面预览时看到的方案范围指纹：与锁定后的实际指纹不一致时明确返回
        `PLAN_SCOPE_CHANGED`——任务必须冻结用户刚刚确认的那一版范围，不在创建时静默取一个已经改过
        的范围。

        task_no 可由调用方预生成，使幂等命令记录与任务在同一次提交中对账。本函数不提交事务，
        也不记录成功操作日志：调用方在命令完成时一并提交。
        """
        locked = crud.AuditTaskCRUD(self.db).lock_plan(plan.id)
        if locked is None:
            raise BusinessError(
                "PLAN_NOT_FOUND",
                "审核方案不存在或已被删除",
                status_code=404,
                details={"plan_code": plan.plan_code},
            )
        plan = locked
        if plan.deleted_at is not None:
            raise BusinessError(
                "PLAN_DELETED",
                "该方案已删除，不能创建任务",
                status_code=409,
                details={"plan_code": plan.plan_code},
            )
        if not plan.enabled:
            raise BusinessError(
                "PLAN_DISABLED",
                "该方案已停用，不能创建任务",
                status_code=409,
                details={"plan_code": plan.plan_code},
            )
        scenario_template = crud.AuditTaskCRUD(self.db).scenario_template(plan.scenario_code)
        if scenario_template is None:
            raise BusinessError(
                "SCENARIO_NOT_FOUND",
                "方案所属场景不存在或已被删除",
                status_code=409,
                details={"scenario_code": plan.scenario_code},
            )
        if not scenario_template.enabled:
            raise BusinessError(
                "SCENARIO_DISABLED",
                "该场景已停用，不能创建任务",
                status_code=409,
                details={"scenario_code": plan.scenario_code},
            )
        if plan.scenario_version_id is None:
            raise BusinessError(
                "PLAN_SCENARIO_VERSION_MISSING",
                "方案还没有绑定已发布的场景版本，不能创建任务",
                status_code=409,
                details={"plan_code": plan.plan_code},
            )
        scenario = crud.AuditTaskCRUD(self.db).scenario_version(plan.scenario_version_id)
        if not scenario:
            raise BusinessError(
                "SCENARIO_VERSION_MISSING",
                "方案关联的场景版本不存在",
                status_code=409,
                details={"plan_code": plan.plan_code, "scenario_version_id": plan.scenario_version_id},
            )
        if (
            expected_scope_hash is not None
            and plan.scope_hash is not None
            and expected_scope_hash != plan.scope_hash
        ):
            raise BusinessError(
                "PLAN_SCOPE_CHANGED",
                "方案范围在创建任务期间已被修改，任务必须冻结刚刚确认的那一版范围。请刷新方案页重新核对后再创建任务",
                status_code=409,
                details={
                    "plan_code": plan.plan_code,
                    "expected_scope_hash": expected_scope_hash,
                    "current_scope_hash": plan.scope_hash,
                },
            )
        manifest = plan.manifest_json
        if not isinstance(manifest, dict) or not isinstance(manifest.get("rules"), list):
            raise BusinessError(
                "PLAN_MANIFEST_UNAVAILABLE",
                "方案当前没有可执行配置，不能创建任务",
                status_code=409,
                details={"plan_code": plan.plan_code},
            )
        if plan.sync_error is not None or plan.scope_hash is None:
            raise BusinessError(
                "PLAN_SCOPE_UNAVAILABLE",
                "方案当前范围尚未生效，不能创建任务；请在方案页处理待核对项后重试",
                status_code=409,
                details={"plan_code": plan.plan_code},
            )
        # 范围是 manifest 的纯函数结果，创建任务时现算，不读方案保存时的副本。
        schema = {**build_task_schema(manifest, None, allow_empty=True),
                  # 方案身份随任务冻结：方案后来改名或删除都不影响任务展示与幂等回放。
                  "plan_code": plan.plan_code, "plan_name": plan.plan_name,
                  "plan_scope_hash": plan.scope_hash}
        if report_generation_mode == "TEMPLATE":
            from app.modules.audit.report_template.crud import ReportTemplateCRUD
            from app.modules.audit.report_template.service.analysis import validate_style_profile

            template = ReportTemplateCRUD(self.db).by_no(str(report_template_no or ""))
            if template is None:
                raise BusinessError("REPORT_TEMPLATE_NOT_FOUND", "所选报告模板不存在", status_code=404)
            if template.status != "READY":
                raise BusinessError("REPORT_TEMPLATE_NOT_READY", "所选报告模板尚未完成风格分析", status_code=409)
            try:
                style_profile = validate_style_profile(template.style_profile_json)
            except BusinessError as exc:
                raise BusinessError(
                    "REPORT_TEMPLATE_NOT_READY", "所选报告模板档案无效，请重新分析或修复档案", status_code=409,
                ) from exc
            # 任务冻结创建时的风格档案，模板后续修改不会影响已创建任务。
            manifest = deepcopy(manifest)
            manifest["report_generation"] = {
                "mode": "TEMPLATE",
                "template_no": template.template_no,
                "template_name": template.name,
                "style_profile": style_profile,
            }
        elif report_generation_mode == "RULES":
            manifest = deepcopy(manifest)
            manifest["report_generation"] = {"mode": "RULES"}
        else:
            raise BusinessError("REPORT_GENERATION_MODE_INVALID", "报告生成方式无效", status_code=422)
        first_node = execution_container_of(manifest)
        task = AuditTask(
            task_no=task_no or public_no("TASK"),
            scenario_version_id=scenario.id,
            plan_id=plan.id,
            task_name=task_name,
            remark=remark,
            status="READY",
            current_node=first_node["node_code"],
            manifest_json=deepcopy(manifest),
            task_schema_json=schema,
        )
        task_crud = crud.AuditTaskCRUD(self.db)
        task_crud.add(task)
        for material in manifest["materials"]:
            task_crud.add(
                TaskDocument(
                    document_no=public_no("DOC"),
                    audit_task_id=task.id,
                    material_key=material["material_key"],
                    material_name=material["name"],
                    required=material["required"],
                    status="WAITING_UPLOAD",
                )
            )
        task_crud.refresh(task)
        return task


    def copy_task(self, task_no: str,
        *,
        task_name: str,
        new_task_no: str | None = None,
    ) -> AuditTask:
        """按源任务所属方案的当前范围重新创建任务。

        明确不是「复制原任务的执行快照」：复制出来的是一个全新的任务，它冻结的是
        方案此刻的范围——源任务的冻结快照与已发生的执行事实一概不带过来。
        没有方案、方案已删除或已停用时拒绝；场景与版本门禁由 create_task_from_plan 统一执行。
        """
        source = AuditTaskLookupService(self.db).task_or_404( task_no)
        if source.plan_id is None:
            raise BusinessError(
                "TASK_COPY_REQUIRES_PLAN",
                "该任务没有关联方案，不能复制。请从已启用的方案创建任务",
                status_code=409,
                details={"task_no": task_no},
            )
        plan = crud.AuditTaskCRUD(self.db).plan_by_id(source.plan_id)
        if plan is None or plan.deleted_at is not None:
            raise BusinessError(
                "PLAN_DELETED",
                "该任务所属方案不存在或已删除，不能复制任务",
                status_code=409,
                details={"task_no": task_no, "plan_id": source.plan_id},
            )
        if not plan.enabled:
            raise BusinessError(
                "PLAN_DISABLED",
                "该任务所属方案已停用，不能复制任务",
                status_code=409,
                details={"plan_code": plan.plan_code},
            )
        # 不在这里提交：由调用方的 finish_task_command 把任务与命令成功状态同一次提交，
        # 避免任务已落库、命令仍是 PENDING 的中间态。
        report_generation = source.manifest_json.get("report_generation") or {}
        return self.create_task_from_plan(
            plan, task_name=task_name, remark=source.remark, task_no=new_task_no,
            report_generation_mode=str(report_generation.get("mode") or "RULES"),
            report_template_no=report_generation.get("template_no"),
        )
