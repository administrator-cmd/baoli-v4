"""材料前置处理、依赖计划、规则实例与模型上下文。"""

from typing import Any

from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....constants.status import LOOP_RUNNING
from ....domain.execution_mode import EXECUTION_MODES, MODE_UNKNOWN
from ....entity.tasks import AuditTask, NodeExecution
from ..field_review import confirmation_is_current, extraction_closed
from ..shared import _material_names, _task_manifest
from ..tasks.lookup import task_or_404
from .errors import agent_step
from .execution import execution_in_progress
from .plan import enabled_rules, evaluation_plan, field_review_boundary
from .shared import agent_module, rule_of_instance


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
        from ..preprocessing import prepare_task_materials

        task = task_or_404(db, task_no, lock=True)
        execution = execution_in_progress(db, task, execution_id)
        prepare_task_materials(db, task, execution)
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
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        execution = execution_in_progress(db, task, execution_id)
        _assert_execution_modes(enabled_rules(task, manifest, node_code))
        plan = evaluation_plan(task, node_code)
        boundary = field_review_boundary(plan, all_rules=manifest["rules"])
        confirmed = confirmation_is_current(db, task, execution)
        closed = extraction_closed(execution)
        return {
            "agent_mode": True,
            "evaluation_plan": plan,
            "field_review": {
                "required": boundary.required,
                "confirmed": confirmed,
                "extraction_closed": closed,
                "extraction_rule_count": len(boundary.extraction),
                "after_review_rule_count": len(boundary.after),
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
        task = task_or_404(db, task_no, lock=True)
        execution = execution_in_progress(db, task, execution_id)
        attempt_no = execution_attempt_no(execution)
        rule = planned_rule
        rule_code = str(rule.get("rule_code") or rule.get("rule_key"))
        agent = agent_module()
        allowed = agent.available_tools()
        identity = rule["rule_key"]
        rule_instance_id = _rule_instance_id(node_code, identity, execution.id)
        evaluation_id = _evaluation_id(task, execution.id, rule_instance_id, attempt_no)
        data = agent.rule_data(db, task, {**rule, "inputs": []} if rule.get("dependency_error") else rule)
        inputs = data["inputs"]
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
                # 输入未形成取值时，未执行原因要用业务字段名而不是内部键。
                "field_names": {
                    str(field.get("field_key")): str(field.get("name") or field.get("field_key"))
                    for field in task.task_schema_json.get("fields") or []
                },
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
    """准备模型上下文：系统提示 + 这条规则的要求与可用字段事实。

    消息历史只在首次进入这条规则实例时生成，已有历史就原样保留——重新生成等于把模型已经看到
    的内容换掉，恢复后的判断会与中断前不一致。

    提示里只用本规则实际使用的事实与标识：给全量事实会让模型在无关字段上做判断，也会让它有机会
    引用不属于这条规则的输入。
    """
    with agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = rule_of_instance(manifest, node_code, instance)
        agent = agent_module()
        inputs = list(instance.get("inputs") or [])
        if planned_rule.get("dependency_error"):
            # 无法解析的身份已由模型步骤明确保存为本条未执行；不先用坏引用读取上下文。
            return {"messages": [], "last_error": None}
        # 输入缺项交给模型结合实际条件判断，不能在调用前拒绝整条规则。
        if messages:
            return {"messages": list(messages), "last_error": None}
        context_rule = dict(rule)
        if planned_rule.get("unavailable_dependencies"):
            context_rule["unavailable_dependencies"] = planned_rule["unavailable_dependencies"]
            context_rule["dependency_details"] = planned_rule.get("dependency_details") or []
        from ..rule_execution import evaluation_context
        execution_context = evaluation_context(db, task, rule)
        context_rule.update(execution_context)
        context_rule["available_materials"] = instance.get("materials") or []
        # 模型看到的上游结果与消息一同保存在本条内存状态；最终引用校验使用同一份，
        # 不在保存时重查后来的结果来替换本次依据。
        from ....domain.fact_projection import fact_identity
        visible_facts = {fact_identity(fact): fact
                         for fact in [*execution_context["upstream_facts"], *list(instance.get("facts") or [])]}
        instance = {**instance, "facts": list(visible_facts.values()),
                    "upstream_results": execution_context["upstream_results"]}
        return {
            "rule_instance": instance,
            "messages": agent.initial_messages(
                context_rule,
                inputs,
                material_names=_material_names(manifest),
                manifest=manifest,
            ),
            "last_error": None,
        }
