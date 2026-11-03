"""真实工具调用与工具消息回填。"""

from typing import Any

from sqlalchemy.orm import Session

from app.modules.audit.execution.service.flow.errors import agent_step
from app.modules.audit.execution.service.flow.shared import agent_module, rule_of_instance
from app.modules.audit.shared import _scenario, _task_manifest
from app.modules.audit.task.service.lookup import AuditTaskLookupService


def graph_execute_tool_calls(
    db: Session,
    task_no: str,
    node_code: str,
    pending_tool_calls: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    rule_instance: dict[str, Any] | None,
    existing_unresolved: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """执行模型明确请求的工具并返回真实结果与错误。"""
    with agent_step(db, task_no):
        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        calls = [item for item in (pending_tool_calls or []) if isinstance(item, dict)]
        if not calls:
            return {"tool_results": [], "pending_tool_calls": [], "last_error": None}
        agent = agent_module().AuditRuleAgentService(db)
        scenario = _scenario(db, task)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = rule_of_instance(manifest, node_code, instance)
        inputs = list(instance.get("inputs") or [])
        authorized = {
            str(item["tool_id"])
            for item in (allowed_tools or [])
            if isinstance(item, dict) and item.get("tool_id")
        }
        context = agent.execution_context(
            task=task,
            scene_code=scenario.scenario_code,
            node_code=node_code,
            rule_instance_id=str(instance.get("rule_instance_id") or ""),
            material_refs=tuple(instance.get("material_refs") or ()),
        )
        runtime, result_context = None, {}
        results: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        channel_failure: dict[str, Any] | None = None
        for call in calls:
            tool_name = str(call.get("tool_name") or "")
            if tool_name not in authorized:
                # 模型请求了本次未授权的工具：明确拒绝并告知，不静默忽略。
                result = {
                    "status": "NOT_EXECUTED",
                    "message": f"工具“{tool_name}”不在本次授权范围内，未执行",
                }
                unresolved.append({"kind": "TOOL_NOT_AUTHORIZED", "tool_name": tool_name, "message": result["message"]})
                results.append(agent.tool_result_entry(call, result))
                continue
            from app.modules.audit.rule.service.rule_execution import AuditRuleExecutionService
            runtime, result_context = AuditRuleExecutionService(db).tool_runtime( task, rule, tool_name)
            result = agent.invoke_tool(
                call=call, inputs=inputs, context=context, runtime=runtime,
                result_context=result_context, rule=rule, manifest=manifest,
            )
            if result.get("status") in {"NEEDS_RECONCILE", "FAILED"}:
                unresolved.append(
                    {
                        "kind": "TOOL_UNDECIDED",
                        "tool_name": tool_name,
                        "tool_call_id": call.get("tool_call_id"),
                        "message": result.get("message"),
                    }
                )
            results.append(agent.tool_result_entry(call, result))
            failure = result.get("channel_failure")
            if isinstance(failure, dict):
                # 工具内部的大模型请求遇到渠道故障：本批剩余工具不再执行，
                # 故障带给调度方，按渠道故障暂停而不是继续请求。
                channel_failure = failure
                break
        return {
            "tool_results": results,
            "pending_tool_calls": [],
            # 累积未解决事项：前几轮的失败与待核查不会被本轮覆盖掉。
            "unresolved_items": [*existing_unresolved, *unresolved],
            "last_error": None,
            **({"channel_failure": channel_failure} if channel_failure is not None else {}),
        }


def graph_append_tool_results(
    messages: list[dict[str, Any]] | None,
    tool_results: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """把本批工具结果生成 `tool` 角色消息并追加到消息历史。

    单独成一个节点而不是塞进 `execute_tool_calls`：消息历史与工具执行是两件事，合在一起会让
    「工具执行成功但消息没写进 State」这种半成品状态无法分辨。

    同一个供应商调用标识在消息历史里只能有一条 `tool` 消息：`tool_results` 会跨轮累积（供最终
    证据校验使用），若把整份累积结果逐项追加，第 N 轮会把前 N-1 轮的消息再追加一遍——同一
    `tool_call_id` 出现多条，上下文平方级膨胀，还可能被供应商判为协议错误。因此这里只追加历史
    中尚未出现的标识，避免跨轮重复回填同一次调用。
    """
    agent = agent_module().AuditRuleAgentService()
    appended = list(messages or [])
    appended_ids = {
        str(item.get("tool_call_id"))
        for item in appended
        if isinstance(item, dict) and item.get("role") == "tool" and item.get("tool_call_id")
    }
    for item in tool_results or []:
        if not isinstance(item, dict) or not item.get("tool_call_id"):
            continue
        # 回填消息用供应商的调用标识：模型认的是它自己给出的那个；
        provider_call_id = item.get("provider_tool_call_id") or item["tool_call_id"]
        if str(provider_call_id) in appended_ids:
            continue
        appended_ids.add(str(provider_call_id))
        result = item.get("result") if isinstance(item.get("result"), dict) else item
        appended.append(
            agent.tool_message(
                {
                    "tool_call_id": item["tool_call_id"],
                    "provider_tool_call_id": item.get("provider_tool_call_id"),
                    "tool_name": item.get("tool_name"),
                },
                result,
            )
        )
    return {"messages": appended, "last_error": None}
