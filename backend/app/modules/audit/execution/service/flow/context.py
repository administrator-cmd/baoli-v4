"""材料前置处理、依赖计划、规则实例与模型上下文。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import ORIGIN_HUMAN_CORRECTION
from app.constants.status import LOOP_RUNNING
from app.domain.execution_mode import EXECUTION_MODES, MODE_EXTRACT, MODE_UNKNOWN
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.execution.service.flow.errors import agent_step
from app.modules.audit.execution.service.flow.execution import execution_in_progress
from app.modules.audit.execution.service.flow.plan import enabled_rules, evaluation_plan, field_review_boundary
from app.modules.audit.execution.service.flow.preload import (
    extraction_artifact_requests,
    preload_extraction_artifacts,
)
from app.modules.audit.execution.service.flow.shared import agent_module, rule_of_instance
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.service.field_review import AuditFieldReviewService
from app.modules.audit.shared import _material_names, _task_manifest
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService


def _assert_execution_modes(rules: list[dict[str, Any]]) -> None:
    """旧任务快照没有执行方式时明确拒绝，不按普通核查的旧逻辑继续跑。"""
    missing = [
        str(rule.get("name") or rule.get("rule_key") or "")
        for rule in rules
        if str(rule.get("execution_mode") or MODE_UNKNOWN) not in EXECUTION_MODES
        or str(rule.get("execution_mode") or MODE_UNKNOWN) == MODE_UNKNOWN
    ]
    if missing:
        names = "、".join(missing[:8])
        more = f" 等 {len(missing)} 条" if len(missing) > 8 else ""
        raise BusinessError(
            "RULE_EXECUTION_MODE_MISSING",
            f"本次任务快照没有确定执行方式：{names}{more}。请重新发布场景并新建任务；旧任务不会按默认核查方式执行。",
            details={"rules": missing},
        )


def execution_attempt_no(execution: NodeExecution) -> int:
    """执行批次的模型请求轮次；缺轮次直接报错，不用默认值冒充。

    每次继续执行都递增，幂等键与评估标识都依赖它。0003 迁移已为旧批次补上第一轮，
    所以这里读到缺失就是真实的结构问题。
    """
    attempt_no = (execution.result_json or {}).get("attempt_no")
    if not isinstance(attempt_no, int) or attempt_no < 1:
        raise BusinessError(
            "AUDIT_EXECUTION_ATTEMPT_MISSING",
            "执行批次缺少轮次标识，无法建立模型请求身份",
            status_code=500,
            details={"execution_id": execution.id},
        )
    return attempt_no


def _evaluation_id(task: AuditTask, execution_id: int, rule_instance_id: str, attempt_no: int) -> str:
    """执行记录、规则实例与执行轮次构成评估标识。"""
    return f"evaluation:{execution_id}:{rule_instance_id}:attempt{attempt_no}"


def _rule_instance_id(node_code: str, rule_identity: str, execution_id: int) -> str:
    """规则实例标识：同一条规则在不同节点执行批次里是不同实例。"""
    return f"{node_code}:{rule_identity}:NE{execution_id}"


def graph_prepare_materials(db: Session, task_no: str, execution_id: int | None) -> dict[str, Any]:
    """程序完成全部材料前置处理，再进入规则与模型循环。"""
    with agent_step(db, task_no):
        from app.modules.audit.artifact.service.preprocessing import AuditArtifactPreprocessingService

        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        execution = execution_in_progress(db, task, execution_id)
        AuditArtifactPreprocessingService(db).prepare_task_materials( task, execution)
        return {"last_error": None}


def graph_load_evaluation(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """从任务冻结配置生成一次依赖计划；单条执行不重新排序。

    同时给出字段核对边界：本次计划是否有抽取段、当前字段版本是否已被确认、
    这一轮允许执行哪些规则。调度与续跑都用这一份判断，不各自推导。
    """
    with agent_step(db, task_no):
        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        manifest = _task_manifest(task)
        execution = execution_in_progress(db, task, execution_id)
        _assert_execution_modes(enabled_rules(task, manifest, node_code))
        plan = evaluation_plan(task, node_code)
        boundary = field_review_boundary(plan, all_rules=manifest["rules"])
        confirmed = AuditFieldReviewService(db).confirmation_is_current( task, execution)
        closed = AuditFieldReviewService().extraction_closed(execution)
        return {
            "agent_mode": True,
            "evaluation_plan": plan,
            "field_review": {
                "required": boundary.required,
                "confirmed": confirmed,
                "extraction_closed": closed,
                "extraction_rule_count": len(boundary.extraction),
                "after_review_rule_count": len(boundary.after),
                # 确认后才执行的规则键：续跑范围的「等待核对」按这一份统计，
                # 不能把边界外已结束的抽取段也算进去。
                "after_rule_keys": sorted(boundary.after),
                "allowed_rule_keys": sorted(
                    boundary.phase_keys(confirmed=confirmed, extraction_closed=closed)
                ),
            },
            "execution_id": execution.id,
            "rule_outcomes": [],
            "loop_count": 0,
            "last_error": None,
        }


def graph_load_rule_instance(
    db: Session,
    task_no: str,
    node_code: str,
    rule_cursor: int,
    execution_id: int | None,
    planned_rule: dict[str, Any],
) -> dict[str, Any]:
    """按本次计划读取单条规则的真实输入，创建独立执行状态。"""
    with agent_step(db, task_no):
        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        execution = execution_in_progress(db, task, execution_id)
        attempt_no = execution_attempt_no(execution)
        rule = planned_rule
        rule_code = str(rule.get("rule_code") or rule.get("rule_key"))
        agent = agent_module().AuditRuleAgentService(db)
        allowed = agent.available_tools()
        identity = rule["rule_key"]
        rule_instance_id = _rule_instance_id(node_code, identity, execution.id)
        evaluation_id = _evaluation_id(task, execution.id, rule_instance_id, attempt_no)
        data = agent.rule_data(task, {**rule, "inputs": []} if rule.get("dependency_error") else rule)
        inputs = data["inputs"]
        # 人工核对过的字段值不能要求模型再从材料补引文；校验阶段按这份真实状态豁免。
        human_field_keys: list[str] = []
        if str(rule.get("execution_mode") or "") == MODE_EXTRACT:
            human_field_keys = sorted(AuditFieldCRUD(db).human_keys_for(
                task.id,
                {field["field_key"] for field in rule.get("output_definitions") or []},
                ORIGIN_HUMAN_CORRECTION,
            ))
        return {
            "agent_mode": True,
            "evaluation_id": evaluation_id,
            "rule_cursor": rule_cursor,
            "rule_instance": {
                "rule_key": rule.get("rule_key"),
                "rule_code": rule_code,
                "rule_name": rule.get("name"),
                "node_code": node_code,
                "rule_instance_id": rule_instance_id,
                "evaluation_id": evaluation_id,
                "input_revision": int(task.input_revision or 0),
                "inputs": inputs,
                # 材料范围 = 这条规则声明的材料位。模型请求超出它的输入会被拒绝。
                "material_refs": list(rule.get("materials") or []),
                "materials": data["materials"],
                "facts": data["facts"],
                "human_field_keys": human_field_keys,
            },
            "allowed_tools": allowed,
            "messages": None,
            "pending_tool_calls": [],
            "tool_results": [],
            "candidate_verdict": None,
            "unresolved_items": [],
            "loop_status": LOOP_RUNNING,
            "loop_count": 0,
            "last_model_response": None,
            "verdict_errors": [],
            "last_error": None,
        }


def graph_prepare_agent_context(
    db: Session,
    task_no: str,
    node_code: str,
    planned_rule: dict[str, Any],
    rule_instance: dict[str, Any] | None,
    messages: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """首次组装本规则上下文；后续轮次沿用已有消息与真实来源。"""
    with agent_step(db, task_no):
        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = rule_of_instance(manifest, node_code, instance)
        agent = agent_module().AuditRuleAgentService(db)
        inputs = list(instance.get("inputs") or [])
        if planned_rule.get("dependency_error"):
            # 无法解析的身份已由模型步骤明确保存为本条未执行；不先用坏引用读取上下文。
            return {"messages": [], "last_error": None}
        if messages:
            return {"messages": list(messages), "last_error": None}
        context_rule = dict(rule)
        if planned_rule.get("unavailable_dependencies"):
            context_rule["unavailable_dependencies"] = planned_rule["unavailable_dependencies"]
            context_rule["dependency_details"] = planned_rule.get("dependency_details") or []
        from app.modules.audit.rule.service.rule_execution import AuditRuleExecutionService
        execution_context = AuditRuleExecutionService(db).evaluation_context( task, rule)
        context_rule.update(execution_context)
        context_rule["available_materials"] = instance.get("materials") or []
        # 引用校验使用模型实际看到的版本，不在保存时重查替换。
        from app.domain.fact_projection import fact_identity
        visible_facts = {fact_identity(fact): fact
                         for fact in [*execution_context["upstream_facts"], *list(instance.get("facts") or [])]}
        instance = {**instance, "facts": list(visible_facts.values()),
                    "upstream_results": execution_context["upstream_results"]}
        # 按声明预读抽取产物，同一材料与产物只读一次。
        requests = extraction_artifact_requests(rule)
        preloaded: list[dict[str, Any]] = []
        allowed_tools = agent.available_tools()
        if requests:
            preloaded = preload_extraction_artifacts(
                db, task, node_code=node_code,
                rule_instance_id=str(instance.get("rule_instance_id") or ""),
                material_refs=tuple(rule.get("materials") or ()),
                requests=requests,
            )
            instance = {**instance, "preloaded_artifacts": preloaded}
            # 正文已在本次上下文中，继续暴露 TASK_DATA_READ 只会让模型重复读取同一产物。
            allowed_tools = [item for item in allowed_tools if item.get("tool_id") != "TASK_DATA_READ"]
        return {
            "rule_instance": instance,
            "allowed_tools": allowed_tools,
            "messages": agent.initial_messages(
                context_rule,
                inputs,
                material_names=_material_names(manifest),
                manifest=manifest,
                preloaded_artifacts=preloaded,
            ),
            "last_error": None,
        }
