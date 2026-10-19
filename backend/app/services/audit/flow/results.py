"""候选结论校验与真实规则结果保存；保留人工结论。"""

import json
import logging
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from ....common.exceptions import BusinessError
from ....domain.audit_vocab import LOOP_BLOCKED, LOOP_DONE, LOOP_NEEDS_HUMAN, LOOP_RUNNING
from ....domain.rule_library import is_risk_rule
from ....entity import RuleResult
from ..tasks.lookup import task_or_404
from ..rule_results import save_rule_result
from ..shared import _task_manifest
from .execution import execution_in_progress
from .shared import agent_module, openai_tools, rule_of_instance
from .errors import agent_step


logger = logging.getLogger(__name__)

# 仅将本条模型产出/引用错误记作未执行；存储损坏、任务状态错误和程序异常继续中止。
_RULE_SAVE_ERRORS = {
    "FACT_VALUE_INVALID", "RULE_OUTPUT_STRUCTURE_INVALID", "RULE_OUTPUT_IDENTITY_INVALID",
    "RULE_OUTPUT_MISSING", "RULE_OUTPUT_STATUS_INVALID", "RULE_OUTPUT_MANUAL_VALUE_CONFLICT",
    "RULE_OUTPUT_SOURCE_MISSING", "RULE_REPORT_CONTENT_MISSING", "RULE_REPORT_FORMAT_UNSUPPORTED",
    "RULE_EVIDENCE_ARTIFACT_INVALID", "RULE_EVIDENCE_SOURCE_INVALID", "RULE_EVIDENCE_MISSING",
}


def graph_produce_candidate_verdict(
    db: Session,
    task_no: str,
    node_code: str,
    rule_instance: dict[str, Any] | None,
    last_model_response: dict[str, Any] | None,
    tool_results: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    loop_status: str,
    unresolved_items: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]] | None,
    verdict_errors: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """校验模型给出的候选结论，形成可采信的结论或明确的未解决事项。

    服务端只核对 `services.audit.agent.validate_verdict` 所定义的结果结构与真实引用。
    模型没有给出可保存的结论时保留原响应与接口错误，交回同一规则继续修正。
    """
    with agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        agent = agent_module()
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = rule_of_instance(manifest, node_code, instance)
        if loop_status in {LOOP_BLOCKED, LOOP_NEEDS_HUMAN}:
            # 模型响应不可用或拒绝判断：没有候选结论，如实保留原因交后续处理。
            return {
                "candidate_verdict": None,
                "loop_status": loop_status,
                "unresolved_items": list(unresolved_items or []),
                "last_error": None,
            }
        response = last_model_response if isinstance(last_model_response, dict) else {}
        raw = agent.parse_verdict(response.get("content"))
        facts = list(instance.get("facts") or [])
        upstream_results = instance.get("upstream_results") or []
        try:
            if raw is None:
                raise BusinessError("VERDICT_UNPARSEABLE", "模型没有返回可解析的最终结论 JSON 对象")
            from ..rule_execution import validate_outputs
            verdict, status, extra = agent.validate_verdict(
                raw,
                rule=rule,
                tool_results=list(tool_results or []),
                facts=facts,
                upstream_results=upstream_results,
                tools=openai_tools(allowed_tools),
            )
            validate_outputs(rule, raw)
        except BusinessError as exc:
            # 原返回与接口错误交回同一条规则的模型上下文，不猜测状态或丢弃产出。
            # 下一次调用仍走既有模型/工具循环及其配置，不另设重试上限。
            logger.warning("规则结论格式未满足接口，交回模型修正 task_no=%s rule_key=%s code=%s details=%s",
                           task_no, rule["rule_key"], exc.code, exc.details)
            return {
                "candidate_verdict": None,
                "loop_status": LOOP_RUNNING,
                "messages": [
                    *list(messages or []),
                    {"role": "assistant", "content": response["content"]},
                    {"role": "user", "content": "上一轮返回未能保存，接口错误：" + json.dumps(
                        {"code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False
                    ) + "。请结合已有真实材料、工具结果和本规则声明的输出继续执行并修正 JSON。"
                    "需要工具时使用原生 tool_calls；最终返回 status、message、basis、missing_items、next_action、evidence、outputs，不返回进度文字。"
                    "不能完成时如实使用 EVIDENCE_INSUFFICIENT 并说明原因，不编造产出。"},
                ],
                "unresolved_items": list(unresolved_items or []),
                "verdict_errors": [*(verdict_errors or []), {
                    "code": exc.code, "message": exc.message, "details": exc.details,
                    "model_response": response,
                }],
                "last_error": None,
            }
        if is_risk_rule(rule):
            verdict["risk_context"] = {
                "source_rules": [
                    {key: item.get(key) for key in ("rule_result_id", "rule_key", "rule_code", "rule_name", "status", "message")}
                    for item in upstream_results
                ],
                "risk_condition": rule.get("risk_condition"),
            }
        # 调用过程中出现的错误保留为历史诊断；最终业务缺口来自已校验的候选结论。
        # 不把恢复前的失败继续冒充当前业务缺项，也不删除调用历史。
        verdict["execution_issues"] = list(unresolved_items or [])
        return {
            "candidate_verdict": verdict,
            "loop_status": status,
            "unresolved_items": extra,
            "last_error": None,
        }


def graph_save_evaluation_result(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
    rule_instance: dict[str, Any] | None,
    candidate_verdict: dict[str, Any] | None,
    loop_status: str,
    unresolved_items: list[dict[str, Any]] | None,
    evaluation_id: str | None,
    loop_count: int,
    last_model_response: dict[str, Any] | None = None,
    verdict_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存这一条规则实例的评估结果。

    保存是确定性生命周期的一部分：Agent 不能跳过或改写。按
    (节点执行记录, 规则编号) 唯一写一条结果——人工复核已经改写过它时是更新而非新增，
    因此「人工结论」不会被随后的保存覆盖成模型候选结论。

    BLOCKED 时不写「通过」类结果：没有结论就如实记录未解决原因。
    """
    with agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        execution = execution_in_progress(db, task, execution_id)
        instance = rule_instance or {}
        rules = [item for item in manifest["rules"] if item.get("rule_key") == instance.get("rule_key")]
        if not rules:
            raise BusinessError(
                "AGENT_RULE_MISSING",
                "待保存的规则不在任务的已发布配置中，本次未保存结果",
                status_code=500,
                details={"rule_key": instance.get("rule_key")},
            )
        outcome = {
            "rule_key": instance.get("rule_key"),
            "rule_code": instance.get("rule_code"),
            "rule_name": instance.get("rule_name"),
            "loop_status": loop_status,
            "loop_count": loop_count,
            "unresolved_items": list(unresolved_items or []),
        }
        saved = db.scalar(select(RuleResult).where(
            RuleResult.node_execution_id == execution.id,
            RuleResult.rule_key == rules[0]["rule_key"],
        ))
        saved_value = saved
        reviewed = saved_value is not None and bool((saved_value.result_json or {}).get("manual_review_no"))
        if reviewed:
            # 人工裁决已写入真实结果，继续执行不能再次保存旧模型候选及其产出。
            outcome.update(status=saved_value.status, loop_status=LOOP_DONE, unresolved_items=[])
        else:
            try:
                with db.begin_nested():
                    from ..rule_execution import save_outputs
                    output_values = save_outputs(db, task, execution, rules[0], candidate_verdict)
                    saved_result = save_rule_result(db, task=task, execution=execution, rule=rules[0], verdict=candidate_verdict,
                                                   loop_status=loop_status, unresolved_items=unresolved_items,
                                                   evaluation_id=evaluation_id, inputs=list(instance.get("inputs") or []),
                                                   output_values=output_values)
            except BusinessError as exc:
                if exc.code not in _RULE_SAVE_ERRORS:
                    raise
                logger.warning("规则产出未保存 task_no=%s rule_key=%s code=%s message=%s", task_no, rules[0]["rule_key"], exc.code, exc.message)
                candidate_verdict = None
                loop_status = LOOP_BLOCKED
                unresolved_items = [*(unresolved_items or []), {"kind": exc.code, "message": exc.message, "details": exc.details}]
                outcome.update(loop_status=loop_status, unresolved_items=unresolved_items)
                with db.begin_nested():
                    saved_result = save_rule_result(db, task=task, execution=execution, rule=rules[0], verdict=None,
                                                   loop_status=loop_status, unresolved_items=unresolved_items,
                                                   evaluation_id=evaluation_id, inputs=list(instance.get("inputs") or []))
            saved_result.result_json = {**saved_result.result_json,
                                        "loop_count": loop_count,
                                        "model_response": last_model_response,
                                        "verdict_errors": list(verdict_errors or [])}
            outcome["status"] = saved_result.status
        db.commit()
        return {
            "rule_outcome": outcome,
            "unresolved_items": [],
            "pending_tool_calls": [],
            "tool_results": [],
            "last_error": None,
        }
