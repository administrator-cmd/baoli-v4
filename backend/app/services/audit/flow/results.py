"""候选结论校验与真实规则结果保存；保留人工结论。"""

import json
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....constants.status import LOOP_BLOCKED, LOOP_NEEDS_HUMAN, LOOP_RUNNING, PRODUCED_RESULT_STATUSES
from ....domain.rule_library import is_risk_rule
from ...shared.storage import resolve_storage_path
from ..rule_results import save_rule_result
from ..shared import _task_manifest, pending_rerun_keys
from ..tasks.lookup import task_or_404
from .errors import agent_step
from .execution import execution_in_progress
from .shared import agent_module, rule_of_instance

logger = logging.getLogger(__name__)

# 仅将本条模型产出/引用错误记作未执行；存储损坏、任务状态错误和程序异常继续中止。
_RULE_SAVE_ERRORS = {
    "FACT_VALUE_INVALID", "RULE_OUTPUT_STRUCTURE_INVALID", "RULE_OUTPUT_IDENTITY_INVALID",
    "RULE_OUTPUT_MISSING", "RULE_OUTPUT_STATUS_INVALID", "RULE_OUTPUT_MANUAL_VALUE_CONFLICT",
    "RULE_OUTPUT_SOURCE_MISSING", "RULE_OUTPUT_RAW_VALUE_MISSING", "RULE_REPORT_CONTENT_MISSING",
    "RULE_REPORT_FORMAT_UNSUPPORTED", "RULE_EVIDENCE_ARTIFACT_INVALID", "RULE_EVIDENCE_SOURCE_INVALID",
    "RULE_EVIDENCE_MISSING",
}


def _unproduced_codes(rule: dict[str, Any], verdict: dict[str, Any] | None) -> set[str]:
    """本次结论没有写出的声明产出；这些产出的旧事实必须退出当前位。

    字段产出即使值为空也会写入新的字段事实，旧事实由 save_outputs 退出当前位；
    计算产出只有真正产生非空值时才算写出。
    """
    fields = {field["field_key"] for field in rule.get("output_definitions") or []}
    declared = set(fields)
    if rule.get("result_code"):
        declared.add(rule["result_code"])
    produced: set[str] = set()
    for item in (verdict or {}).get("outputs") or []:
        code = item.get("code")
        if code in fields:
            produced.add(code)
        elif (
            code == rule.get("result_code")
            and item.get("value") is not None
            and (verdict or {}).get("status") in PRODUCED_RESULT_STATUSES
        ):
            produced.add(code)
    return declared - produced


def graph_produce_candidate_verdict(
    db: Session,
    task_no: str,
    node_code: str,
    rule_instance: dict[str, Any] | None,
    last_model_response: dict[str, Any] | None,
    tool_results: list[dict[str, Any]] | None,
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


def _save_verdict_diagnostics(task, execution, rule: dict[str, Any], errors: list[dict[str, Any]]) -> str | None:
    """保存失败时的原始模型响应，供事后判断是截断、代码块还是散文。

    只保存失败响应，不把每次成功响应都落盘；写入失败只记录日志，不影响业务结论保存。
    """
    relative = (
        f"tasks/{task.task_no}/diagnostics/rule_verdict_errors/"
        f"{execution.id}_{rule.get('rule_key')}_{uuid4().hex}.json"
    )
    try:
        path = resolve_storage_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "task_no": task.task_no,
            "execution_id": execution.id,
            "rule_key": rule.get("rule_key"),
            "rule_code": rule.get("rule_code"),
            "errors": errors,
        }, ensure_ascii=False, default=str), encoding="utf-8")
        return relative
    except OSError:
        logger.exception("规则失败响应诊断未能保存 task_no=%s rule_key=%s", task.task_no, rule.get("rule_key"))
        return None


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
    (节点执行记录, 规则编号) 唯一写一条结果，重复保存是更新而非新增。
    人工结论来自字段纠错后的当前事实版本，由规则输入沿用，不在这里改写。

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
        from ..rule_execution import revoke_outputs, save_outputs
        revoke_codes = _unproduced_codes(rules[0], candidate_verdict)
        try:
            with db.begin_nested():
                if revoke_codes:
                    # 本次没有写出的声明产出：上一轮的模型产出不能继续作为当前值供下游读取。
                    revoke_outputs(db, task, execution, rules[0], revoke_codes)
                output_values = save_outputs(
                    db, task, execution, rules[0], candidate_verdict,
                    evaluation_id=evaluation_id,
                )
                saved_result = save_rule_result(db, task=task, execution=execution, rule=rules[0], verdict=candidate_verdict,
                                               loop_status=loop_status, unresolved_items=unresolved_items,
                                               evaluation_id=evaluation_id, inputs=list(instance.get("inputs") or []),
                                               output_values=output_values, turn_count=loop_count)
        except BusinessError as exc:
            if exc.code not in _RULE_SAVE_ERRORS:
                raise
            logger.warning("规则产出未保存 task_no=%s rule_key=%s code=%s message=%s", task_no, rules[0]["rule_key"], exc.code, exc.message)
            candidate_verdict = None
            loop_status = LOOP_BLOCKED
            unresolved_items = [*(unresolved_items or []), {"kind": exc.code, "message": exc.message, "details": exc.details}]
            outcome.update(loop_status=loop_status, unresolved_items=unresolved_items)
            with db.begin_nested():
                revoke_outputs(db, task, execution, rules[0])
                saved_result = save_rule_result(db, task=task, execution=execution, rule=rules[0], verdict=None,
                                               loop_status=loop_status, unresolved_items=unresolved_items,
                                               evaluation_id=evaluation_id, inputs=list(instance.get("inputs") or []),
                                               turn_count=loop_count)
        diagnostic_path = _save_verdict_diagnostics(
            task, execution, rules[0], list(verdict_errors or []),
        ) if verdict_errors else None
        cleaned_errors = []
        for error in verdict_errors or []:
            cleaned = {key: value for key, value in error.items() if key != "model_response"}
            if diagnostic_path is not None:
                cleaned["diagnostics_path"] = diagnostic_path
            cleaned_errors.append(cleaned)
        saved_result.result_json = {**saved_result.result_json,
                                    "verdict_errors": cleaned_errors}
        # 结果与续跑清单同一事务提交：报告刚生成时不会短暂被当成“已列入重跑”。
        if (execution.result_json or {}).get("pending_rerun_keys") is not None:
            execution.result_json = {
                **(execution.result_json or {}),
                "pending_rerun_keys": sorted(pending_rerun_keys(execution) - {rules[0]["rule_key"]}),
            }
        outcome["status"] = saved_result.status
        db.commit()
        return {
            "rule_outcome": outcome,
            "unresolved_items": [],
            "pending_tool_calls": [],
            "tool_results": [],
            "last_error": None,
        }
