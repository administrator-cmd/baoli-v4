"""规则执行 Agent：直接抽取、计算和判断，按需调用真实工具；输入和消息由图状态保存。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...constants.execution import FACT_KIND_DERIVED
from ...constants.status import LOOP_DONE, MODEL_VERDICT_STATUSES
from ...domain import fact_projection, tool_catalog
from ...domain.execution_mode import (
    MODE_AGGREGATE,
    MODE_CALCULATE,
    MODE_CROSS_SCOPE_COMPARE,
    MODE_DERIVE_RISK,
    MODE_EXTRACT,
    MODE_MATERIAL_CHECK,
    MODE_REPORT,
    MODE_UNKNOWN,
    MODE_VALUE_COMPARE,
    SOURCE_ONLY_MODES,
    execution_mode_label,
)
from ...domain.tool_context import ToolExecutionContext
from ...entity.tasks import AuditTask
from ...integrations.llm.gateway import ModelTurn, call_chat
from .prompts import (
    BATCH_PROMPT_SUFFIX,
    EXTRACTION_BATCH_SCHEMA,
    SYSTEM_PROMPT,
    VERDICT_RESPONSE_FORMAT,
    VERDICT_SCHEMA,
)
from .tools import runtime as tool_service

logger = logging.getLogger(__name__)

EXECUTION_MODE_GUIDANCE: dict[str, str] = {
    MODE_MATERIAL_CHECK: "只检查本条声明的材料/文件本身（格式、大小、页数、印章等）；不比较字段值，不读取未声明的材料。",
    MODE_VALUE_COMPARE: "只使用下方列出的输入字段事实进行比较或判断；没有字段输入时，只使用本条声明的材料/产物，不读取未声明材料。",
    MODE_CROSS_SCOPE_COMPARE: "只比较下方列出的输入事实；没有字段输入时，只比较本条声明的材料/产物，不读取未声明材料。",
    MODE_AGGREGATE: "只汇总下方来源规则结果；不得重新执行子规则、不得读取材料或字段、不得新增子规则未给出的结论。",
    MODE_DERIVE_RISK: "只根据下方来源规则结果与风险成立条件推导风险；不得重新检查材料或字段。",
    MODE_EXTRACT: "只抽取本条声明的字段；不做超出字段取值的业务判断。",
    MODE_CALCULATE: "按本条公式与参数计算；不重新抽取字段。",
    MODE_REPORT: "按报告章节要求生成正文；不重新执行上游规则。",
    MODE_UNKNOWN: "执行方式未确定；不得猜测执行动作，按证据不足说明原因。",
}


def _without_origin(value: Any) -> Any:
    """去掉模型不需要的来源快照（Excel 工作表名、行号和原始单元格）。

    来源信息只用于页面溯源与导入回查，模型执行只依赖规则要求、输入身份与真实取值。
    """
    if isinstance(value, dict):
        return {key: _without_origin(item) for key, item in value.items() if key != "origin"}
    if isinstance(value, list):
        return [_without_origin(item) for item in value]
    return value


# 上游规则结果里下游真正需要的身份与业务结论。
# evidence、tool_findings、report、risk_context 等属于该条结果的诊断与展示，不在规则之间重复传递。
_UPSTREAM_RESULT_KEYS = ("rule_result_id", "rule_key", "rule_code", "rule_name", "status", "message", "node_code")


def _upstream_result_view(item: dict[str, Any]) -> dict[str, Any]:
    """上游规则结果的紧凑视图：身份、状态、说明与业务结论，不带完整 CSV 负载。

    同时接受完整上下文项（身份在顶层、取值负载在 result）和已经压缩过的视图，
    规则上下文与报告数据因此可以复用同一份视图；证据校验依赖的 rule_result_id、
    rule_key、rule_code 始终保留在顶层。
    """
    payload = item.get("result")
    if not isinstance(payload, dict):
        payload = item
    return {
        **{key: item.get(key) for key in _UPSTREAM_RESULT_KEYS},
        "basis": payload.get("basis", []),
        "missing_items": payload.get("missing_items", []),
        "outputs": payload.get("outputs", []),
        # 报告与风险处置需要等级、处置建议和下一步；这些是业务结论，不是执行诊断。
        "risk_level": payload.get("risk_level"),
        "action": payload.get("action"),
        "next_action": payload.get("next_action") or "",
    }


def build_rule_context(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    material_names: dict[str, str],
) -> str:
    """规则的评估上下文：执行方式 + 规则要求 + 该动作真正需要的事实。"""
    tool_id = "LLM 与可用工具"
    mode = str(rule.get("execution_mode") or MODE_UNKNOWN)
    source_only = mode in SOURCE_ONLY_MODES
    material_only = mode == MODE_MATERIAL_CHECK
    published_parameters = dict(rule.get("parameters") or {})
    configuration = _without_origin({key: value for key, value in rule.items()
                                     if key not in {"upstream_facts", "upstream_results", "available_materials", "report_data"}})
    # 产出身份只能取本规则声明的字段键与结果编号；单独列出来，避免模型自造 code。
    allowed_codes = [str(item["field_key"]) for item in rule.get("output_definitions") or []
                     if isinstance(item, dict) and item.get("field_key")]
    if rule.get("result_code"):
        allowed_codes.append(str(rule["result_code"]))
    lines = [
        f"规则编号：{rule.get('rule_code') or rule.get('rule_key')}",
        f"规则名称：{rule.get('name')}",
        f"本条执行方式：{mode}（{execution_mode_label(mode)}）",
        EXECUTION_MODE_GUIDANCE.get(mode, EXECUTION_MODE_GUIDANCE[MODE_UNKNOWN]),
        f"审核要求：{rule.get('requirement') or '（未填写）'}",
        f"执行能力：{tool_id}",
        "规则原始配置：" + json.dumps(configuration, ensure_ascii=False, sort_keys=True),
        "业务执行参数：" + json.dumps(published_parameters, ensure_ascii=False, sort_keys=True),
        "threshold 如为十进制字符串，工具调用时按工具 Schema 填 JSON 数字；实际计算仍采用发布原值。",
        "",
    ]
    if allowed_codes:
        lines.append("本条规则允许的产出 code（outputs[].code 只能逐字取以下值，不要自造也不要重复）：")
        lines.append("、".join(allowed_codes))
        lines.append("")
    else:
        lines.append("本条规则不产出字段取值或计算结果：outputs 必须为空数组 []，判断依据写进 basis、证据写进 evidence。")
        lines.append("")
    if inputs and not source_only and not material_only:
        lines.append("可用字段事实（工具 inputs 与证据引用都按 field_key、material_key 两个字段填写，不要拼成一个字符串）：")
        for item in inputs:
            material_key = item.get("material_key")
            lines.append(
                f"- field_key={item.get('field_key')}，material_key={material_key}"
                f"（{material_names.get(material_key, material_key)}）："
                f"原文={item.get('raw_value')!r}，标准化值={item.get('normalized_value')!r}，"
                f"状态={item.get('status')}，类型={item.get('value_type')}，"
                f"单位={item.get('unit') or '未声明'}，币种={item.get('currency') or '未声明'}"
            )
        if any(item.get("status") == "NOT_EXECUTED" for item in inputs):
            lines.append(
                "状态为 NOT_EXECUTED 的输入表示上游没有形成取值，不能据此认定材料未载明，"
                "也不能把它当作事实证据引用；应返回 EVIDENCE_INSUFFICIENT 并说明无法判断。"
            )
    elif mode in {MODE_VALUE_COMPARE, MODE_CROSS_SCOPE_COMPARE}:
        lines.append("本条规则没有声明字段输入；只使用本条允许的材料/产物，按规则要求判断，不读取未声明材料。")
    # 上面已逐条打印的 inputs 与 upstream_facts 是同一批字段事实，按身份去重后只保留未覆盖的部分。
    # inputs 没有打印时（来源规则、材料检查）不能视为已覆盖，否则会丢掉模型看不到的数据。
    visible_inputs = inputs if inputs and not source_only and not material_only else []
    covered_inputs = {
        (item.get("field_key") or item.get("fact_code"), item.get("material_key"))
        for item in visible_inputs
    }
    upstream_facts = [
        item for item in rule.get("upstream_facts") or []
        if (item.get("field_key") or item.get("fact_code"), item.get("material_key")) not in covered_inputs
    ]
    # 报告正文按章节从 report_data 取数；再把全量字段事实打印一遍属于同一份数据重复输入。
    if upstream_facts and not source_only and rule.get("kind") != "REPORT":
        lines.extend([
            "",
            "本条规则可直接使用的上游字段事实（仅限以下身份）：",
        ])
        lines.extend(json.dumps(item, ensure_ascii=False, default=str) for item in upstream_facts)
    # 报告正文数据由 report_data 按章节提供，不再重复打印完整 upstream_results；
    # 结论校验使用的原始 upstream_results 由调用方单独保存，不受这里是否打印影响。
    upstream_results = rule.get("upstream_results") or []
    if (source_only or upstream_results) and rule.get("kind") != "REPORT":
        lines.extend([
            "",
            "本条规则可直接引用的上游规则结果（evidence 使用其中真实 rule_result_id）：",
        ])
        if upstream_results:
            lines.extend(json.dumps(_upstream_result_view(item), ensure_ascii=False, default=str)
                         for item in upstream_results)
        else:
            lines.append("（没有可用的来源规则结果；不得自行补做来源检查，按证据不足说明缺少哪些来源结果。）")
    if rule.get("report_data") is not None:
        lines.extend(["", "按报告章节关联的业务数据：" + json.dumps(rule["report_data"], ensure_ascii=False, default=str)])
    if rule.get("available_materials") and not source_only:
        lines.extend([
            "",
            "本任务当前材料及上传状态（正文读取 OCR，印章读取 SEAL；未上传也如实保留）：",
            *[
                json.dumps({"material_key": item.get("material_key"), "name": item.get("material_name"),
                            "uploaded": item.get("upload_id") is not None,
                            "filename": item.get("upload_filename"), "content_type": item.get("upload_content_type")}, ensure_ascii=False)
                for item in rule["available_materials"]
                if isinstance(item, dict)
            ],
        ])
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
        {"role": "system", "content": SYSTEM_PROMPT + "\n最终结论的数据结构：" + json.dumps(VERDICT_SCHEMA, ensure_ascii=False)},
        {"role": "user", "content": build_rule_context(rule, inputs, manifest=manifest, material_names=material_names)},
    ]


def initial_batch_messages(
    rules: list[dict[str, Any]],
    *,
    material_key: str,
    material_name: str,
    field_contexts: dict[str, dict[str, Any]],
    field_codes: dict[str, str],
) -> list[dict[str, Any]]:
    """批量字段抽取的消息历史：一份材料、多个字段，只读取一次正文。

    每条规则必须是单字段抽取（一个 output_definition、无 inputs）；调用方已按材料分好组。
    字段定义逐条列出，避免模型把抽取要求当成材料原文或漏字段。
    模型只看到本批的短编号：内部字段键是 20 位不透明字符串，让模型逐字抄写容易出错。
    """
    definitions = []
    for rule in rules:
        supported = rule.get("output_definitions") or []
        if len(supported) != 1:
            raise BusinessError(
                "EXTRACTION_BATCH_INVALID", "批量抽取要求每条规则恰好声明一个输出字段",
                status_code=500, details={"rule_key": rule.get("rule_key"), "outputs": len(supported)},
            )
        definitions.append(supported[0])
    lines = [
        f"本次是批量字段抽取：在材料「{material_name}」（material_key={material_key}）上抽取 {len(definitions)} 个字段。",
        "先使用 TASK_DATA_READ 读取字段定义指定的产物：artifact_source=SEAL 时读取 SEAL，其余读取 OCR。",
        "同一次读取覆盖全部字段，不要为每个字段重复读取材料。",
        "",
        "待抽取字段：",
    ]
    for index, definition in enumerate(definitions, 1):
        field_key = definition["field_key"]
        code = field_codes.get(field_key)
        if not code:
            raise BusinessError(
                "EXTRACTION_BATCH_CODE_MISSING", "批量抽取缺少字段短编号",
                status_code=500, details={"field_key": field_key},
            )
        lines.extend([
            f"{index}. code={code}",
            f"   名称：{definition.get('name')}",
            f"   类型：{definition.get('value_type')}，单位={definition.get('unit') or '未声明'}，币种={definition.get('currency') or '未声明'}",
            f"   抽取要求：{definition.get('instruction') or '（未填写）'}",
        ])
        attributes = {
            key: definition.get(key) for key in (
                "artifact_source", "extraction_strategy", "structure_schema",
                "period_scope", "statement_scope", "entity_scope", "required",
            ) if definition.get(key) is not None
        }
        if attributes:
            lines.append("   执行属性：" + json.dumps(attributes, ensure_ascii=False, sort_keys=True, default=str))
        requirement = rules[index - 1].get("requirement")
        parameters = rules[index - 1].get("parameters") or {}
        if requirement or parameters:
            lines.append("   规则要求与参数：" + json.dumps(
                {"requirement": requirement, "parameters": parameters},
                ensure_ascii=False, sort_keys=True, default=str,
            ))
        context = field_contexts[field_key]
        if context.get("upstream_facts") or context.get("upstream_results"):
            lines.append("   当前可用事实与上游结果：" + json.dumps(context, ensure_ascii=False, default=str))
    lines.extend([
        "",
        "输出要求：",
        "- 只返回 fields 数组，每个字段一项，code 逐字取自上面列出的短编号，不遗漏、不重复、不自造。",
        "- status 是本字段的业务结论：取到真实值为 COMPUTED，材料中确认没有该内容为 ABNORMAL，无法判断为 EVIDENCE_INSUFFICIENT。",
        "- field_status=PRESENT 时 value 填按字段类型标准化后的真实取值、raw_value 只填字段本身的原始写法；NOT_PRESENT 时 value 必须是 null 并在 message 说明未载明；"
        "EVIDENCE_INSUFFICIENT 时 value 必须是 null 并说明缺少什么。",
        "- evidence 每一项都属于本字段：读取材料的引用给 tool_call_id、artifact_id、material_key、quote_text，"
        "quote_text 必须是读取结果中的原文片段；output_code 由系统按字段补齐，不用填写。",
        "- quote_text 只取直接支撑该字段的最小原文片段：表格只引用目标值所在单元格，不复制整行、整表或无关列。",
        "- message 是面向审核用户的一句结论；basis 记录判断依据；missing_items 记录该字段的业务缺项；没有时用空数组。",
        "",
        "批量抽取结论的数据结构：" + json.dumps(EXTRACTION_BATCH_SCHEMA, ensure_ascii=False),
    ])
    return [
        {"role": "system", "content": SYSTEM_PROMPT + BATCH_PROMPT_SUFFIX},
        {"role": "user", "content": "\n".join(lines)},
    ]


def call_model_once(
    messages: list[dict[str, Any]], *, tools: list[dict[str, Any]], tool_choice: Any = "auto",
    idempotency_key: str | None = None,
) -> ModelTurn:
    """调用模型一次，返回这一次的响应契约。

    这里是整个 Agent 循环里唯一的模型调用点。工具执行不由本函数完成——
    模型请求了工具就由 Graph 的 `execute_tool_calls` 节点执行，再回到 `call_model`。
    """
    return call_chat(
        messages,
        tools=tools or None,
        tool_choice=tool_choice,
        # 工具轮次允许原生函数调用；JSON 格式只约束没有工具的直接结论请求。
        response_format=None if tools else VERDICT_RESPONSE_FORMAT,
        idempotency_key=idempotency_key,
    )


def assistant_message_of(turn: ModelTurn) -> dict[str, Any]:
    """把模型这一轮完整组装的响应并入消息历史。

    `tool_calls` 使用网关按流式 index 组装的原始标识和参数字符串，不重新生成标识或序列化参数。
    这里取 `raw_message`，保留模型返回的推理内容等消息字段，只补 role。
    """
    message = dict(turn.raw_message or {})
    message["role"] = "assistant"
    return message


def pending_calls_of(turn: ModelTurn) -> list[dict[str, Any]]:
    """本轮模型请求的工具调用，写进 State 的 `pending_tool_calls`。

    `tool_call_id` 用于本次评估内定位工具结果；`provider_tool_call_id` 保留供应商标识，
    回填 `tool` 消息时必须用它——模型认的是自己给的那个。当前两者相同。
    """
    return [
        {
            "tool_call_id": call.tool_call_id,
            "provider_tool_call_id": call.tool_call_id,
            "tool_name": call.tool_name,
            "arguments": call.arguments,
        }
        for call in turn.tool_calls
    ]




def tool_message(call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """把真实结果回填到对应的供应商工具消息。"""
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

    要求整体是一个 JSON 对象（见 `VERDICT_RESPONSE_FORMAT`）。
    模型偶尔把整个对象包在 Markdown 代码块里：这是排版产物，剥掉后仍是同一份 JSON。
    解析不出对象时返回 None，由调用方按「模型没有给出可解析的结论」处理（BLOCKED），
    不在说明文字里扫描「最后一行像 JSON 的东西」——那会把片段当成结论。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _schema_error(message: str, **details: Any) -> BusinessError:
    return BusinessError("AGENT_VERDICT_INVALID", message, details=details)


def _assert_verdict_shape(verdict: dict[str, Any]) -> None:
    """候选结论的形状校验：状态枚举、说明必填、证据必须是数组。

    形状不合法是「模型没有按契约回答」，不是「模型判错了」，因此单独一类错误，
    由调用方按 BLOCKED 处理并把原因带回。
    """
    missing = [key for key in VERDICT_SCHEMA["required"] if key not in verdict]
    if missing:
        raise _schema_error("候选结论缺少必填字段，本次未采信", missing=missing)
    status = verdict.get("status")
    if not isinstance(status, str) or status not in MODEL_VERDICT_STATUSES:
        raise _schema_error(
            "候选结论的状态不在允许范围内，本次未采信",
            status=status,
            allowed=sorted(MODEL_VERDICT_STATUSES),
        )
    message = verdict.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _schema_error("候选结论必须给出非空的判断说明，本次未采信")
    if not isinstance(verdict.get("evidence"), list):
        raise _schema_error("候选结论的 evidence 必须是数组，本次未采信")
    basis = verdict.get("basis")
    if not isinstance(basis, list) or any(
        not isinstance(item, dict) or set(item) != {"label", "value", "explanation"}
        or not isinstance(item["label"], str) or not item["label"].strip()
        or not isinstance(item["explanation"], str) for item in basis
    ):
        raise _schema_error("basis 必须为包含中文业务 label、value 和 explanation 的对象数组")
    missing_items = verdict.get("missing_items")
    if not isinstance(missing_items, list) or any(not isinstance(item, str) or not item.strip() for item in missing_items):
        raise _schema_error("missing_items 必须为最终业务缺项的非空字符串数组")
    if not isinstance(verdict.get("next_action"), str):
        raise _schema_error("next_action 必须为字符串，无处理建议时填空字符串")


def _evidence_refs(
    verdict: dict[str, Any], *, tool_results: list[dict[str, Any]], facts: list[dict[str, Any]],
    upstream_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """候选结论的证据引用，逐条核对是否真的属于本次评估。

    现在接受三种引用：

    - `tool_call_id`：必须是本次评估真实执行过的工具调用；
    - `fact_id` 或完整字段身份：必须唯一对应本次评估提供的事实；
    - `rule_result_id`（也可同时给出 rule_key/rule_code）：必须是本次上下文提供的真实上游结果。

    引用不到的依据一律拒绝：让模型「说一个编号」而不核对，等于证据要求形同虚设。
    """
    executed = {
        str(item.get("tool_call_id")): item
        for item in tool_results
        if item.get("tool_call_id")
    }
    result_index = {
        str(item.get("rule_result_id")): item
        for item in upstream_results
        if isinstance(item, dict) and item.get("rule_result_id") is not None
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
            call = executed[tool_call_id]
            source_items = (call.get("result") or {}).get("items") or []
            if call.get("tool_name") == "TASK_DATA_READ":
                matched = [source for source in source_items
                           if (item.get("artifact_id") is None or source.get("artifact_id") == item["artifact_id"])
                           and (item.get("material_key") is None or source.get("material_key") == item["material_key"])]
                if not matched:
                    raise _schema_error("证据引用的产物不在本次工具读取结果中", tool_call_id=tool_call_id)
                quote = item.get("quote_text")
                if quote is not None and (not isinstance(quote, str) or not quote.strip()):
                    raise _schema_error("证据原文必须是非空字符串", tool_call_id=tool_call_id)
                if quote is not None:
                    from .evidence import _check_evidence_content, artifact_content_texts
                    matched = [source for source in matched if _check_evidence_content(
                        artifact_content_texts(source["content"]), quote) == "FOUND"]
                    if not matched:
                        raise _schema_error("证据引文在所引用的工具读取内容中找不到", tool_call_id=tool_call_id)
                for source in matched:
                    resolved.append({"tool_call_id": tool_call_id, "artifact_id": source["artifact_id"],
                                     "material_key": source["material_key"], "quote_text": quote,
                                     "output_code": item.get("output_code")})
            else:
                resolved.append({"tool_call_id": tool_call_id})
            continue
        fact_code = item.get("fact_code")
        material_key = item.get("material_key")
        if item.get("field_key") or item.get("fact_id") is not None or (isinstance(fact_code, str) and fact_code):
            fact_id = item.get("fact_id")
            if fact_id is not None and (isinstance(fact_id, bool) or not isinstance(fact_id, int)):
                raise _schema_error("证据引用里的 fact_id 必须是上下文提供的整数", index=index)
            if material_key is not None and not isinstance(material_key, str):
                raise _schema_error("证据引用里的 material_key 必须是字符串或为空", index=index)
            references = {key: item[key] for key in ("fact_id", "field_key", "fact_code", "material_key", "period_scope", "statement_scope", "entity_scope") if key in item}
            matched = [fact for fact in facts if all(fact.get(key) == value for key, value in references.items())]
            if len(matched) != 1:
                raise _schema_error(
                    "事实证据引用必须唯一对应本次上下文的完整字段身份，请使用 fact_id 或 field_key 与完整口径",
                    reference=references, matched_count=len(matched),
                )
            fact = matched[0]
            source = {key: fact[key] for key in ("source_field_value_id", "source_rule_result_id") if key in fact}
            if len(source) != 1:
                raise _schema_error("事实没有唯一的已保存取值来源", fact_id=fact.get("fact_id"))
            resolved.append({**{key: fact.get(key) for key in ("fact_id", "field_key", "fact_code", "material_key", "period_scope", "statement_scope", "entity_scope")},
                             **source})
            continue
        result_id = item.get("rule_result_id")
        if result_id is not None:
            if isinstance(result_id, bool) or not isinstance(result_id, int) or str(result_id) not in result_index:
                raise _schema_error("候选结论引用了本次上下文之外的规则结果", rule_result_id=result_id)
            upstream = result_index[str(result_id)]
            for identity in ("rule_key", "rule_code"):
                if identity in item and item[identity] != upstream.get(identity):
                    raise _schema_error("上游规则证据的身份与结果编号不一致", rule_result_id=result_id, identity=identity)
            resolved.append({
                "rule_result_id": upstream.get("rule_result_id"),
                "rule_key": upstream.get("rule_key"),
                "rule_code": upstream.get("rule_code"),
            })
            continue
        raise _schema_error(
            "候选结论的证据引用必须给出 tool_call_id、fact_id、field_key、完整事实身份或本次上下文的整数 rule_result_id",
            index=index,
        )
    return resolved


def validate_verdict(
    raw: dict[str, Any],
    *,
    rule: dict[str, Any],
    tool_results: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    upstream_results: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """仅检查结果形状与真实引用；业务判断照模型结果保存。"""
    _assert_verdict_shape(raw)
    refs = _evidence_refs(raw, tool_results=tool_results, facts=facts, upstream_results=upstream_results)
    definitions = {field["field_key"]: field for field in rule.get("output_definitions") or []}
    for reference in refs:
        code = reference.get("output_code")
        # 模型对非字段证据常填空字符串；它不是声明产出，按未填写处理。
        if isinstance(code, str) and not code.strip():
            code = None
            reference.pop("output_code", None)
        if code is not None:
            if not isinstance(code, str) or code not in definitions:
                raise _schema_error("字段证据引用的 output_code 不属于本规则的输出", output_code=code)
            if reference.get("material_key") != definitions[code]["material_key"]:
                raise _schema_error("字段证据的来源材料与声明不一致", output_code=code)
    verdict = {
        "presentation_version": 2,
        "status": raw["status"],
        "message": raw["message"].strip(),
        "basis": raw["basis"],
        "missing_items": raw["missing_items"],
        "next_action": raw["next_action"].strip(),
        "evidence": refs,
        "outputs": raw.get("outputs", []),
        "report": raw.get("report"),
        "rule_key": rule.get("rule_key"),
        "rule_name": rule.get("name"),
    }
    referenced_calls = {item.get("tool_call_id") for item in refs if item.get("tool_call_id")}
    verdict["tool_findings"] = [
        {"tool_call_id": item["tool_call_id"], "tool_name": item["tool_name"],
         **{key: item["result"][key] for key in ("comparison", "per_material", "input_snapshot") if key in item["result"]}}
        for item in tool_results if item.get("tool_call_id") in referenced_calls
        and any(key in item.get("result", {}) for key in ("comparison", "per_material"))
    ]
    # DONE 表示本条模型执行形成结果，不代表业务通过；材料不足也必须保存真实原因。
    return verdict, LOOP_DONE, [{"kind": "BUSINESS_DATA_MISSING", "message": item} for item in raw["missing_items"]]


# 规则输入


def _task_facts(db: Session, task: AuditTask, materials: list[dict[str, Any]], *,
                rule_inputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """从统一事实读取可用取值，不再逐条与字段展示副本对账。"""
    from ...entity.tasks import TaskFact
    definitions = {f["field_key"]: f for f in task.task_schema_json["fields"]}
    if rule_inputs is not None:
        selected = {r["field_key"] for r in rule_inputs}
        definitions = {k: f for k, f in definitions.items() if k in selected}
    from .fact_context import extracted_identity_filter, project_task_facts
    rows = list(db.scalars(select(TaskFact).where(
        TaskFact.audit_task_id == task.id, TaskFact.is_current.is_(True),
        extracted_identity_filter(definitions),
    ).order_by(TaskFact.id)))
    return project_task_facts(
        db, task, rows, definitions=definitions,
        material_states={m["material_key"]: m.get("material_state") for m in materials},
    )


def _task_materials(db: Session, task: AuditTask) -> list[dict[str, Any]]:
    """当前任务材料与上传状态，包括尚未上传的材料。"""
    from sqlalchemy import select

    from ...entity.tasks import DocumentUpload, TaskDocument
    from .state import _material_states

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
                "upload_sha256": upload.sha256 if upload else None,
                "upload_revision": upload.upload_no if upload else None,
                "upload_filename": upload.original_filename if upload else None,
                "upload_content_type": upload.content_type if upload else None,
                "upload_size_bytes": upload.size_bytes if upload else None,
            }
        )
    return materials




















def rule_data(db: Session, task: AuditTask, rule: dict[str, Any]) -> dict[str, Any]:
    """直接读取当前规则的实际输入；内容由图状态保存，不另建规则输入。"""
    materials = _task_materials(db, task)
    mode = str(rule.get("execution_mode") or MODE_UNKNOWN)
    # 汇总/风险规则只消费来源结果；材料检查只读材料本身，都不加载字段事实。
    source_only = mode in SOURCE_ONLY_MODES or mode == MODE_MATERIAL_CHECK
    references = [] if source_only else fact_projection.declared_inputs_of(rule)
    facts = _task_facts(db, task, materials,
                        rule_inputs=[] if source_only else (None if rule.get("kind") == "REPORT" else references))
    from ...entity.tasks import TaskFact
    definition = rule.get("calculation_definition") or {}
    result_codes = set(rule.get("source_rule_codes") or []) | set(definition.get("depends_on_result_codes") or [])
    result_codes.update(p["source_code"] for p in definition.get("parameters") or []
                        if p.get("source_type") == "CALC_RESULT" and p.get("source_code"))
    query = select(TaskFact).where(TaskFact.audit_task_id == task.id,
                                   TaskFact.is_current.is_(True), TaskFact.fact_kind == FACT_KIND_DERIVED)
    if rule.get("kind") != "REPORT":
        query = query.where(TaskFact.fact_code.in_(result_codes))
    derived = list(db.scalars(query.order_by(TaskFact.id)))
    from .fact_context import project_task_facts
    derived_facts = project_task_facts(db, task, derived)
    facts.extend(derived_facts)
    return {"materials": materials, "facts": facts,
            "inputs": fact_projection.resolve_rule_inputs(facts, references)}


# 工具范围与执行


def tool_definitions(allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """根据当前循环的工具列表生成实际调用定义。"""
    return [tool_service.resolve(item["tool_id"]).to_definition() for item in allowed_tools or []]


def available_tools() -> list[dict[str, Any]]:
    """当前有真实实现、可由模型选择的工具。"""
    return [{"tool_id": spec.tool_id} for spec in tool_catalog.TOOL_SPECS.values()
            if spec.agent_selectable and spec.implemented]


def execution_context(db: Session, *, task: AuditTask, scene_code: str,
                      node_code: str, rule_instance_id: str,
                      material_refs: tuple[str, ...]) -> ToolExecutionContext:
    """工具只需要任务身份、规则与材料范围，以及运行期会话。"""
    return ToolExecutionContext(task_id=task.id, scene_code=scene_code, node_code=node_code,
                                rule_instance_id=rule_instance_id, material_refs=material_refs,
                                database_session=db)


# 工具实现或运行环境错误不能当成模型参数错误交回模型继续猜结论。
_TOOL_PROGRAM_ERRORS = frozenset({
    "TOOL_CATALOG_OUTPUT_CONTRACT_INVALID", "TOOL_CONTEXT_MISSING", "TOOL_HANDLER_MISSING",
    "TOOL_NOT_REGISTERED", "TOOL_RESULT_INVALID", "TOOL_RUNTIME_MISSING",
    "TOOL_RUNTIME_UNEXPECTED", "TOOL_SCHEMA_INVALID", "TOOL_TASK_MISSING", "TOOL_VERSION_MISMATCH",
})


def invoke_tool(db: Session, *, call: dict[str, Any], inputs: list[dict[str, Any]],
                context: ToolExecutionContext, runtime: dict[str, Any] | None,
                result_context: dict[str, Any], rule: dict[str, Any],
                manifest: dict[str, Any]) -> dict[str, Any]:
    """真实执行工具；业务错误作为明确失败结果交回模型，程序异常直接暴露。"""
    try:
        query_tool = call["tool_name"] == "RULE_LIBRARY_QUERY"
        result = tool_service.invoke(call["tool_name"], call.get("arguments"), inputs=inputs,
                                     runtime=None if query_tool else runtime, context=context)
        if not query_tool:
            result = {**result, **result_context}
        db.commit()
        return result
    except BusinessError as exc:
        if exc.code in _TOOL_PROGRAM_ERRORS:
            logger.error("工具实现或运行环境错误 tool=%s code=%s message=%s",
                         call["tool_name"], exc.code, exc.message)
            raise
        logger.warning("工具调用失败 tool=%s code=%s message=%s details=%s",
                       call["tool_name"], exc.code, exc.message, exc.details)
        return {"status": "FAILED", "code": exc.code, "message": exc.message, "details": exc.details}


def tool_result_entry(call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """图状态中的真实工具请求与结果。"""
    return {
        "tool_call_id": call["tool_call_id"],
        "provider_tool_call_id": call.get("provider_tool_call_id"),
        "tool_name": call["tool_name"],
        "arguments": call.get("arguments"),
        "status": result.get("status"),
        "message": result.get("message"),
        "result": result,
    }
