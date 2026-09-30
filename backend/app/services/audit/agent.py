"""规则评估 Agent：在授权范围内选择取证与比较工具，形成候选结论。

职责边界
--------
Agent **只**负责在授权范围内选择工具并形成候选结论。以下全部由 Graph 控制，
不由模型决定：任务加载、模型调用、人工审核、保存结果、报告渲染。

这里的函数都是**单步**的：一次模型调用、一次工具执行批、一次结论校验。
没有函数内部循环——循环由 `graphs/audit_graph.py` 的节点与条件边表达，
因此每一步都是可 checkpoint 的状态，interrupt / resume 能停在循环中间，
恢复后也不会重复执行已经登记过的工具调用。

为什么不在这里循环
------------------
一旦在函数内部跑完整个「模型 → 工具 → 模型」，interrupt 就只能停在函数外面：
进程重启后无法知道循环走到第几轮、哪些工具已经执行过。工具可能已经产生真实
外部效果，重跑是危险的；不重跑又没有可恢复的中间状态。把每一步做成图节点，
「当前轮次、待执行调用、已得结果」全部落在 State 与账本里。

不做什么
--------
- **不保存规则结果**：候选结论交回 Graph，由 `save_evaluation_result` 落库。
- **不自动通过**：模型给出 PASSED 也必须带证据引用，且与确定性工具结果一致。
- **不把「跑满轮次」当结论**：轮次上限由调用方给，达到即显式失败。
- **不重复执行已成功的工具调用**：每次调用先查账本（见 `agent_tool_ledger`）。

模型结论与确定性工具冲突时
--------------------------
不能覆盖确定性结果。冲突时把候选结论降为「无法判断」并附冲突说明，
交人工复核：模型不能推翻精确计算。

模型结论文本怎么解析
--------------------
要求模型以 JSON 对象给出结论（`response_format={"type": "json_object"}`），
再由服务端按固定 Schema 逐项校验。**不**从文本里扫描「最后一行像 JSON 的东西」：
那样会把一段夹带 JSON 的说明文字当成结论，也没法区分「模型写错了」与「模型没写」。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ...domain import capabilities, fact_snapshot, tool_catalog
from ...domain.tool_context import ToolExecutionContext
from ...common.exceptions import BusinessError
from ...integrations.llm.gateway import ModelTurn, call_chat
from ...entities import AgentToolCall, AuditTask, EvaluationSnapshot
from .. import agent_tool_ledger as ledger
from .. import tool_service
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 循环状态。BLOCKED 必须带 unresolved_items，不允许用「跑完轮次」冒充 DONE。
LOOP_NEEDS_HUMAN = "NEEDS_HUMAN"
LOOP_DONE = "DONE"

# 需要人工复核的候选结论状态。
HUMAN_REVIEW_STATUSES = frozenset({"PENDING_REVIEW", "EVIDENCE_INSUFFICIENT"})

# 模型能给出的结论状态。与能力目录的规则状态口径一致：模型不是另立一套状态。
VERDICT_STATUSES = frozenset({"PASSED", "ABNORMAL", "EVIDENCE_INSUFFICIENT", "PENDING_REVIEW"})

# 候选结论的固定 Schema。模型按它输出，服务端按它校验；两者是同一份清单
# （见 `validate_verdict`），任何一侧单独改动都会被校验当场发现。
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": sorted(VERDICT_STATUSES)},
        "message": {"type": "string", "minLength": 1},
        # 证据引用：本次评估的工具调用标识，或冻结快照里的事实身份。
        # PASSED / ABNORMAL 必须至少给出一条，且必须能在本次评估里找到。
        "evidence": {"type": "array"},
    },
    "required": ["status", "message", "evidence"],
    "additionalProperties": False,
}

# 响应格式：要求模型返回 JSON 对象。这里用 `json_object` 而不是 `json_schema`，
# 因为本项目的模型端点已经用同一形式（`_call_json`）真实跑通；语义约束由服务端
# 按 `VERDICT_SCHEMA` 完整校验，不依赖供应商对 strict schema 的支持程度。
_VERDICT_RESPONSE_FORMAT = {"type": "json_object"}

_SYSTEM_PROMPT = """你是合同审核判断器。你的职责是：根据给定的规则、字段事实与可用工具，
调用工具完成确定性核对，然后给出候选审核结论。

严格约束：
1. 金额、日期、加总与字段比较**必须**通过工具完成，不要自行心算或推断。
   工具的返回值是确定性的真实结果，你不能改写它。
2. inputs 里的 field_key 与 material_key 必须逐字使用提示中给出的标识，
   不得改写、不得自行编造材料标识。
3. 只使用本次提供的工具。不要假设存在其它工具。
   parameters 必须与规则的固定参数完全一致，不能自行更改比较方向、阈值或审核标准。
4. 结论只能取 PASSED、ABNORMAL、EVIDENCE_INSUFFICIENT、PENDING_REVIEW 之一。
   证据不足时返回 EVIDENCE_INSUFFICIENT，不要凭猜测判通过。
5. 如果工具返回「不可判定」或「能力未实现」，如实反映在结论里，不要改写成通过。
   工具返回 FAILED 或提示需要核查时，不得给出 PASSED 或 ABNORMAL。
6. 给出结论时用一句话说明依据（用了哪个工具、比了什么、结果是什么）。

当你不再需要调用工具时，只输出一个 JSON 对象，不要输出别的文字：
{"status": "...", "message": "...", "evidence": [{"tool_call_id": "..."}]}

evidence 至少给出一项依据：调用工具时**工具结果里返回的 tool_call_id**，
或冻结事实的身份（{"fact_code": "...", "material_key": "..."}）。
判定为 PASSED 或 ABNORMAL 时 evidence 不得为空。"""


def build_rule_context(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    material_names: dict[str, str],
) -> str:
    """规则的评估上下文：规则要求 + 可用的字段事实。

    只给**本次规则实际使用的**事实与标识。给全量事实会让模型在无关字段上做判断，
    也会让它有机会引用不属于这条规则的输入。
    """
    tool_id = capabilities.capability_id_of_rule(rule)
    published_parameters = published_tool_parameters(rule, manifest, tool_id)
    lines = [
        f"规则编号：{rule.get('rule_code') or rule.get('rule_key')}",
        f"规则名称：{rule.get('name')}",
        f"审核要求：{rule.get('requirement') or '（未填写）'}",
        f"执行能力：{tool_id}",
        "工具固定参数：" + json.dumps(published_parameters, ensure_ascii=False, sort_keys=True),
        "threshold 如为十进制字符串，工具调用时按工具 Schema 填 JSON 数字；实际计算仍采用发布原值。",
        "",
        "可用字段事实（inputs 必须逐字使用这些标识）：",
    ]
    for item in inputs:
        material_key = item.get("material_key")
        lines.append(
            f"- {item.get('field_key')}@{material_key}"
            f"（{material_names.get(material_key, material_key)}）："
            f"原文={item.get('raw_value')!r}，标准化值={item.get('normalized_value')!r}，"
            f"状态={item.get('status')}，类型={item.get('value_type')}，"
            f"单位={item.get('unit') or '未声明'}，币种={item.get('currency') or '未声明'}"
        )
    return "\n".join(lines)


def initial_messages(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    material_names: dict[str, str],
) -> list[dict[str, Any]]:
    """一条规则实例的初始消息历史。只在 `prepare_agent_context` 里生成一次。"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": build_rule_context(rule, inputs, manifest=manifest, material_names=material_names)},
    ]


def call_model_once(messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]) -> ModelTurn:
    """调用模型**一次**，返回这一次的响应契约。

    这里是整个 Agent 循环里唯一的模型调用点。工具执行不由本函数完成——
    模型请求了工具就由 Graph 的 `execute_tool_calls` 节点执行，再回到 `call_model`。
    """
    if not tools:
        raise BusinessError(
            "AGENT_TOOLS_EMPTY",
            "本次评估没有任何授权工具，不能进入规则评估 Agent 循环",
            status_code=500,
        )
    return call_chat(
        messages,
        tools=tools,
        tool_choice="auto",
        response_format=_VERDICT_RESPONSE_FORMAT,
    )


def assistant_message_of(turn: ModelTurn) -> dict[str, Any]:
    """把模型这一轮的响应原样并入消息历史。

    `tool_calls` 必须逐字回填模型返回的那一份：重新构造会让 `tool_call_id` 对不上，
    供应商会拒绝或按另一条调用处理。因此这里取 `raw_message`，只补 role。
    """
    message = dict(turn.raw_message or {})
    message["role"] = "assistant"
    return message


def pending_calls_of(turn: ModelTurn, evaluation_id: str) -> list[dict[str, Any]]:
    """本轮模型请求的工具调用，写进 State 的 `pending_tool_calls`。

    **账本标识**带上评估批次前缀：供应商给的调用标识只在一次响应内唯一，
    不同评估（重跑节点、下一条规则）之间可能重复，而账本要求 `tool_call_id`
    全局唯一且「同一次调用不重复执行」。原始标识另存为
    `provider_tool_call_id`，回填 `tool` 消息时必须用它——模型认的是自己给的那个。
    """
    return [
        {
            "tool_call_id": ledger_call_id(evaluation_id, call.tool_call_id),
            "provider_tool_call_id": call.tool_call_id,
            "tool_name": call.tool_name,
            "arguments": call.arguments,
        }
        for call in turn.tool_calls
    ]


def ledger_call_id(evaluation_id: str, provider_call_id: str) -> str:
    """账本里的工具调用标识：评估批次 + 供应商标识。"""
    return f"{evaluation_id}:{provider_call_id}"


def tool_message(call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """一次工具结果的回填消息（`append_tool_results` 用它生成）。

    `tool_call_id` 用**供应商**给的标识：模型据此把结果对回它那次调用。
    结果内容里额外带上账本标识，模型引用证据时用的就是它——账本标识才是
    「哪一次真实执行」的稳定身份（见 `services/agent_tool_ledger`）。
    """
    content = {
        **result,
        "tool_call_id": call.get("tool_call_id"),
        "tool_name": call.get("tool_name"),
    }
    return {
        "role": "tool",
        "tool_call_id": call.get("provider_tool_call_id") or call.get("tool_call_id"),
        "content": json.dumps(content, ensure_ascii=False, default=str),
    }


# 候选结论的服务端校验


def parse_verdict(content: str | None) -> dict[str, Any] | None:
    """把模型的最终响应解析成候选结论。

    要求整体是一个 JSON 对象（见 `_VERDICT_RESPONSE_FORMAT`）。
    解析不出对象时返回 None，由调用方按「模型没有给出可解析的结论」处理（BLOCKED），
    **不**在文本里扫描「最后一行像 JSON 的东西」——那会把说明文字里的片段当成结论。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _schema_error(message: str, **details: Any) -> BusinessError:
    return BusinessError("AGENT_VERDICT_INVALID", message, details=details)


def _assert_verdict_shape(verdict: dict[str, Any]) -> None:
    """候选结论的**形状**校验：状态枚举、说明必填、证据必须是数组。

    形状不合法是「模型没有按契约回答」，不是「模型判错了」，因此单独一类错误，
    由调用方按 BLOCKED 处理并把原因带回。
    """
    unexpected = sorted(set(verdict) - set(VERDICT_SCHEMA["properties"]))
    if unexpected:
        raise _schema_error(
            "候选结论包含契约之外的字段，本次未采信",
            unexpected=unexpected,
            allowed=sorted(VERDICT_SCHEMA["properties"]),
        )
    missing = [key for key in VERDICT_SCHEMA["required"] if key not in verdict]
    if missing:
        raise _schema_error("候选结论缺少必填字段，本次未采信", missing=missing)
    status = verdict.get("status")
    if status not in VERDICT_STATUSES:
        raise _schema_error(
            "候选结论的状态不在允许范围内，本次未采信",
            status=status,
            allowed=sorted(VERDICT_STATUSES),
        )
    message = verdict.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _schema_error("候选结论必须给出非空的判断说明，本次未采信")
    if not isinstance(verdict.get("evidence"), list):
        raise _schema_error("候选结论的 evidence 必须是数组，本次未采信")


def _evidence_refs(verdict: dict[str, Any], *, tool_results: list[dict[str, Any]], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选结论的证据引用，逐条核对是否真的属于本次评估。

    只认两种引用：

    - `tool_call_id`：必须是本次评估**真实执行过**的工具调用；
    - `(fact_code, material_key)`：必须在本此评估冻结的事实里存在。

    引用不到的依据一律拒绝：让模型「说一个编号」而不核对，等于证据要求形同虚设。
    """
    executed = {
        str(item.get("tool_call_id"))
        for item in tool_results
        if item.get("tool_call_id")
    }
    fact_index = {
        (str(item.get("fact_code") or ""), item.get("material_key"))
        for item in facts
    }
    resolved: list[dict[str, Any]] = []
    for index, item in enumerate(verdict.get("evidence") or []):
        if not isinstance(item, dict):
            raise _schema_error("候选结论的证据引用必须是对象", index=index, value=item)
        tool_call_id = item.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            if tool_call_id not in executed:
                raise _schema_error(
                    "候选结论引用了本次评估没有执行过的工具调用，本次未采信",
                    tool_call_id=tool_call_id,
                )
            resolved.append({"tool_call_id": tool_call_id})
            continue
        fact_code = item.get("fact_code")
        material_key = item.get("material_key")
        if isinstance(fact_code, str) and fact_code:
            if material_key is not None and not isinstance(material_key, str):
                raise _schema_error("证据引用里的 material_key 必须是字符串或为空", index=index)
            if (fact_code, material_key) not in fact_index:
                raise _schema_error(
                    "候选结论引用了本次评估冻结事实之外的字段，本次未采信",
                    fact_code=fact_code,
                    material_key=material_key,
                )
            resolved.append({"fact_code": fact_code, "material_key": material_key})
            continue
        raise _schema_error(
            "候选结论的证据引用必须给出 tool_call_id 或 fact_code",
            index=index,
        )
    return resolved


def conflicts_with_tool(verdict: dict[str, Any], tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """模型结论与确定性工具结果的冲突。

    冲突的判据只有一条：工具给出了**确定的**结论（PASSED / ABNORMAL），
    而模型的结论与它相反。此时不能采信模型——确定性结果由程序算出，
    模型不能推翻它。

    工具返回 EVIDENCE_INSUFFICIENT 不算冲突：那是「没算出结论」，
    模型据其它证据给出判断是允许的。
    """
    conflicts: list[dict[str, Any]] = []
    model_status = verdict.get("status")
    if model_status not in {"PASSED", "ABNORMAL"}:
        return conflicts
    for item in tool_results:
        tool_status = item.get("status")
        if tool_status not in {"PASSED", "ABNORMAL"}:
            continue
        if tool_status != model_status:
            tool_name = str(item.get("tool_name"))
            tool_call_id = item.get("tool_call_id")
            conflicts.append(
                {
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_status": tool_status,
                    "model_status": model_status,
                    "reason": f"工具“{tool_name}”（{tool_call_id}）判定为 {tool_status}，模型结论为 {model_status}",
                }
            )
    return conflicts


def _undecided_tools(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本次评估里没有给出确定结论的工具调用。

    `FAILED`（真实执行失败）与 `NEEDS_RECONCILE`（上次停在执行中、结果未知）都不是
    「已核对」；模型据此判通过等于把一次未完成的核对当成通过。
    """
    return [
        {
            "tool_name": item.get("tool_name"),
            "tool_call_id": item.get("tool_call_id"),
            "status": item.get("status"),
            "message": item.get("message"),
            "reason": (
                f"工具“{item.get('tool_name')}”本次执行失败（{item.get('status')}），"
                "不能据此判定通过或不通过"
            ),
        }
        for item in tool_results
        if item.get("status") in {"FAILED", "NEEDS_RECONCILE"}
    ]


def validate_verdict(
    raw: dict[str, Any],
    *,
    rule: dict[str, Any],
    tool_results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """服务端校验候选结论，返回（可采信的结论, 循环状态, 未解决事项）。

    不能只靠 system prompt 约束模型。这里逐条落实：

    1. 状态只能取允许枚举，说明必填，形状不符即 BLOCKED（模型没有按契约回答）；
    2. 需要确定性判定的规则（本次有授权工具）至少要有一次**成功**的工具结果；
    3. PASSED / ABNORMAL 必须引用本次评估的工具调用或冻结事实，且引用要真实存在；
    4. 结论不得越过 EVIDENCE_INSUFFICIENT / FAILED / NEEDS_RECONCILE：
       工具没算出结论或执行失败时，模型不能给出确定结论；
    5. 与确定性工具结果冲突时不采信模型，降为「无法判断」并转人工。
    """
    _assert_verdict_shape(raw)
    refs = _evidence_refs(raw, tool_results=tool_results, facts=facts)
    verdict = {
        "status": raw["status"],
        "message": raw["message"].strip(),
        "evidence": refs,
        "rule_key": rule.get("rule_key"),
        "rule_name": rule.get("name"),
    }
    status = verdict["status"]
    if status not in {"PASSED", "ABNORMAL"}:
        # 非确定结论照原样采信：它没有越过任何确定性结果，人工复核由调用方按状态决定。
        return verdict, (LOOP_NEEDS_HUMAN if status in HUMAN_REVIEW_STATUSES else LOOP_DONE), [
            {"kind": "VERDICT_NEEDS_REVIEW", "message": verdict["message"]}
        ] if status in HUMAN_REVIEW_STATUSES else []

    blocked = _undecided_tools(tool_results)
    if blocked:
        return (
            {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": "本次工具调用未全部得到确定结果，模型给出的确定结论不予采信，需人工复核",
                "model_verdict": raw,
                "undecided_tools": blocked,
                "rule_key": rule.get("rule_key"),
                "rule_name": rule.get("name"),
            },
            LOOP_NEEDS_HUMAN,
            [{"kind": "TOOL_RESULT_UNDECIDED", **item} for item in blocked],
        )
    succeeded = [
        item for item in tool_results if item.get("status") in {"PASSED", "ABNORMAL"}
    ]
    if tools and not succeeded:
        # 需要确定性判定的规则：没有一次成功的工具结果就给出确定结论，等于凭猜测判定。
        return (
            {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": "本规则需要确定性判定，但本次没有产生任何成功的工具结果，不能判定通过或不通过",
                "model_verdict": raw,
                "rule_key": rule.get("rule_key"),
                "rule_name": rule.get("name"),
            },
            LOOP_NEEDS_HUMAN,
            [
                {
                    "kind": "DETERMINISTIC_EVIDENCE_MISSING",
                    "message": "需要确定性判定的规则缺少成功的工具结果",
                }
            ],
        )
    if not refs:
        return (
            {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": "模型的确定结论没有引用本次评估的工具调用或冻结事实，不予采信，需人工复核",
                "model_verdict": raw,
                "rule_key": rule.get("rule_key"),
                "rule_name": rule.get("name"),
            },
            LOOP_NEEDS_HUMAN,
            [{"kind": "VERDICT_EVIDENCE_MISSING", "message": "确定结论必须带证据引用"}],
        )
    conflicts = conflicts_with_tool(verdict, tool_results)
    if conflicts:
        # 模型不能推翻确定性结果：保留冲突并转人工，不采信模型结论。
        return (
            {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": "模型结论与确定性工具结果冲突，本次不采信模型结论，需人工复核",
                "model_verdict": raw,
                "conflicts": conflicts,
                "rule_key": rule.get("rule_key"),
                "rule_name": rule.get("name"),
            },
            LOOP_NEEDS_HUMAN,
            [{"kind": "VERDICT_CONFLICT", "message": item["reason"], **item} for item in conflicts],
        )
    return verdict, LOOP_DONE, []


# 冻结事实快照


def _task_facts(
    db: Session,
    task: AuditTask,
    materials: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """本任务当前有效的事实集合（合同审核走字段事实，财报走统一事实）。

    两类事实**共用同一份投影口径**（见 `domain/fact_snapshot`），差别只在取哪张表：

    - 统一事实的 `fact_code` 是业务编号（FEX-… / 计算结果编号），而规则输入引用的是
      字段身份，因此这里把编译期就确定的「业务编号 → 字段身份」索引传进去，
      让快照里的每条事实都带上 `field_key`；
    - 材料位状态随之冻结：算子按 `material_state` 判定依据是否可用，
      用当前值会让结论无法复现。
    """
    from ...entities import DocumentUpload, Evidence, TaskDocument, TaskFact, TaskFieldValue
    from sqlalchemy import select

    material_states = {
        str(item["material_key"]): item.get("material_state")
        for item in materials
        if item.get("material_key")
    }
    field_definitions = {
        str(field["field_key"]): field
        for field in task.task_schema_json["fields"]
    }
    field_code_index = {
        str(field["source_ref"]): str(field["field_key"])
        for field in field_definitions.values()
        if field.get("source_ref")
    }
    field_rows = list(
        db.scalars(
            select(TaskFieldValue)
            .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.is_current.is_(True),
                DocumentUpload.audit_task_id == task.id,
                DocumentUpload.is_current.is_(True),
                TaskDocument.audit_task_id == task.id,
                TaskDocument.material_key == TaskFieldValue.material_key,
                TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
            )
        ).all()
    )
    field_row_ids = [row.id for row in field_rows]
    evidence_ids_by_field_value: dict[int, list[int]] = {}
    if field_row_ids:
        for field_value_id, evidence_id in db.execute(
            select(Evidence.field_value_id, Evidence.id)
            .where(
                Evidence.audit_task_id == task.id,
                Evidence.field_value_id.in_(field_row_ids),
                Evidence.rule_result_id.is_(None),
            )
            .order_by(Evidence.field_value_id, Evidence.id)
        ).all():
            if field_value_id is not None:
                evidence_ids_by_field_value.setdefault(field_value_id, []).append(evidence_id)
    field_facts = fact_snapshot.facts_of_field_value_rows(
        field_rows,
        field_definitions=field_definitions,
        material_states=material_states,
        evidence_ids_by_field_value=evidence_ids_by_field_value,
    )
    task_fact_rows = list(
        db.scalars(
            select(TaskFact).where(
                TaskFact.audit_task_id == task.id,
                TaskFact.is_current.is_(True),
            )
        ).all()
    )
    task_facts = fact_snapshot.facts_of_task_fact_rows(
        task_fact_rows,
        field_code_index=field_code_index,
        field_definitions=field_definitions,
        material_states=material_states,
    )
    # 一个字段身份在快照中只能出现一次。TaskFact 决定状态与可执行值，
    # TaskFieldValue 只补充统一事实没有保存的原文和证据引用。
    extracted_by_identity: dict[tuple[str, Any], dict[str, Any]] = {}
    facts: list[dict[str, Any]] = []
    for item in task_facts:
        if item.get("fact_kind") != "EXTRACTED":
            facts.append(item)
            continue
        identity = (str(item.get("field_key") or ""), item.get("material_key"))
        if identity in extracted_by_identity:
            raise BusinessError(
                "TASK_FACT_IDENTITY_DUPLICATE",
                "同一字段与材料位存在多条当前统一事实，无法冻结唯一执行输入",
                status_code=500,
                details={"field_key": identity[0], "material_key": identity[1]},
            )
        extracted_by_identity[identity] = item

    for candidate in field_facts:
        identity = (str(candidate.get("field_key") or ""), candidate.get("material_key"))
        canonical = extracted_by_identity.pop(identity, None)
        if canonical is None:
            raise BusinessError(
                "TASK_FACT_MISSING_FOR_FIELD",
                "当前字段版本没有对应的统一事实，不能作为规则或 Agent 输入",
                status_code=500,
                details={"field_key": identity[0], "material_key": identity[1]},
            )
        if canonical.get("status") != candidate.get("status"):
            raise BusinessError(
                "TASK_FACT_FIELD_STATUS_MISMATCH",
                "当前字段版本与统一事实的状态不一致，不能继续执行",
                status_code=500,
                details={
                    "field_key": identity[0],
                    "material_key": identity[1],
                    "field_status": candidate.get("status"),
                    "fact_status": canonical.get("status"),
                },
            )
        contract_keys = ("value_type", "unit", "currency")
        mismatched_contract = {
            key: {"field": candidate.get(key), "fact": canonical.get(key)}
            for key in contract_keys
            if candidate.get(key) != canonical.get(key)
        }
        if mismatched_contract:
            raise BusinessError(
                "TASK_FACT_FIELD_CONTRACT_MISMATCH",
                "统一事实的类型、单位或币种与任务字段契约不一致",
                status_code=500,
                details={
                    "field_key": identity[0],
                    "material_key": identity[1],
                    "mismatches": mismatched_contract,
                },
            )
        if canonical.get("status") in {"PRESENT", "REVIEWED"} and canonical.get("normalized_value") is None:
            raise BusinessError(
                "TASK_FACT_VALUE_MISSING",
                "统一事实已标记为有效，但没有可执行值",
                status_code=500,
                details={"field_key": identity[0], "material_key": identity[1]},
            )
        if (
            canonical.get("status") in {"PRESENT", "REVIEWED"}
            and fact_snapshot.canonical_field_value(
                canonical.get("normalized_value"), field_definitions[identity[0]]
            )
            != fact_snapshot.canonical_field_value(
                candidate.get("normalized_value"), field_definitions[identity[0]]
            )
        ):
            raise BusinessError(
                "TASK_FACT_FIELD_VALUE_MISMATCH",
                "当前字段候选值与统一事实的有效值不一致，不能冻结规则输入",
                status_code=500,
                details={"field_key": identity[0], "material_key": identity[1]},
            )
        facts.append(
            {
                **canonical,
                "raw_value": candidate.get("raw_value"),
                "evidence_id": candidate.get("evidence_id"),
                "evidence_ids": list(candidate.get("evidence_ids") or []),
            }
        )
    if extracted_by_identity:
        identity = next(iter(extracted_by_identity))
        raise BusinessError(
            "TASK_FIELD_VALUE_MISSING_FOR_FACT",
            "当前统一事实没有对应的当前字段版本，无法补齐原文与证据",
            status_code=500,
            details={"field_key": identity[0], "material_key": identity[1]},
        )
    return facts


def _task_materials(db: Session, task: AuditTask) -> list[dict[str, Any]]:
    """本任务全部材料位的**冻结身份**，含未上传的材料位。

    未上传的材料位必须保留：材料存在性判定要区分「必传缺失」与「可选缺失」，
    只列已上传的会让必传缺失变成查不到，判不出不合格。
    """
    from .state import _material_states
    from ...entities import DocumentUpload, TaskDocument
    from sqlalchemy import select

    states = _material_states(db, task)
    documents = list(
        db.scalars(
            select(TaskDocument)
            .where(TaskDocument.audit_task_id == task.id)
            .order_by(TaskDocument.material_key)
        ).all()
    )
    upload_ids = [item.current_upload_id for item in documents if item.current_upload_id]
    uploads = (
        {
            item.id: item
            for item in db.scalars(select(DocumentUpload).where(DocumentUpload.id.in_(upload_ids))).all()
        }
        if upload_ids
        else {}
    )
    materials: list[dict[str, Any]] = []
    for document in documents:
        upload = uploads.get(document.current_upload_id)
        materials.append(
            {
                "material_key": document.material_key,
                "material_name": document.material_name,
                "required": bool(document.required),
                "document_status": document.status,
                "material_state": states.get(document.material_key),
                "upload_id": upload.id if upload else None,
                # 上传内容哈希进指纹：工具读文件或产物时依据的就是它。
                "upload_sha256": upload.sha256 if upload else None,
                "upload_revision": upload.upload_no if upload else None,
                "upload_filename": upload.original_filename if upload else None,
                "upload_content_type": upload.content_type if upload else None,
                "upload_size_bytes": upload.size_bytes if upload else None,
            }
        )
    return materials


def _snapshot_mismatches(
    record: EvaluationSnapshot,
    *,
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    tool_catalog_version: str,
    tool_catalog_hash: str,
    tool_policy_version: str,
    allowed_tools: list[dict[str, Any]],
) -> list[str]:
    """已有快照与本次冻结内容的逐项差异。

    判据是**冻结时的完整内容**：事实与材料（`facts_json`）、内容指纹、工具目录身份、
    策略身份、授权工具定义。任一列不同就不是同一份输入，必须显式冲突。
    """
    compared = [
        ("facts_json", record.facts_json, payload),
        ("fact_snapshot_hash", record.fact_snapshot_hash, snapshot["hash"]),
        ("tool_catalog_version", record.tool_catalog_version, tool_catalog_version),
        ("tool_catalog_hash", record.tool_catalog_hash, tool_catalog_hash),
        ("tool_policy_version", record.tool_policy_version, tool_policy_version),
        ("allowed_tools_json", record.allowed_tools_json, allowed_tools),
    ]
    return [name for name, recorded, requested in compared if recorded != requested]


def freeze_snapshot(
    db: Session,
    *,
    task: AuditTask,
    node_code: str,
    rule_instance_id: str,
    evaluation_id: str,
    allowed_tools: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """冻结一条规则实例的事实快照并落库，返回（ref, hash, 冻结事实）。

    冻结发生在评估开始时、**调模型之前**：本次评估的全部工具调用都从这份内容解析事实，
    不再读「当前数据」。

    幂等的口径是「先按作用域查，再逐列核对内容」：

    - 同一 (评估批次, 规则实例, 输入修订) 已有记录、且事实与材料内容、工具目录身份、
      策略身份、授权工具定义完全一致时，**复用原记录**，不重新插入；
    - 已有记录但内容不一致时抛 `AGENT_SNAPSHOT_SCOPE_CONFLICT`：同一次评估拿到了
      两份不同的输入，必须显式失败而不是覆盖；
    - 并发插入撞到作用域唯一键时，只捕获该唯一约束的冲突，回查并复用；
      其它 `IntegrityError`（其余唯一键、外键、数据长度）原样上抛，不当成成功。

    `snapshot_ref` 含评估作用域身份（见 `domain/fact_snapshot.snapshot_of`），
    因此「两条规则的事实内容恰好相同」不会再撞全局唯一键。
    """
    # 冻结的工具定义来自发布期清单；缺失即显式报错（见 frozen_tool_definitions）。
    frozen_tool_definitions(allowed_tools)
    tool_catalog_version = str(policy.get("tool_catalog_version") or "")
    tool_catalog_hash = str(policy.get("tool_catalog_hash") or "")
    tool_policy_version = str(policy.get("tool_policy_version") or "")
    input_revision = int(task.input_revision or 0)
    materials = _task_materials(db, task)
    facts = _task_facts(db, task, materials)
    scope = fact_snapshot.scope_of(
        evaluation_id=evaluation_id,
        rule_instance_id=rule_instance_id,
        input_revision=input_revision,
    )
    snapshot = fact_snapshot.snapshot_of(facts, materials, scope=scope)
    payload = fact_snapshot.snapshot_payload(
        facts,
        materials,
        task_no=task.task_no,
        node_code=node_code,
        evaluation_id=evaluation_id,
        rule_instance_id=rule_instance_id,
        input_revision=input_revision,
    )
    existing = _snapshot_of_scope(db, evaluation_id=evaluation_id, rule_instance_id=rule_instance_id, input_revision=input_revision)
    if existing is not None:
        _assert_snapshot_same(
            existing,
            payload=payload,
            snapshot=snapshot,
            tool_catalog_version=tool_catalog_version,
            tool_catalog_hash=tool_catalog_hash,
            tool_policy_version=tool_policy_version,
            allowed_tools=allowed_tools,
            task_no=task.task_no,
        )
        return _snapshot_identity(existing)
    record = EvaluationSnapshot(
        snapshot_ref=snapshot["ref"],
        audit_task_id=task.id,
        evaluation_id=evaluation_id,
        rule_instance_id=rule_instance_id,
        node_code=node_code,
        input_revision=input_revision,
        facts_json=payload,
        fact_snapshot_hash=snapshot["hash"],
        tool_catalog_version=tool_catalog_version,
        tool_catalog_hash=tool_catalog_hash,
        tool_policy_version=tool_policy_version,
        allowed_tools_json=allowed_tools,
    )
    try:
        # 自己的 SAVEPOINT：并发撞唯一键时只回滚这一次插入，
        # 外层事务（调用方的 begin_nested）仍可继续，回查与复用才不会连带失败。
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError as exc:
        if not _is_snapshot_scope_conflict(exc):
            raise
        logger.info(
            "冻结快照并发冲突，回查并复用 task_no=%s evaluation_id=%s rule_instance_id=%s",
            task.task_no,
            evaluation_id,
            rule_instance_id,
        )
        concurrent = _snapshot_of_scope(
            db,
            evaluation_id=evaluation_id,
            rule_instance_id=rule_instance_id,
            input_revision=input_revision,
        )
        if concurrent is None:
            raise BusinessError(
                "AGENT_SNAPSHOT_SCOPE_CONFLICT",
                "同一次评估的冻结快照正在被另一次执行写入，本次未继续；请稍后重试",
                status_code=409,
                details={
                    "evaluation_id": evaluation_id,
                    "rule_instance_id": rule_instance_id,
                    "input_revision": input_revision,
                },
            ) from exc
        _assert_snapshot_same(
            concurrent,
            payload=payload,
            snapshot=snapshot,
            tool_catalog_version=tool_catalog_version,
            tool_catalog_hash=tool_catalog_hash,
            tool_policy_version=tool_policy_version,
            allowed_tools=allowed_tools,
            task_no=task.task_no,
        )
        return _snapshot_identity(concurrent)
    return {
        "ref": snapshot["ref"],
        "hash": snapshot["hash"],
        "count": snapshot["count"],
        "material_count": snapshot["material_count"],
    }


def _snapshot_of_scope(
    db: Session,
    *,
    evaluation_id: str,
    rule_instance_id: str,
    input_revision: int,
) -> EvaluationSnapshot | None:
    """按 (评估批次, 规则实例, 输入修订) 取那一份冻结快照。"""
    from sqlalchemy import select

    return db.scalar(
        select(EvaluationSnapshot).where(
            EvaluationSnapshot.evaluation_id == evaluation_id,
            EvaluationSnapshot.rule_instance_id == rule_instance_id,
            EvaluationSnapshot.input_revision == input_revision,
        )
    )


def _snapshot_identity(record: EvaluationSnapshot) -> dict[str, Any]:
    """快照身份的可读回报（复用已有记录与新建记录给出同一形状）。"""
    payload = record.facts_json if isinstance(record.facts_json, dict) else {}
    facts = payload.get("facts")
    materials = payload.get("materials")
    return {
        "ref": record.snapshot_ref,
        "hash": record.fact_snapshot_hash,
        "count": len(facts) if isinstance(facts, list) else 0,
        "material_count": len(materials) if isinstance(materials, list) else 0,
    }


def _assert_snapshot_same(
    record: EvaluationSnapshot,
    *,
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    tool_catalog_version: str,
    tool_catalog_hash: str,
    tool_policy_version: str,
    allowed_tools: list[dict[str, Any]],
    task_no: str,
) -> None:
    """已有快照必须与本次输入完全一致，否则显式冲突。"""
    differing = _snapshot_mismatches(
        record,
        payload=payload,
        snapshot=snapshot,
        tool_catalog_version=tool_catalog_version,
        tool_catalog_hash=tool_catalog_hash,
        tool_policy_version=tool_policy_version,
        allowed_tools=allowed_tools,
    )
    if differing:
        raise BusinessError(
            "AGENT_SNAPSHOT_SCOPE_CONFLICT",
            "同一次评估的输入已冻结为另一份内容，本次未重新冻结；"
            "同一次评估不允许拿到两份不同的输入",
            status_code=409,
            details={
                "snapshot_ref": record.snapshot_ref,
                "task_no": task_no,
                "evaluation_id": record.evaluation_id,
                "rule_instance_id": record.rule_instance_id,
                "input_revision": int(record.input_revision),
                "differing": sorted(differing),
            },
        )


def _is_snapshot_scope_conflict(exc: IntegrityError) -> bool:
    """只认「作用域唯一键」这一种冲突。

    其余 `IntegrityError`（其它唯一键、外键、长度超限）说明数据或代码有问题，
    必须原样上抛——把它们当成「有人并发写入了同一份快照」会让真实缺陷静默消失。
    """
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    code = args[0] if args else None
    # MySQL 1062 = ER_DUP_ENTRY。
    if code != 1062:
        return False
    return "uq_evaluation_snapshot_scope" in str(orig)


def load_snapshot(db: Session, *, task: AuditTask, snapshot_ref: str) -> EvaluationSnapshot:
    """按引用读回冻结内容。读不到即显式失败——不能退回读「当前数据」。"""
    from sqlalchemy import select

    record = db.scalar(
        select(EvaluationSnapshot).where(
            EvaluationSnapshot.snapshot_ref == snapshot_ref,
            EvaluationSnapshot.audit_task_id == task.id,
        )
    )
    if record is None:
        raise BusinessError(
            "AGENT_SNAPSHOT_MISSING",
            "规则评估的冻结事实快照不存在，本次调用未执行",
            status_code=409,
            details={"snapshot_ref": snapshot_ref, "task_no": task.task_no},
        )
    if record.fact_snapshot_hash == "" or not isinstance(record.facts_json, dict):
        raise BusinessError(
            "AGENT_SNAPSHOT_INVALID",
            "规则评估的冻结事实快照内容不完整，本次调用未执行",
            status_code=500,
            details={"snapshot_ref": snapshot_ref},
        )
    payload = record.facts_json
    if payload.get("version") != fact_snapshot.FACT_SNAPSHOT_VERSION:
        # 口径不同就不能按当前规则解释：旧口径的取值投影与新口径不同，
        # 强行读会让「指纹对得上、取值却是另一个形式」这种情况无从发现。
        raise BusinessError(
            "AGENT_SNAPSHOT_INVALID",
            "规则评估的冻结事实快照口径与当前版本不一致，本次调用未执行",
            status_code=409,
            details={
                "snapshot_ref": snapshot_ref,
                "frozen_version": payload.get("version"),
                "current_version": fact_snapshot.FACT_SNAPSHOT_VERSION,
            },
        )
    if not isinstance(payload.get("facts"), list) or not isinstance(payload.get("materials"), list):
        raise BusinessError(
            "AGENT_SNAPSHOT_INVALID",
            "规则评估的冻结事实快照缺少事实或材料位身份，本次调用未执行",
            status_code=500,
            details={"snapshot_ref": snapshot_ref},
        )
    if int(record.input_revision) != int(task.input_revision or 0):
        # 输入修订已变：这份快照不再是当前输入。继续用它会让结论依据旧输入，
        # 而任务页面显示的是新输入——两者矛盾且无法解释。
        raise BusinessError(
            "AGENT_SNAPSHOT_STALE",
            "任务输入已变化，之前冻结的事实快照不再适用，本次调用未执行",
            status_code=409,
            details={
                "snapshot_ref": snapshot_ref,
                "snapshot_revision": int(record.input_revision),
                "task_revision": int(task.input_revision or 0),
            },
        )
    return record


def inputs_of_snapshot(record: EvaluationSnapshot, rule: dict[str, Any]) -> list[dict[str, Any]]:
    """工具输入只从冻结快照解析（见 `fact_snapshot.resolve_rule_inputs`）。"""
    payload = record.facts_json if isinstance(record.facts_json, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
    return fact_snapshot.resolve_rule_inputs(facts, fact_snapshot.declared_inputs_of(rule))


def facts_of_snapshot(record: EvaluationSnapshot) -> list[dict[str, Any]]:
    """冻结快照里的事实清单（候选结论的证据引用按它核对）。"""
    payload = record.facts_json if isinstance(record.facts_json, dict) else {}
    facts = payload.get("facts")
    return [item for item in facts if isinstance(item, dict)] if isinstance(facts, list) else []


# 工具范围与执行


def frozen_tool_definitions(allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """从冻结的授权工具清单里取出**完整工具定义**。

    模型看到的函数定义必须来自发布期冻结的那一份，不能由当前 `tool_catalog`
    现场重新生成：目录升级后旧任务恢复执行时，模型看到的工具定义会与首次不同，
    历史结论因此无法复现。

    清单里的条目缺少 `definition` 时**显式报错**，不静默改用当前目录——
    「旧任务缺少完整冻结定义」是必须被看见的事实，不是可以绕过的兼容问题。
    """
    definitions: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in allowed_tools or []:
        if not isinstance(item, dict) or not item.get("tool_id"):
            continue
        definition = item.get("definition")
        if not isinstance(definition, dict) or not definition.get("name"):
            missing.append(str(item["tool_id"]))
            continue
        definitions.append(definition)
    if missing:
        raise BusinessError(
            "TOOL_DEFINITION_NOT_FROZEN",
            "本次评估的授权工具清单里没有冻结的工具定义，不能用当前工具目录重新生成："
            + "、".join(f"“{item}”" for item in sorted(missing))
            + "。请重新发布该场景配置后再执行",
            status_code=409,
            details={"tools": sorted(missing)},
        )
    return definitions


def tools_for_node(
    manifest: dict[str, Any],
    *,
    node_code: str,
    rule_codes: tuple[str, ...] | None = None,
    rule_keys: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """某个节点本次允许 Agent 调用的工具定义（发布期冻结的那一份）。

    范围来自已发布配置的 `allowed_tools`（发布期由规则实际引用的能力算出），
    不是把整个工具目录倒给模型。新版本按 `rule_keys` 收窄到执行分支；
    旧版本清单没有分支键时按 `rule_codes` 读取原冻结授权。

    清单里没有该节点时返回空列表——Agent 在该节点没有可用工具，这是配置事实。
    清单存在但缺少冻结定义时显式报错（见 `frozen_tool_definitions`）。
    """
    return frozen_tool_definitions(allowed_tools_of(
        manifest, node_code=node_code, rule_codes=rule_codes, rule_keys=rule_keys,
    ))


def allowed_tools_of(
    manifest: dict[str, Any],
    *,
    node_code: str,
    rule_codes: tuple[str, ...] | None = None,
    rule_keys: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """本次评估允许的工具范围（发布期冻结的那份清单，按节点与规则收窄）。

    与 `tools_for_node` 的区别：这里返回**已发布配置里的原始条目**，
    它同时是「这些工具服务于哪条规则」的答案，随快照一起冻结。
    """
    allowed = manifest.get("allowed_tools")
    if not isinstance(allowed, list):
        return []
    wanted_codes = set(rule_codes) if rule_codes is not None else None
    wanted_keys = set(rule_keys) if rule_keys is not None else None
    return [
        dict(item)
        for item in allowed
        if isinstance(item, dict)
        and item.get("node_code") == node_code
        and item.get("tool_id")
        and (
            (wanted_keys is None or bool(wanted_keys & set(item["rule_keys"])))
            if "rule_keys" in item
            else (wanted_codes is None or bool(wanted_codes & set(item.get("rule_codes") or [])))
        )
    ]


def policy_of(manifest: dict[str, Any]) -> dict[str, Any]:
    """已发布配置里冻结的工具策略与目录身份。

    三项都取自 manifest，**不回落到当前常量**：回落到当前值等于让旧任务静默改用
    新目录/新策略执行，历史结论无法复现。任一项缺失即显式报错（旧格式 manifest）。
    """
    policy = {
        "tool_catalog_version": manifest.get("tool_catalog_version"),
        "tool_catalog_hash": manifest.get("tool_catalog_hash"),
        "tool_policy_version": manifest.get("tool_policy_version"),
    }
    missing = sorted(name for name, value in policy.items() if not value)
    if missing:
        raise BusinessError(
            "TOOL_POLICY_NOT_FROZEN",
            "本次发布配置里没有冻结的" + "、".join(missing) + "，无法确定模型当时看到的是哪一版工具定义",
            status_code=409,
            details={"missing": missing},
        )
    return policy


def execution_context(
    db: Session,
    *,
    task: AuditTask,
    snapshot: EvaluationSnapshot,
    scene_code: str,
    node_code: str,
    rule_instance_id: str,
    material_refs: tuple[str, ...],
) -> ToolExecutionContext:
    """本次工具调用的运行期信封。

    评估身份、快照身份、工具目录身份与策略版本全部取自**冻结记录**，
    不在这里重新生成、也不取当前目录常量：用当前值覆盖旧任务的冻结身份会让
    同一个 `tool_call_id` 在恢复时被判成协议冲突，本可复用的结果反而失败。
    """
    return ToolExecutionContext(
        task_id=task.id,
        evaluation_id=snapshot.evaluation_id,
        rule_instance_id=rule_instance_id,
        scene_code=scene_code,
        node_code=node_code,
        material_refs=material_refs,
        fact_snapshot_ref=snapshot.snapshot_ref,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        tool_catalog_version=snapshot.tool_catalog_version,
        tool_catalog_hash=snapshot.tool_catalog_hash,
        tool_policy_version=snapshot.tool_policy_version or None,
        caller_identity="audit_agent",
        database_session=db,
    )


def published_tool_parameters(
    rule: dict[str, Any],
    manifest: dict[str, Any],
    tool_id: str | None,
) -> dict[str, Any]:
    spec = tool_catalog.tool_spec(tool_id)
    if spec is None:
        raise BusinessError(
            "TOOL_NOT_REGISTERED",
            "已发布规则引用的执行能力不在工具目录中",
            status_code=500,
            details={"rule_key": rule.get("rule_key"), "tool_id": tool_id},
        )
    rule_parameters = rule.get("parameters")
    if not isinstance(rule_parameters, dict):
        raise BusinessError(
            "TOOL_RULE_PARAMETERS_MISSING",
            "已发布规则缺少参数对象，本次工具调用未执行",
            status_code=500,
            details={"rule_key": rule.get("rule_key")},
        )
    accepted = spec.input_schema["properties"]["parameters"]["properties"]
    published = {key: value for key, value in rule_parameters.items() if key in accepted}
    if "material_refs" in accepted:
        published["material_refs"] = list(rule.get("materials") or [])
    if "source_rule_codes" in accepted:
        from .rules import _risk_source_result_keys

        published["source_rule_codes"] = _risk_source_result_keys(rule, manifest)
    return published


def invoke_tool(
    db: Session,
    *,
    call: dict[str, Any],
    inputs: list[dict[str, Any]],
    context: ToolExecutionContext,
    runtime: dict[str, Any] | None,
    rule: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], AgentToolCall]:
    """经账本执行一次工具调用：登记 → 判定恢复情形 →（必要时）执行 → 记录结果。

    恢复情形决定要不要真的执行：已成功直接复用结果、执行中进待核查、
    失败保留原错误（重试必须换新的调用标识）。身份任一列不同则账本直接报协议冲突。

    事务边界：登记与 `RUNNING` 各自短事务提交，工具执行期间**不持有**行锁；
    结果写回也是一个短事务。工具可能是真实外部调用，长时间持锁会把并发请求堵死。
    """
    record, recovery = ledger.begin_call(
        db,
        tool_call_id=call["tool_call_id"],
        tool_name=call["tool_name"],
        arguments=call.get("arguments"),
        context=context,
    )
    if recovery == ledger.RECOVERY_REUSE:
        return ledger.reuse_result(record), record
    if recovery == ledger.RECOVERY_RECONCILE:
        # 可能已发出但结果未知：不重跑，把这一事实交回循环。
        return ledger.reconcile_message(record), record
    if recovery == ledger.RECOVERY_RETRY:
        # 上次失败：不自动重试，把原错误交回模型，由它换一个新的调用标识再来。
        return ledger.failed_message(record), record
    ledger.mark_running(db, record)
    # 执行前先提交 RUNNING：工具可能真实调用外部服务，进程被杀时
    # 留下的 RUNNING 正是「状态未知、必须核查」的真实表达。提交后即释放行锁。
    db.commit()
    try:
        published_tool_id = capabilities.capability_id_of_rule(rule)
        published_parameters = published_tool_parameters(rule, manifest, published_tool_id)
        result = tool_service.invoke(
            call["tool_name"],
            call.get("arguments"),
            inputs=inputs,
            runtime=runtime,
            context=context,
            published_parameters=published_parameters,
            published_tool_id=published_tool_id,
        )
    except BusinessError as exc:
        ledger.mark_failed(db, record, exc)
        db.commit()
        # 业务错误如实交回模型（缺输入、参数非法），不吞掉。
        return {"status": "FAILED", "code": exc.code, "message": exc.message}, record
    except Exception as exc:  # noqa: BLE001 - 未预期异常必须落账后暴露
        ledger.mark_failed(db, record, exc)
        db.commit()
        logger.exception("工具调用发生未处理异常 tool=%s", call.get("tool_name"))
        raise
    ledger.mark_succeeded(db, record, result)
    db.commit()
    return result, record


def tool_result_entry(call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """写进 State 的一条工具结果（与账本对账，state 用于流程推进）。

    两个标识都要留：`tool_call_id` 是账本身份（模型引用证据用它），
    `provider_tool_call_id` 是回填消息时的对应关系。
    """
    return {
        "tool_call_id": call["tool_call_id"],
        "provider_tool_call_id": call.get("provider_tool_call_id"),
        "tool_name": call["tool_name"],
        "arguments": call.get("arguments"),
        "status": result.get("status"),
        "message": result.get("message"),
        "result": result,
    }
