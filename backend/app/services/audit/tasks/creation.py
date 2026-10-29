"""从方案创建任务及复制任务的业务写入。"""

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....domain.identity import public_no
from ....domain.manifest import build_task_schema
from ....domain.rule_library import execution_container_of
from ....entity.plans import AuditPlan
from ....entity.scenarios import ScenarioTemplate, ScenarioVersion
from ....entity.tasks import AuditTask, TaskDocument
from .lookup import task_or_404


def create_task_from_plan(
    db: Session,
    plan: AuditPlan,
    *,
    task_name: str,
    remark: str | None,
    task_no: str | None = None,
    expected_scope_hash: str | None = None,
) -> AuditTask:
    """使用审核方案创建审核任务。这是新任务的唯一创建服务。

    同一事务里先锁定方案行，再校验方案、场景和已固定版本。通过后把方案的编译产物冻结进
    `manifest_json`，并按它现算生效范围写入 `task_schema_json`（规则与字段清单，以及方案的
    编号、名称与范围指纹）。任务之后一切读取都从这里取，不再读方案当前名称或方案后来重算的范围。

    `expected_scope_hash` 是页面预览时看到的方案范围指纹：与锁定后的实际指纹不一致时明确返回
    `PLAN_SCOPE_CHANGED`——任务必须冻结用户刚刚确认的那一版范围，不在创建时静默取一个已经改过
    的范围。

    task_no 可由调用方预生成，使幂等命令记录与任务在同一次提交中对账。本函数不记录成功操作
    日志：调用方要在等待点确认成功之后才记。
    """
    locked = db.scalar(select(AuditPlan).where(AuditPlan.id == plan.id).with_for_update())
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
    scenario_template = db.scalar(
        select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == plan.scenario_code)
    )
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
    scenario = db.get(ScenarioVersion, plan.scenario_version_id)
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
    db.add(task)
    db.flush()
    for material in manifest["materials"]:
        db.add(
            TaskDocument(
                document_no=public_no("DOC"),
                audit_task_id=task.id,
                material_key=material["material_key"],
                material_name=material["name"],
                required=material["required"],
                status="WAITING_UPLOAD",
            )
        )
    db.refresh(task)
    return task


def copy_task(
    db: Session,
    task_no: str,
    *,
    task_name: str,
    new_task_no: str | None = None,
) -> AuditTask:
    """按源任务所属方案的当前范围重新创建任务。

    明确不是「复制原任务的执行快照」：复制出来的是一个全新的任务，它冻结的是
    方案此刻的范围——源任务的冻结快照与已发生的执行事实一概不带过来。
    没有方案、方案已删除或已停用时拒绝；场景与版本门禁由 create_task_from_plan 统一执行。
    """
    source = task_or_404(db, task_no)
    if source.plan_id is None:
        raise BusinessError(
            "TASK_COPY_REQUIRES_PLAN",
            "该任务没有关联方案，不能复制。请从已启用的方案创建任务",
            status_code=409,
            details={"task_no": task_no},
        )
    plan = db.get(AuditPlan, source.plan_id)
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
    created = create_task_from_plan(db, plan, task_name=task_name, remark=source.remark, task_no=new_task_no)
    db.commit()
    db.refresh(created)
    return created
