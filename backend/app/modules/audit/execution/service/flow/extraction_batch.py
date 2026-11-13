"""同一份材料上的多个字段一次读取、一次抽取。

批量只针对「无输入、单字段」的抽取规则：它们在同一条材料上互不依赖，逐个执行会把同一份
正文重复送进模型（实测主合同 81K 字符 × 12 条）。这里按材料把它们合成一次调用，再按字段
拆回各自的规则结果——每条规则仍然独立校验、独立保存、独立成败，不因为同批其他字段失败
而被连带阻断。

校验与保存全部复用单条路径的 `validate_verdict`、`validate_outputs`、`save_outputs`
和 `save_rule_result`，两种执行方式的结果口径一致。
"""

import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import ORIGIN_HUMAN_CORRECTION
from app.constants.status import LOOP_BLOCKED, LOOP_DONE
from app.core.transactions import commit_transaction
from app.domain.audit_state import EvaluationPlanEntry, RuleOutcome
from app.domain.execution_mode import MODE_EXTRACT, execution_mode_label
from app.domain.llm_failure import ChannelFailure
from app.integrations.llm.gateway import RequestLogContext
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.execution.service.flow.context import execution_attempt_no
from app.modules.audit.execution.service.flow.errors import agent_step
from app.modules.audit.execution.service.flow.execution import execution_in_progress
from app.modules.audit.execution.service.flow.model import graph_call_model
from app.modules.audit.execution.service.flow.preload import (
    extraction_artifact_requests,
    preload_extraction_artifacts,
)
from app.modules.audit.execution.service.flow.results import _RULE_SAVE_ERRORS
from app.modules.audit.execution.service.flow.shared import agent_module
from app.modules.audit.execution.service.flow.tools import graph_append_tool_results, graph_execute_tool_calls
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.rule.service.result_contract import validate_result_shape
from app.modules.audit.rule.service.rule_execution import AuditRuleExecutionService
from app.modules.audit.rule.service.rule_results import AuditRuleResultsService
from app.modules.audit.shared import _task_manifest, pending_rerun_keys
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService

logger = logging.getLogger(__name__)

# 字段级校验失败只让该字段记为 BLOCKED，与单条路径的 `_RULE_SAVE_ERRORS` 口径一致；
# 模型给出的结论结构不合法同属「本条没有形成可保存结果」，不是程序缺陷。
_FIELD_SAVE_ERRORS = _RULE_SAVE_ERRORS | {"AGENT_VERDICT_INVALID"}


def _outcome(rule: dict[str, Any], saved, *, loop_status: str, turn_count: int,
             unresolved: list[dict[str, Any]], counted_turns: int) -> RuleOutcome:
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
) -> RuleOutcome:
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
    counted_turns: int, channel_failure: ChannelFailure | None = None,
) -> RuleOutcome:
    with db.begin_nested():
        AuditRuleExecutionService(db).revoke_outputs( task, execution, rule)
        saved = AuditRuleResultsService(db).save_rule_result(
             task=task, execution=execution, rule=rule, verdict=None,
            loop_status=LOOP_BLOCKED, unresolved_items=unresolved,
            evaluation_id=evaluation_id, inputs=[], output_values=None,
            turn_count=turn_count,
        )
    outcome = _finish(saved, rule, loop_status=LOOP_BLOCKED, turn_count=turn_count,
                      unresolved=unresolved, batch_size=batch_size, counted_turns=counted_turns)
    if channel_failure is not None:
        # 同批其他字段已经各自保存；这条字段的未完成原因是渠道故障，交给调度方停止后续请求。
        outcome["channel_failure"] = channel_failure
    commit_transaction(db)
    return outcome


def _sample_field_verdict(item: dict[str, Any], field_key: str) -> dict[str, Any]:
    """把批量结果里的一个字段还原成单条规则使用的结论结构。

    模型看到的是本批短编号，保存前统一换回内部字段键：产出 code 与证据的 output_code
    都由服务端按当前字段补齐，避免模型抄写长编号或把短编号带进单条规则的校验。
    工具读取与后端预读的产物证据都按字段归属；预读证据没有 tool_call_id，不能因此
    被当成非字段证据。
    """
    evidence = []
    for entry in item["evidence"]:
        if isinstance(entry, dict) and (
            entry.get("tool_call_id") or entry.get("artifact_id") is not None
        ):
            entry = {**entry, "output_code": field_key}
        evidence.append(entry)
    field_status = item.get("field_status")
    return {
        "status": item.get("status"),
        "message": item.get("message"),
        "basis": item.get("basis", []),
        "missing_items": item.get("missing_items", []),
        "next_action": item.get("next_action", ""),
        "evidence": evidence,
        "outputs": [{
            "code": field_key,
            "value": item.get("value"),
            "raw_value": item.get("raw_value"),
            "field_status": field_status,
            "recognized_period": item.get("recognized_period"),
            "recognized_statement_scope": item.get("recognized_statement_scope"),
            "recognized_entity_scope": item.get("recognized_entity_scope"),
            "recognized_unit": item.get("recognized_unit"),
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
    tool_results: list[dict[str, Any]], context: dict[str, Any],
    preloaded_artifacts: list[dict[str, Any]], human_field_keys: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """校验一个字段的结论与产出；错误交调用方决定是让模型修正还是记未执行。"""
    field_key = str((rule.get("output_definitions") or [{}])[0].get("field_key"))
    validate_result_shape(item, batch_field=True)
    raw = _sample_field_verdict(item, field_key)
    verdict, _, unresolved = agent.validate_verdict(
        raw, rule=rule, tool_results=tool_results, facts=context["upstream_facts"],
        upstream_results=context["upstream_results"],
        preloaded_artifacts=preloaded_artifacts,
        human_field_keys=human_field_keys,
        inputs=[],
    )
    return verdict, list(unresolved)


def _validate_fields(
    agent, rules: list[dict[str, Any]], by_field: dict[str, dict[str, Any]],
    duplicate_fields: set[str], *, tool_results: list[dict[str, Any]],
    field_contexts: dict[str, dict[str, Any]],
    preloaded_artifacts: list[dict[str, Any]], human_field_keys: set[str],
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
                context=field_contexts[field_key],
                preloaded_artifacts=preloaded_artifacts,
                human_field_keys=human_field_keys,
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


def _clear_pending_key(execution: NodeExecution, rule_key: str) -> None:
    """在内存里清掉这一条规则的待重跑标记，由调用方与字段结果一起提交。

    只清本条规则自己的标记：同批其他字段与依赖它的历史结果的重算意图不受影响。
    """
    if (execution.result_json or {}).get("pending_rerun_keys") is None:
        return
    keys = pending_rerun_keys(execution)
    if rule_key in keys:
        execution.result_json = {
            **(execution.result_json or {}),
            "pending_rerun_keys": sorted(keys - {rule_key}),
        }


def _save_field(
    db: Session, task: AuditTask, execution: NodeExecution, rule: dict[str, Any], *,
    verdict: dict[str, Any], unresolved: list[dict[str, Any]],
    execution_issues: list[dict[str, Any]],
    evaluation_id: str, turn_count: int, batch_size: int, counted_turns: int,
) -> RuleOutcome:
    """保存一个已校验通过的字段；保存期的字段问题只让它记为 BLOCKED。"""
    try:
        verdict["execution_issues"] = list(execution_issues)
        with db.begin_nested():
            output_values = AuditRuleExecutionService(db).save_outputs(
                 task, execution, rule, verdict,
                evaluation_id=evaluation_id,
            )
            saved = AuditRuleResultsService(db).save_rule_result(
                 task=task, execution=execution, rule=rule, verdict=verdict,
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
    # 字段结果与它的待重跑标记同一事务提交：再次中断时不会重复请求已经完成的字段。
    _clear_pending_key(execution, rule["rule_key"])
    commit_transaction(db)
    return outcome


def graph_execute_extraction_batch(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
    entries: list[EvaluationPlanEntry],
) -> list[RuleOutcome]:
    """一次读取材料、一次抽取多个字段，再按字段拆回各自的规则结果。

    `entries` 是执行计划里连续的、同一材料同一产物来源的单字段抽取项；调用方保证长度大于 1。
    批次失败时保存真实原因，不自动追加逐条模型调用。
    """
    with agent_step(db, task_no):
        task = AuditTaskLookupService(db).task_or_404( task_no, lock=True)
        manifest = _task_manifest(task)
        execution = execution_in_progress(db, task, execution_id)
        rules = [entry["rule"] for entry in entries]
        definition = (rules[0].get("output_definitions") or [{}])[0]
        material_key = str(definition.get("material_key"))
        declared_material_name = next(
            (str(item.get("name")) for item in manifest.get("materials") or []
             if item.get("material_key") == material_key and item.get("name")),
            None,
        )
        material_name = declared_material_name or material_key
        log_context = RequestLogContext(
            task_name=task.task_name,
            material_name=declared_material_name,
            operation=execution_mode_label(MODE_EXTRACT),
            field_count=len(rules),
        )
        agent = agent_module().AuditRuleAgentService(db)
        allowed = agent.available_tools()
        # 同一轮里同一批字段得到同一个评估编号：网络重发复用幂等键，只有续跑换轮次才换键。
        attempt_no = execution_attempt_no(execution)
        field_keys = [rule["output_definitions"][0]["field_key"] for rule in rules]
        # 人工核对过的字段值不能要求模型再从材料补引文；校验阶段按这份真实状态豁免。
        human_field_keys = set(AuditFieldCRUD(db).human_keys_for(
            task.id, set(field_keys), ORIGIN_HUMAN_CORRECTION,
        ))
        batch_digest = hashlib.sha1("|".join(sorted(field_keys)).encode("utf-8")).hexdigest()[:12]
        evaluation_id = f"evaluation:{execution.id}:batch:attempt{attempt_no}:{batch_digest}"
        field_contexts: dict[str, dict[str, Any]] = {}
        for rule in rules:
            context = AuditRuleExecutionService(db).evaluation_context( task, rule)
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
        # 工具执行需要一个规则实例来解析材料范围；同批规则材料相同，取第一条即可。
        instance = {
            "rule_key": rules[0]["rule_key"],
            "rule_instance_id": evaluation_id,
            "inputs": [],
            "material_refs": [material_key],
        }
        # 同批字段在同一（材料，产物）上抽取：预读一次，由整批字段共享。
        preloaded = preload_extraction_artifacts(
            db, task, node_code=node_code,
            rule_instance_id=evaluation_id,
            material_refs=(material_key,),
            requests=extraction_artifact_requests(rules[0]),
        )
        if preloaded:
            # 正文已在本批上下文中，继续暴露 TASK_DATA_READ 只会让模型重复读取同一产物。
            allowed = [item for item in allowed if item.get("tool_id") != "TASK_DATA_READ"]
        messages = agent.initial_batch_messages(
            rules, material_key=material_key, material_name=material_name,
            field_contexts=field_contexts, field_codes=field_codes,
            preloaded_artifacts=preloaded,
        )
        tool_results: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        loop_count = 0
        final_response: dict[str, Any] | None = None
        # 本次请求的状态，只决定「还没有结论的字段」怎么记录，不影响已通过校验的字段。
        request_blocked = False
        # 渠道级故障：已校验的字段照常保存，未形成结论的字段带着真实原因停止调度。
        channel_failure: ChannelFailure | None = None
        repaired = False
        by_field: dict[str, dict[str, Any]] = {}
        # 已通过本轮接口校验的字段：修正轮未再返回的字段仍按它们上一轮的结论保存。
        settled_fields: dict[str, dict[str, Any]] = {}
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
                log_context=log_context,
            )
            loop_count = int(turn.get("loop_count") or loop_count)
            messages = turn.get("messages") or messages
            unresolved = [*unresolved, *(turn.get("unresolved_items") or [])]
            if turn.get("channel_failure"):
                channel_failure = turn["channel_failure"]
            if turn.get("last_model_response"):
                final_response = turn["last_model_response"]
            pending = turn.get("pending_tool_calls") or []
            if pending:
                executed = graph_execute_tool_calls(
                    db, task_no, node_code, pending, allowed, instance, unresolved,
                )
                tool_results = [*tool_results, *(executed.get("tool_results") or [])]
                unresolved = list(executed.get("unresolved_items") or unresolved)
                failure = executed.get("channel_failure")
                if isinstance(failure, dict):
                    # 工具内部的大模型请求遇到渠道故障：本批不再请求模型，未形成结论的
                    # 字段带着这一原因保存，故障交给调度方暂停调度。
                    channel_failure = failure
                    request_blocked = True
                    break
                messages = graph_append_tool_results(messages, tool_results)["messages"]
                continue
            if turn.get("loop_status") == LOOP_BLOCKED:
                request_blocked = True
                break
            parsed = agent.parse_verdict((final_response or {}).get("content"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("fields"), list):
                request_blocked = True
                unresolved = [*unresolved, {
                    "kind": "EXTRACTION_BATCH_UNPARSEABLE",
                    "message": "模型没有返回可解析的批量抽取结果",
                }]
                break
            parsed_fields, duplicate_fields, response_issues = _index_fields(parsed["fields"], code_to_field)
            # 修正轮只要求模型重答出错字段。已通过校验的字段只能沿用上一轮结论：
            # 本轮重答（包括重复返回）不能再改写它们，否则一个字段抄错就会把已经有效的结论作废。
            parsed_fields = {
                field_key: item for field_key, item in parsed_fields.items()
                if field_key not in settled_fields
            }
            duplicate_fields -= set(settled_fields)
            by_field = {**settled_fields, **parsed_fields}
            verdicts, field_errors = _validate_fields(
                agent, rules, by_field, duplicate_fields,
                tool_results=tool_results, field_contexts=field_contexts,
                preloaded_artifacts=preloaded,
                human_field_keys=human_field_keys,
            )
            if field_errors and not repaired:
                # 单条路径把接口错误交回模型继续修正；批量先给一次同样的修正机会：
                # 一个字段的引文或编号抄错，不该让整批字段一起记成未执行。
                repaired = True
                settled_fields = {
                    field_key: by_field[field_key]
                    for field_key in verdicts if field_key not in field_errors
                }
                messages = [
                    *messages,
                    {"role": "assistant", "content": str((final_response or {}).get("content") or "")},
                    {"role": "user", "content": _repair_message(field_errors, field_codes)},
                ]
                continue
            break

        if request_blocked:
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

        outcomes: list[RuleOutcome] = []
        batch_size = len(rules)
        for position, rule in enumerate(rules):
            # 同批字段共享一次模型调用，汇总只在第一条计入轮次。
            counted = loop_count if position == 0 else 0
            field_key = str((rule.get("output_definitions") or [{}])[0].get("field_key"))
            item = by_field.get(field_key)
            # 已通过接口校验的字段只取决于它自己的结论。后续修正请求整体失败（模型被阻断、
            # 返回不可解析）时，这里仍然按上一轮已校验的结果保存，不跟着记为未执行。
            if field_key in verdicts:
                verdict, field_unresolved = verdicts[field_key]
                outcomes.append(_save_field(
                    db, task, execution, rule, verdict=verdict, unresolved=field_unresolved,
                    execution_issues=unresolved,
                    evaluation_id=evaluation_id, turn_count=loop_count, batch_size=batch_size,
                    counted_turns=counted,
                ))
                continue
            field_issues = [*unresolved, *response_issues]
            if field_key in duplicate_fields:
                field_issues.append({
                    "kind": "EXTRACTION_BATCH_FIELD_DUPLICATE",
                    "message": "批量结果重复返回同一字段，本条未形成唯一结论",
                    "details": {"code": field_codes.get(field_key)},
                })
            elif item is None and not request_blocked:
                field_issues.append({
                    "kind": "EXTRACTION_BATCH_FIELD_MISSING",
                    "message": "本次批量抽取没有返回该字段的结果，本条未形成结论",
                })
            error = field_errors.get(field_key)
            if error is not None:
                field_issues.append({"kind": error.code, "message": error.message, "details": error.details})
            outcomes.append(_record_blocked(
                db, task, execution, rule,
                unresolved=field_issues,
                evaluation_id=evaluation_id, turn_count=loop_count, batch_size=batch_size,
                counted_turns=counted,
                # 本次请求因渠道故障中断时，未形成结论的字段都带着这一原因；
                # 已通过校验的字段在上面已经各自保存，不受影响。
                channel_failure=channel_failure,
            ))
        return outcomes
