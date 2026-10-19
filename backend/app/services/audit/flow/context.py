"""材料前置处理、依赖计划、规则实例与模型上下文。"""

from typing import Any
from sqlalchemy.orm import Session
from ....domain.audit_vocab import LOOP_RUNNING
from ....domain.rule_library import active
from ....entity import AuditTask
from ..tasks.lookup import task_or_404
from ..shared import _material_names, _task_manifest
from .execution import execution_in_progress
from .shared import agent_module, rule_of_instance
from .errors import agent_step


def _enabled_rules(task: AuditTask, manifest: dict[str, Any], node_code: str) -> list[dict[str, Any]]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    return [
        rule
        for rule in manifest["rules"]
        if active(rule) and rule.get("node") == node_code and rule.get("rule_key") in enabled_keys
    ]


def _plan_entry(node_code: str, cursor: int, rule: dict[str, Any]) -> dict[str, Any]:
    """统一执行计划中的一条生效规则实例。"""
    return {
        "cursor": cursor,
        "rule_key": rule.get("rule_key"),
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "node_code": node_code,
        "rule": rule,
    }


def _evaluation_id(task: AuditTask, execution_id: int, rule_instance_id: str) -> str:
    """执行记录与规则实例构成评估标识。"""
    return f"evaluation:{execution_id}:{rule_instance_id}"


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
    """从任务冻结配置生成一次依赖计划；单条执行不重新排序。"""
    with agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        execution = execution_in_progress(db, task, execution_id)
        from ....domain.rule_library import execution_order
        agent_rules = execution_order(_enabled_rules(task, manifest, node_code), all_rules=manifest["rules"])
        return {
            "agent_mode": True,
            "evaluation_plan": [_plan_entry(node_code, index, rule) for index, rule in enumerate(agent_rules)],
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
        rule = planned_rule
        rule_code = str(rule.get("rule_code") or rule.get("rule_key"))
        agent = agent_module()
        allowed = agent.available_tools()
        identity = rule["rule_key"]
        rule_instance_id = _rule_instance_id(node_code, identity, execution.id)
        evaluation_id = _evaluation_id(task, execution.id, rule_instance_id)
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
