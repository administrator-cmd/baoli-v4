"""同一份材料上的多个字段一次读取、一次抽取。

批量只针对「无输入、单字段」的抽取规则：它们在同一条材料上互不依赖，逐个执行会把同一份
正文重复送进模型（实测主合同 81K 字符 × 12 条）。这里按材料把它们合成一次调用，再按字段
拆回各自的规则结果——每条规则仍然独立校验、独立保存、独立成败，不因为同批其他字段失败
而被连带阻断。

校验与保存全部复用单条路径的 `validate_verdict`、`validate_outputs`、`save_outputs`
和 `save_rule_result`，两种执行方式的结果口径一致。
"""

import json
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....constants.status import LOOP_BLOCKED, LOOP_DONE
from ....entity.tasks import AuditTask, NodeExecution
from ..rule_execution import evaluation_context, save_outputs, validate_outputs
from ..rule_results import save_rule_result
from ..shared import _task_manifest
from ..tasks.lookup import task_or_404
from .errors import agent_step
from .execution import execution_in_progress
from .model import graph_call_model
from .results import _RULE_SAVE_ERRORS
from .shared import agent_module, openai_tools
from .tools import graph_append_tool_results, graph_execute_tool_calls

logger = logging.getLogger(__name__)

# 字段级校验失败只让该字段记为 BLOCKED，与单条路径的 `_RULE_SAVE_ERRORS` 口径一致；
# 模型给出的结论结构不合法同属「本条没有形成可保存结果」，不是程序缺陷。
_FIELD_SAVE_ERRORS = _RULE_SAVE_ERRORS | {"AGENT_VERDICT_INVALID"}


def _outcome(rule: dict[str, Any], saved, *, loop_status: str, turn_count: int,
             unresolved: list[dict[str, Any]], counted_turns: int) -> dict[str, Any]:
    """`turn_count` 是这条字段实际产自的批次轮次；`counted_turns` 是本批次计入汇总的轮次。

    同批字段共享一次模型调用，逐个都计入汇总会把批次轮次重复计算 N 次；
    汇总只在批内第一条计入。
    """
    return {
        "rule_key": rule.get("rule_key"),
        "rule_code": rule.get("rule_code"),
        "rule_name": rule.get("name"),
        "loop_status": loop_status,
        "loop_count": counted_turns,
        "unresolved_items": list(unresolved),
        "status": saved.status,
    }


def _finish(
    saved, rule: dict[str, Any], *, loop_status: str, turn_count: int,
    unresolved: list[dict[str, Any]], batch_size: int, counted_turns: int,
) -> dict[str, Any]:
    """批量来源写进诊断，标明这次结果不是单独调用得到的。"""
    saved.result_json = {
        **(saved.result_json or {}),
        "extraction_batch": {"field_count": batch_size, "turn_count": turn_count},
    }
    return _outcome(rule, saved, loop_status=loop_status, turn_count=turn_count,
                    unresolved=unresolved, counted_turns=counted_turns)


def _record_blocked(
    db: Session, task: AuditTask, execution: NodeExecution, rule: dict[str, Any], *,
    unresolved: list[dict[str, Any]], evaluation_id: str, turn_count: int, batch_size: int,
    counted_turns: int,
) -> dict[str, Any]:
    with db.begin_nested():
        saved = save_rule_result(
            db, task=task, execution=execution, rule=rule, verdict=None,
            loop_status=LOOP_BLOCKED, unresolved_items=unresolved,
            evaluation_id=evaluation_id, inputs=[], output_values=None,
            turn_count=turn_count,
        )
    outcome = _finish(saved, rule, loop_status=LOOP_BLOCKED, turn_count=turn_count,
                      unresolved=unresolved, batch_size=batch_size, counted_turns=counted_turns)
    db.commit()
    return outcome


def _sample_field_verdict(item: dict[str, Any], field_key: str) -> dict[str, Any]:
    """把批量结果里的一个字段还原成单条规则使用的结论结构。

    模型看到的是本批短编号，保存前统一换回内部字段键：产出 code 与证据的 output_code
    都由服务端按当前字段补齐，避免模型抄写长编号或把短编号带进单条规则的校验。
    结论状态沿用模型对该字段的判断（与单条路径同口径），不由程序改写：
    材料里确认没有该内容时单条路径给出的是 ABNORMAL，这里同样保留。
    """
    evidence = []
    for entry in item.get("evidence") or []:
        if isinstance(entry, dict) and entry.get("tool_call_id"):
            entry = {**entry, "output_code": field_key}
        evidence.append(entry)
    field_status = item.get("field_status")
    return {
        "status": item.get("status"),
        "message": item.get("message"),
        "basis": item.get("basis") or [],
        "missing_items": item.get("missing_items") or [],
        "next_action": item.get("next_action") or "",
        "evidence": evidence,
        "outputs": [{
            "code": field_key,
            "value": item.get("value"),
            "raw_value": item.get("raw_value"),
            "field_status": field_status,
        }],
        "report": None,
    }


def _index_fields(
    items: list[Any], code_to_field: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], set[str], list[dict[str, Any]]]:
    """把模型返回的字段项按本批短编号还原成内部字段键；定位不了身份的条目单独记录。"""
    by_field: dict[str, dict[str, Any]] = {}
    duplicate_fields: set[str] = set()
    response_issues: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("code"), str):
            response_issues.append({
                "kind": "EXTRACTION_BATCH_FIELD_INVALID",
                "message": "批量结果包含无法定位身份的字段项",
                "details": {"position": position},
            })
            continue
        code = item["code"].strip().lower()
        field_key = code_to_field.get(code)
        if field_key is None:
            response_issues.append({
                "kind": "EXTRACTION_BATCH_FIELD_UNKNOWN",
                "message": "批量结果包含本组未声明的字段编号，该条未采信",
                "details": {"code": code},
            })
            continue
        if field_key in by_field:
            duplicate_fields.add(field_key)
        else:
            by_field[field_key] = item
    return by_field, duplicate_fields, response_issues


def _validate_field(
    agent, rule: dict[str, Any], item: dict[str, Any], *,
    tool_results: list[dict[str, Any]], context: dict[str, Any], tools: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """校验一个字段的结论与产出；错误交调用方决定是让模型修正还是记未执行。"""
    field_key = str((rule.get("output_definitions") or [{}])[0].get("field_key"))
    raw = _sample_field_verdict(item, field_key)
    verdict, _, unresolved = agent.validate_verdict(
        raw, rule=rule, tool_results=tool_results, facts=context["upstream_facts"],
        upstream_results=context["upstream_results"],
        tools=tools,
    )
    validate_outputs(rule, raw)
    return verdict, list(unresolved)


def _validate_fields(
    agent, rules: list[dict[str, Any]], by_field: dict[str, dict[str, Any]],
    duplicate_fields: set[str], *, tool_results: list[dict[str, Any]],
    field_contexts: dict[str, dict[str, Any]], tools: list[dict[str, Any]],
) -> tuple[dict[str, tuple[dict[str, Any], list[dict[str, Any]]]], dict[str, BusinessError]]:
    """逐字段校验；字段自身的问题只记在该字段上，程序异常照常上抛。"""
    verdicts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    errors: dict[str, BusinessError] = {}
    for rule in rules:
        field_key = str((rule.get("output_definitions") or [{}])[0].get("field_key"))
        item = by_field.get(field_key)
        if item is None or field_key in duplicate_fields:
            continue
        try:
            verdicts[field_key] = _validate_field(
                agent, rule, item, tool_results=tool_results,
                context=field_contexts[field_key], tools=tools,
            )
        except BusinessError as exc:
            if exc.code not in _FIELD_SAVE_ERRORS:
                raise
            errors[field_key] = exc
    return verdicts, errors


def _repair_message(errors: dict[str, BusinessError], field_codes: dict[str, str]) -> str:
    """把逐字段的接口错误交回模型修正；code 用本批短编号，避免模型抄写长字段键。"""
    details = [
        {"code": field_codes.get(key), "error": exc.code, "message": exc.message, "details": exc.details}
        for key, exc in errors.items()
    ]
    return (
        "上一轮批量抽取有字段未通过接口校验："
        + json.dumps(details, ensure_ascii=False, default=str)
        + "。请只修正这些字段后重新返回完整的 fields 数组；code 使用本批给出的短编号。"
          "无法完成时如实使用 EVIDENCE_INSUFFICIENT 并说明原因，不编造产出。"
    )


def _save_field(
    db: Session, task: AuditTask, execution: NodeExecution, rule: dict[str, Any], *,
    verdict: dict[str, Any], unresolved: list[dict[str, Any]],
    execution_issues: list[dict[str, Any]],
    evaluation_id: str, turn_count: int, batch_size: int, counted_turns: int,
) -> dict[str, Any]:
    """保存一个已校验通过的字段；保存期的字段问题只让它记为 BLOCKED。"""
    try:
        verdict["execution_issues"] = list(execution_issues)
        with db.begin_nested():
            output_values = save_outputs(db, task, execution, rule, verdict)
            saved = save_rule_result(
                db, task=task, execution=execution, rule=rule, verdict=verdict,
                loop_status=LOOP_DONE, unresolved_items=unresolved,
                evaluation_id=evaluation_id, inputs=[], output_values=output_values,
                turn_count=turn_count,
            )
    except BusinessError as exc:
        if exc.code not in _FIELD_SAVE_ERRORS:
            raise
        logger.warning(
            "批量抽取的字段未能保存 task_no=%s rule_key=%s code=%s message=%s",
            task.task_no, rule.get("rule_key"), exc.code, exc.message,
        )
        return _record_blocked(
            db, task, execution, rule,
            unresolved=[*execution_issues, {"kind": exc.code, "message": exc.message, "details": exc.details}],
            evaluation_id=evaluation_id, turn_count=turn_count, batch_size=batch_size,
            counted_turns=counted_turns,
        )
    outcome = _finish(saved, rule, loop_status=LOOP_DONE, turn_count=turn_count,
                      unresolved=list(unresolved), batch_size=batch_size, counted_turns=counted_turns)
    db.commit()
    return outcome


def graph_execute_extraction_batch(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """一次读取材料、一次抽取多个字段，再按字段拆回各自的规则结果。

    `entries` 是执行计划里连续的、同一材料同一产物来源的单字段抽取项；调用方保证长度大于 1。
    批次失败时保存真实原因，不自动追加逐条模型调用。
    """
    with agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        execution = execution_in_progress(db, task, execution_id)
        rules = [entry["rule"] for entry in entries]
        definition = (rules[0].get("output_definitions") or [{}])[0]
        material_key = str(definition.get("material_key"))
        material_name = next(
            (str(item.get("name")) for item in manifest.get("materials") or []
             if item.get("material_key") == material_key),
            material_key,
        )
        agent = agent_module()
        allowed = agent.available_tools()
        # 每次批量抽取生成新的评估编号，组内各轮沿用；网络重发目前会生成新编号。
        evaluation_id = f"evaluation:{execution.id}:batch:{uuid4().hex}"
        field_contexts: dict[str, dict[str, Any]] = {}
        for rule in rules:
            context = evaluation_context(db, task, rule)
            field_contexts[rule["output_definitions"][0]["field_key"]] = {
                "upstream_facts": context["upstream_facts"],
                "upstream_results": context["upstream_results"],
            }
        # 模型只抄写本批短编号。内部字段键是 20 位不透明字符串，逐字抄写容易多写或漏写字符；
        # 短编号带本次批次前缀，其他批次或历史响应不会误命中本批字段。
        batch_code = uuid4().hex[:6]
        field_codes = {
            field_key: f"{batch_code}-{index}"
            for index, field_key in enumerate(field_contexts, 1)
        }
        code_to_field = {code.lower(): field_key for field_key, code in field_codes.items()}
        messages = agent.initial_batch_messages(
            rules, material_key=material_key, material_name=material_name,
            field_contexts=field_contexts, field_codes=field_codes,
        )
        # 工具执行需要一个规则实例来解析材料范围；同批规则材料相同，取第一条即可。
        instance = {
            "rule_key": rules[0]["rule_key"],
            "rule_instance_id": evaluation_id,
            "inputs": [],
            "material_refs": [material_key],
        }
        tool_results: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        loop_count = 0
        final_response: dict[str, Any] | None = None
        blocked = False
        repaired = False
        tools = openai_tools(allowed)
        by_field: dict[str, dict[str, Any]] = {}
        duplicate_fields: set[str] = set()
        # 无法定位身份的条目单独记录：它们不能让整批可用字段一起作废，
        # 但对应字段会因为没有结果而记为未执行，不会被静默跳过。
        response_issues: list[dict[str, Any]] = []
        verdicts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        field_errors: dict[str, BusinessError] = {}
        while True:
            turn = graph_call_model(
                db, task_no, node_code, rules[0], messages, allowed,
                loop_count, evaluation_id, tool_results,
            )
            loop_count = int(turn.get("loop_count") or loop_count)
            messages = turn.get("messages") or messages
            unresolved = [*unresolved, *(turn.get("unresolved_items") or [])]
            if turn.get("last_model_response"):
                final_response = turn["last_model_response"]
            pending = turn.get("pending_tool_calls") or []
            if pending:
                executed = graph_execute_tool_calls(
                    db, task_no, node_code, pending, allowed, instance, unresolved,
                )
                tool_results = [*tool_results, *(executed.get("tool_results") or [])]
                unresolved = list(executed.get("unresolved_items") or unresolved)
                messages = graph_append_tool_results(messages, tool_results)["messages"]
                continue
            if turn.get("loop_status") == LOOP_BLOCKED:
                blocked = True
                break
            parsed = agent.parse_verdict((final_response or {}).get("content"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("fields"), list):
                blocked = True
                unresolved = [*unresolved, {
                    "kind": "EXTRACTION_BATCH_UNPARSEABLE",
                    "message": "模型没有返回可解析的批量抽取结果",
                }]
                break
            by_field, duplicate_fields, response_issues = _index_fields(parsed["fields"], code_to_field)
            verdicts, field_errors = _validate_fields(
                agent, rules, by_field, duplicate_fields,
                tool_results=tool_results, field_contexts=field_contexts, tools=tools,
            )
            if field_errors and not repaired:
                # 单条路径把接口错误交回模型继续修正；批量先给一次同样的修正机会：
                # 一个字段的引文或编号抄错，不该让整批字段一起记成未执行。
                repaired = True
                messages = [
                    *messages,
                    {"role": "assistant", "content": str((final_response or {}).get("content") or "")},
                    {"role": "user", "content": _repair_message(field_errors, field_codes)},
                ]
                continue
            break

        if blocked:
            logger.warning(
                "批量抽取未完成 task_no=%s material=%s fields=%s reasons=%s",
                task.task_no, material_key, len(rules),
                [item.get("kind") for item in unresolved],
            )
        elif response_issues:
            logger.warning(
                "批量抽取有未采信的字段响应 task_no=%s material=%s codes=%s",
                task.task_no, material_key,
                [(item.get("details") or {}).get("code") or item.get("details") for item in response_issues],
            )

        outcomes = []
        batch_size = len(rules)
        for position, rule in enumerate(rules):
            # 同批字段共享一次模型调用，汇总只在第一条计入轮次。
            counted = loop_count if position == 0 else 0
            field_key = str((rule.get("output_definitions") or [{}])[0].get("field_key"))
            item = by_field.get(field_key)
            field_issues = [*unresolved, *response_issues]
            if field_key in duplicate_fields:
                field_issues.append({
                    "kind": "EXTRACTION_BATCH_FIELD_DUPLICATE",
                    "message": "批量结果重复返回同一字段，本条未形成唯一结论",
                    "details": {"code": field_codes.get(field_key)},
                })
            elif not blocked and item is None:
                field_issues.append({
                    "kind": "EXTRACTION_BATCH_FIELD_MISSING",
                    "message": "本次批量抽取没有返回该字段的结果，本条未形成结论",
                })
            error = field_errors.get(field_key)
            if error is not None:
                field_issues.append({"kind": error.code, "message": error.message, "details": error.details})
            if blocked or field_key in duplicate_fields or item is None or error is not None:
                outcomes.append(_record_blocked(
                    db, task, execution, rule,
                    unresolved=field_issues,
                    evaluation_id=evaluation_id, turn_count=loop_count, batch_size=batch_size,
                    counted_turns=counted,
                ))
                continue
            verdict, field_unresolved = verdicts[field_key]
            outcomes.append(_save_field(
                db, task, execution, rule, verdict=verdict, unresolved=field_unresolved,
                execution_issues=unresolved,
                evaluation_id=evaluation_id, turn_count=loop_count, batch_size=batch_size,
                counted_turns=counted,
            ))
        return outcomes
