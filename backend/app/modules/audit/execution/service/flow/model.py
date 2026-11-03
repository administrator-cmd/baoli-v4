"""单轮模型调用与既有循环上限的显式阻断。"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import LOOP_BLOCKED, LOOP_RUNNING
from app.domain.execution_mode import MODE_EXTRACT, execution_mode_label
from app.domain.llm_failure import channel_failure_detail, is_channel_failure
from app.integrations.llm.gateway import RequestLogContext
from app.modules.audit.execution.service.flow.errors import agent_step
from app.modules.audit.execution.service.flow.shared import agent_module, openai_tools
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService

logger = logging.getLogger(__name__)


def _loop_limit_reached(loop_count: int) -> int | None:
    """本条规则是否已经到达模型轮次上限；到达时返回上限值。

    轮次的定义全链路只有一种：一次模型调用算一轮（与 `loop_count` 的计数口径一致）。
    批次不是轮次：一次模型调用可以请求多个工具，它们仍属于同一轮。

    `settings.llm_tool_max_rounds` 为 0 表示不设上限（由调用方自行控制终止），
    这是配置事实，不在这里改成某个默认值。
    """
    from app.core.config import get_settings

    max_rounds = get_settings().llm_tool_max_rounds
    if max_rounds and loop_count >= max_rounds:
        return max_rounds
    return None


def _loop_limit_state(max_rounds: int, loop_count: int, executed_tool_calls: list[str]) -> dict[str, Any]:
    """到达上限时的显式阻断状态：原因、已用轮次与已经执行过的工具调用。"""
    return {
        "loop_status": LOOP_BLOCKED,
        "unresolved_items": [
            {
                "kind": "LLM_TOOL_LOOP_LIMIT",
                "message": (
                    f"模型与工具循环达到已配置上限（{max_rounds} 轮）仍未形成可保存的结论，本次未完成；"
                    "系统不会把「跑到上限」当作结论，也不会继续执行下一批工具"
                ),
                "max_rounds": max_rounds,
                "loop_count": loop_count,
                "executed_tool_calls": list(executed_tool_calls),
            }
        ],
    }


def _log_context_of(task: AuditTask, rule: dict[str, Any]) -> RequestLogContext:
    """从已加载的任务与规则生成日志上下文；缺项保持 None，不额外查库、不猜。"""
    mode = str(rule.get("execution_mode") or "")
    manifest = task.manifest_json if isinstance(task.manifest_json, dict) else {}
    material_names = {
        str(item.get("material_key")): str(item.get("name"))
        for item in manifest.get("materials") or []
        if isinstance(item, dict) and item.get("material_key") and item.get("name")
    }
    names = list(dict.fromkeys(
        material_names[str(key)]
        for key in rule.get("materials") or []
        if str(key) in material_names
    ))
    definitions = rule.get("output_definitions") or []
    return RequestLogContext(
        task_name=str(task.task_name) if task.task_name else None,
        material_name="、".join(names) if names else None,
        operation=execution_mode_label(mode) if mode else None,
        # 只有字段抽取的产出定义就是本次要抽的字段；其他执行方式不把产出当字段数。
        field_count=len(definitions) if mode == MODE_EXTRACT and definitions else None,
    )


def graph_call_model(
    db: Session,
    task_no: str,
    node_code: str,
    planned_rule: dict[str, Any],
    messages: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    loop_count: int,
    state_evaluation_id: str | None,
    executed_tool_results: list[dict[str, Any]] | None = None,
    log_context: RequestLogContext | None = None,
) -> dict[str, Any]:
    """调用模型一次，返回本条规则这一轮的状态变化。

    这里不执行工具：模型请求了工具就由 `graph_execute_tool_calls` 执行、由
    `graph_append_tool_results` 生成 tool 消息，再由单条执行器决定继续调用或保存结果。
    本条循环不创建图回边，不占用任务图的步数。

    轮次上限（`settings.llm_tool_max_rounds`，按模型调用次数计）在这里兜底：已用完额度时不再
    调用模型，直接进 `LOOP_BLOCKED`；这一轮是最后一轮而模型仍请求工具时，不执行这一批工具，
    同样进 `LOOP_BLOCKED`。

    两种情况都写入 `LLM_TOOL_LOOP_LIMIT` 与未解决事项，并保留本条内存中的工具结果
    （`executed_tool_results`）与已得到的模型响应，交后续按阻断处理——既不默认通过，
    也不静默结束。

    ``log_context`` 只用于日志展示：批量抽取会传入真实批次字段数与材料名；
    单条规则未传时从已加载的任务与规则生成。
    """
    with agent_step(db, task_no):
        from app.integrations.llm.gateway import TURN_CONTENT, TURN_REFUSAL, TURN_TOOL_CALLS

        # 锁定任务行；同时复用已加载的任务与冻结配置生成日志上下文，不额外查询。
        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        agent = agent_module().AuditRuleAgentService(db)
        executed = [
            str(item.get("tool_call_id"))
            for item in (executed_tool_results or [])
            if isinstance(item, dict) and item.get("tool_call_id")
        ]
        exhausted = _loop_limit_reached(loop_count)
        if exhausted is not None:
            # 额度已用完：不再调用模型，也不再执行任何工具。
            return {
                "loop_count": loop_count,
                "candidate_verdict": None,
                "pending_tool_calls": [],
                "messages": list(messages or []),
                **_loop_limit_state(exhausted, loop_count, executed),
                "last_error": None,
            }
        rule = planned_rule
        if rule.get("dependency_error"):
            return {"loop_status": LOOP_BLOCKED, "candidate_verdict": None, "pending_tool_calls": [], "last_error": None,
                    "unresolved_items": [{"kind": "RULE_DEPENDENCY_UNAVAILABLE", "message": "无法执行：字段、材料或计算参数的身份引用缺失或有歧义", "references": rule["dependency_error"]}]}
        tools = openai_tools(allowed_tools)
        request_log_context = log_context or _log_context_of(task, rule)
        next_loop_count = loop_count
        try:
            # 抽取所需的真实产物已由后端预读并注入上下文；这里不再强制首轮工具调用。
            tool_choice = "auto"
            next_loop_count += 1
            # 同一条规则的同一轮模型请求在网络重试期间必须复用幂等键；
            # 轮次递增后键也随之变化，避免把不同上下文误识别为同一请求。
            request_key = ":".join(
                (
                    "audit",
                    task_no,
                    node_code,
                    state_evaluation_id or "unknown-evaluation",
                    str(next_loop_count),
                )
            )
            turn = agent.call_model_once(
                list(messages or []),
                tools=tools,
                tool_choice=tool_choice,
                idempotency_key=request_key,
                log_context=request_log_context,
            )
        except BusinessError as exc:
            channel = is_channel_failure(exc.code)
            # 渠道故障与单条规则的问题分开表达：前者说明这次请求本身没完成，
            # 后续规则同样会失败，由调用方停止调度；后者只影响本条规则。
            logger.warning(
                "规则模型请求失败 task_no=%s rule_key=%s code=%s channel_failure=%s message=%s",
                task_no, rule["rule_key"], exc.code, channel, exc.message,
            )
            turn = {
                "loop_status": LOOP_BLOCKED, "loop_count": next_loop_count,
                "candidate_verdict": None, "pending_tool_calls": [], "last_error": None,
                "unresolved_items": [{"kind": exc.code, "message": exc.message, "details": exc.details}],
            }
            if channel:
                turn["channel_failure"] = channel_failure_detail(
                    exc.code, exc.message, rule_key=rule["rule_key"],
                    # 是否可能已经产生费用由网关按请求阶段给出，不按错误码猜。
                    result_unknown=bool((exc.details or {}).get("result_unknown")),
                    # 上游错误码、说明与请求 ID 随暂停原因保存，便于定位具体批次。
                    details=exc.details,
                )
            return turn
        summary = {
            "kind": turn.kind,
            "finish_reason": turn.finish_reason,
            "model": turn.model,
            "response_id": turn.response_id,
            "requested_at": turn.requested_at,
            "responded_at": turn.responded_at,
            "tool_call_count": len(turn.tool_calls),
            "content": turn.content,
            "refusal": turn.refusal,
            "raw_message": turn.raw_message,
        }
        base = {
            "loop_count": next_loop_count,
            "last_model_response": summary,
            "candidate_verdict": None,
            "pending_tool_calls": [],
            "last_error": None,
        }
        if turn.kind == TURN_TOOL_CALLS:
            exhausted = _loop_limit_reached(next_loop_count)
            if exhausted is not None:
                # 这一轮是最后一轮：不把这一批工具放进待执行集合（它不会被执行），
                # assistant 消息也不并入历史——留着一条没有结果的 tool_calls
                # 会让恢复后的消息历史是一段不完整的协议。
                return {
                    **base,
                    "messages": list(messages or []),
                    **_loop_limit_state(exhausted, next_loop_count, executed),
                }
            return {
                **base,
                "messages": [*list(messages or []), agent.assistant_message_of(turn)],
                "pending_tool_calls": agent.pending_calls_of(turn),
                "loop_status": LOOP_RUNNING,
            }
        if turn.kind == TURN_CONTENT:
            return {**base, "messages": list(messages or []), "loop_status": LOOP_RUNNING}
        if turn.kind == TURN_REFUSAL:
            return {
                **base,
                "messages": list(messages or []),
                "loop_status": LOOP_BLOCKED,
                "unresolved_items": [
                    {
                        "kind": "MODEL_REFUSAL",
                        "message": turn.refusal or "模型拒绝了本次判断",
                    }
                ],
            }
        return {
            **base,
            "messages": list(messages or []),
            "loop_status": LOOP_BLOCKED,
            "unresolved_items": [
                {
                    "kind": "MODEL_RESPONSE_UNUSABLE",
                    "message": f"模型响应不可用（finish_reason={turn.finish_reason}）",
                }
            ],
        }
