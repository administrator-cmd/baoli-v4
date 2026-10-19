"""规则执行 Agent：直接抽取、计算和判断，按需调用真实工具；输入和消息由图状态保存。"""

from __future__ import annotations

import json
import logging
from typing import Any

from ...domain import fact_projection, tool_catalog
from ...domain.audit_vocab import LOOP_DONE, MODEL_VERDICT_STATUSES
from ...domain.tool_context import ToolExecutionContext
from ...common.exceptions import BusinessError
from ...integrations.llm.gateway import ModelTurn, call_chat
from ...entity import AuditTask
from . import value_store as task_value_service
from .tools import runtime as tool_service
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 模型能给出的结论状态。与能力目录的规则状态口径一致：模型不是另立一套状态。
# 当前审核执行器不启用人工审核。历史结果仍可展示 PENDING_REVIEW，
# 但模型新结果不能用它表示“开始执行/等待人工”，否则会把进度文字当成结论。
# 候选结论的固定 Schema。模型按它输出，服务端按它校验；两者是同一份清单
# （见 `validate_verdict`），任何一侧单独改动都会被校验当场发现。
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": sorted(MODEL_VERDICT_STATUSES)},
        "message": {"type": "string", "minLength": 1, "description": "面向审核用户的一句业务结论及直接原因，不含内部身份、工具调用或执行过程"},
        "basis": {"type": "array", "items": {
            "type": "object", "properties": {
                "label": {"type": "string"}, "value": {},
                "explanation": {"type": "string"},
            }, "required": ["label", "value", "explanation"], "additionalProperties": False,
        }},
        "missing_items": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
        # 提供的引用必须能在本次评估里找到。
        "evidence": {"type": "array"},
        "outputs": {"type": "array", "items": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "value": {}, "raw_value": {"type": "string"},
                           "field_status": {"enum": ["PRESENT", "NOT_PRESENT", "EVIDENCE_INSUFFICIENT"]}},
            "required": ["code", "value"],
        }},
        "report": {"type": ["object", "null"]},
    },
    "required": ["status", "message", "basis", "missing_items", "next_action", "evidence", "outputs"],
    "additionalProperties": True,
}

# 无工具请求使用 JSON 对象模式；带工具请求由原生 tool_calls 与最终 JSON 两种响应组成。
# 最终 JSON 接口结构由同一份 VERDICT_SCHEMA 告知模型并核对。
_VERDICT_RESPONSE_FORMAT = {"type": "json_object"}

_SYSTEM_PROMPT = """你是场景共用的辅助规则执行器。按本次传入的规则原文和真实输入执行，
不审核规则的业务正确性，不替管理员修改方法、阈值或条件。
能由 LLM 完成的计算、日期处理、比较、汇总和判断直接完成，不要求必须调用工具。
需要额外能力时从本次工具列表选择合适工具，通过 tool_calls 执行；参数按规则原文和工具 Schema 填写。
需要查阅关联规则、计算公式或 Excel 原文时调用 RULE_LIBRARY_QUERY，inputs 填 []。
parameters 可按 rule_codes（规则或计算结果编号）、rule_keys、query（原文关键词）和 kind 查询；
不填写条件则列出本任务全部生效规则。多个条件同时填写时取交集。
查询返回的是规则配置，查询成功不代表业务检查通过；不得据此编造材料事实或来源检查结果。
inputs 只使用提供的 field_key/material_key，真实数值由系统按这些身份读取，不编造数据。
工具不可用或调用失败时，系统保留真实调用错误；若已有其他真实依据足以执行，可按该依据完成。
LLM 和可用工具都不能完成时返回 EVIDENCE_INSUFFICIENT，message 明确说明“无法执行”及原因，
不要假装通过。本阶段暂不提供人工审核，不请求用户确认或暂停执行。
原文明确要求人工确认时，先完成你能够完成的核查；人工审核当前未启用，不能返回 PENDING_REVIEW，
请使用 EVIDENCE_INSUFFICIENT，basis 写明已完成的业务核查，missing_items 写明尚未确认事项，不声称已获得人工确认。
不能用“开始执行”、进度说明或请求批准调用工具代替真实结论。
“必须审核”“生效”“最高优先级”等要求由你直接执行，不代表需要人工确认。
材料、事实或关联结果尚未读取时按执行需要使用真实工具；不能仅因尚未开展审核就请求复核。
组合、汇总类规则按原文读取关联规则配置与实际结果，不能将“准备执行关联规则”当作已完成结论。
数据不足、工具失败或不能完成时使用 EVIDENCE_INSUFFICIENT 并说明具体原因，其余生效规则继续执行。
状态使用 PASSED、ABNORMAL、COMPUTED、NOT_APPLICABLE、EVIDENCE_INSUFFICIENT。
业务原文仅要求计算中间值、未要求审核判断时，成功返回 COMPUTED；业务适用条件不成立时返回 NOT_APPLICABLE。
结果面向审核用户，不是执行日志。message 直接回答本条检查的结果，仅在异常或无法判断时补充直接原因。
能用一个短语表达就不用长句；不要复述规则要求、核查步骤、材料清单、依据全文或字段身份。
表达示例（仅说明文风，必须依据本次真实结果，不能照抄当作结论）：
格式检查通过写“格式正确”；大小检查通过写“文件大小符合要求”；
确认字段未载明写“未找到合同编号”；已经抽取写“已抽取甲方名称”；
比较异常写“签署日期晚于生效日期”；缺值无法判断写“缺少履约开始日期，无法比较”。
确认未载明与无法读取必须区分，无法读取时不能说材料没有该字段。
执行完成与审核通过也必须区分，发现异常同样可能是已完成执行。
message 用材料和字段的中文名称，
不要写内部键、产物编号、调用编号、工具名称、接口状态、output_definitions 或未启用的系统能力。
basis 独立列出真实取值、比较双方、算式和业务依据；每项包含 label、value、explanation，使用中文业务名称。
missing_items 只列最终仍影响本条结论的缺失数据或待确认事项；已经成功恢复的工具失败不是业务缺项。
next_action 仅填写依据本条规则所需的业务处理建议，没有需要处理的事项时填空字符串。
内部身份只放在 evidence 与 outputs 的对应身份字段中；系统负责记录技术诊断，不要在业务说明里复述。
每项抽取字段输出必须给出 field_status：PRESENT 表示取得真实值；NOT_PRESENT 表示读取材料后确认未载明；
EVIDENCE_INSUFFICIENT 表示无法确定。后两者 value 填 null，PRESENT 的 value 不得为空。
抽取不到字段时 value 填 null，raw_value 不得用失败说明、执行过程或猜测值代替材料原文；
成功抽取/计算用 COMPUTED，无法确定必需输出用 EVIDENCE_INSUFFICIENT；确认字段未载明时保留 NOT_PRESENT，
业务规则判断另按其原文执行，不用空字符串伪装成已抽取。
字段缺失原因放 missing_items，真实原文引文放 evidence.quote_text，计算过程放 basis。
抽取与计算必须返回 outputs，不得只描述已经完成。每项包含 code、value，可选 raw_value；
code 使用声明的输出字段键或 result_code，value 填真实结果，raw_value 填来源原文或实际计算过程。
只允许产出本规则 output_definitions 和 result_code 声明的身份，STRUCTURED字段value为声明Schema对应的JSON对象。
报告返回 report 对象，content 填完整正文，content_type 填实际正文 MIME 类型，支持text/markdown、text/plain、text/html，正文按 report_definition 的业务内容要求输出。正文成功生成用 COMPUTED；PDF、Word 等文件格式由系统单独记录支持情况，不在 message 和正文混入导出诊断，也不因缺少导出文件而放弃正文。
报告正文使用业务名称、取值、核查结论、风险、缺项和建议，不复制工具日志、内部身份或上游整段执行说明。
报告章节 data_refs 可以引用字段事实、计算结果和规则结论；report_data 按章节提供对应的真实 facts/results。
按每章 content_instruction、业务引用和材料范围组织正文，不把计算结果或规则结论当成待抽取字段。
missing_data=true 时在对应章节明确说明缺少哪项数据，其余真实内容继续生成；引用为空时按章节要求选择相关的 upstream_facts/upstream_results。
结果存在但没有所需数值时如实说明；规则配置、公式或停用规则不代表已经执行，不编造数值、风险或通过结论。
上游 PENDING_REVIEW 表示业务尚未确认，报告应列明未确认事项，不得写成已确认或通过。
需要实际材料内容时调用 TASK_DATA_READ，parameters.material_keys 填提供的材料身份；inputs 填 []。
工具调用必须使用接口原生 tool_calls，不要在正文 JSON 中写 function、arguments、tool_calls 或 action 来模拟调用。
读取材料时，TASK_DATA_READ 的 inputs 填空数组，parameters.material_keys 使用本条允许读取的真实材料身份。
程序已在业务审核前完成 8018 校验、8010 去章、PaddleOCR 正文识别和 8003 原文件印章识别。
正文抽取使用 artifact_type=OCR，读取去章文件生成的 Markdown；印章检验使用 artifact_type=SEAL。
印章主体来自 SEAL 的 seal_subjects/subject_name，正文主体来自 OCR Markdown；按规则要求比较，不能从去章正文猜印章主体。
字段 output_definitions 的 artifact_source=SEAL 时从印章产物抽取，其余字段从 OCR Markdown 抽取。
upstream_facts 或 inputs 中 origin=HUMAN_CORRECTION 的字段是人工结论，必须沿用：REVIEWED 保留人工值，输出时 field_status=PRESENT；NOT_PRESENT 保留“未载明”，输出 field_status=NOT_PRESENT、value=null。不得重新抽取覆盖人工结论；后续计算和校验使用该结论。
8018 的 matches_declared_material 是实际校验结果，需核对材料类型时读取 artifact_type=MATERIAL_CLASSIFY。
工具调用后等待真实工具结果，再继续执行；“准备读取”“开始核查”不是结论，不能用 PENDING_REVIEW 提前结束。
PDF和图片原文件不是正文，8010 二进制产物也不是正文；不要重新触发文档服务，不要绕过去章和 OCR 链路。
字段定义中的“失败处理：人工复核”不是要求本次跳过抽取，先读取材料并完成可以完成的工作。
执行抽取和计算不需要调用另一个LLM工具，直接依据读取的材料和 upstream_facts 完成。
最终只输出符合上述 Schema 的 JSON 对象；不要复制示例占位符，也不要在 JSON 外输出说明文字。
evidence 中每项为引用对象，可填写实际工具结果的 tool_call_id、实际字段事实的 fact_id 或 field_key 与 material_key；使用 fact_code 时需补齐期间、报表、主体口径以唯一定位，
或本次上下文 upstream_results 中真实的整数 rule_result_id（可同时填写匹配的 rule_key、rule_code）；
不要把规则编号伪装成 fact_code，也不要引用上下文之外的上游结果。
引用 TASK_DATA_READ 的 OCR/SEAL 时，同时给出实际结果中的 artifact_id、material_key 和 quote_text 原文。
抽取字段的引用再给出 output_code，使用本规则声明的字段键；一个字段有多处依据时分别引用。
quote_text 必须是读取结果中的原文，不能用抽取值、判断说明或整篇正文代替具体引文。
只有工具调用编号而没有引文时仅记录产物来源，不代表已核对字段原文。
如果本条规则声明了抽取输出且 inputs 中没有实际字段事实，先使用 TASK_DATA_READ 获取声明材料的真实正文或印章产物；
不能以“开始读取”“准备抽取”或空 outputs 代替实际读取。读取结果必须进入后续判断并在需要时作为证据引用。
"""


def build_rule_context(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    material_names: dict[str, str],
) -> str:
    """规则的评估上下文：规则要求 + 可用的字段事实。

    只给本次规则实际使用的事实与标识。给全量事实会让模型在无关字段上做判断，
    也会让它有机会引用不属于这条规则的输入。
    """
    tool_id = "LLM 与可用工具"
    published_parameters = dict(rule.get("parameters") or {})
    configuration = {key: value for key, value in rule.items()
                     if key not in {"upstream_facts", "upstream_results", "available_materials", "report_data"}}
    lines = [
        f"规则编号：{rule.get('rule_code') or rule.get('rule_key')}",
        f"规则名称：{rule.get('name')}",
        f"审核要求：{rule.get('requirement') or '（未填写）'}",
        f"执行能力：{tool_id}",
        "规则原始配置：" + json.dumps(configuration, ensure_ascii=False, sort_keys=True),
        "业务执行参数：" + json.dumps(published_parameters, ensure_ascii=False, sort_keys=True),
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
    upstream_facts = rule.get("upstream_facts") or []
    lines.extend([
        "",
        "本条规则可直接使用的上游字段事实（仅限以下身份）：",
    ])
    lines.extend(json.dumps(item, ensure_ascii=False, default=str) for item in upstream_facts)
    upstream_results = rule.get("upstream_results") or []
    lines.extend([
        "",
        "本条规则可直接引用的上游规则结果（evidence 使用其中真实 rule_result_id）：",
    ])
    lines.extend(json.dumps(item, ensure_ascii=False, default=str) for item in upstream_results)
    if rule.get("report_data") is not None:
        lines.extend(["", "按报告章节关联的业务数据：" + json.dumps(rule["report_data"], ensure_ascii=False, default=str)])
    if rule.get("available_materials"):
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
        {"role": "system", "content": _SYSTEM_PROMPT + "\n最终结论的数据结构：" + json.dumps(VERDICT_SCHEMA, ensure_ascii=False)},
        {"role": "user", "content": build_rule_context(rule, inputs, manifest=manifest, material_names=material_names)},
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
        response_format=None if tools else _VERDICT_RESPONSE_FORMAT,
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


def pending_calls_of(turn: ModelTurn, evaluation_id: str) -> list[dict[str, Any]]:
    """本轮模型请求的工具调用，写进 State 的 `pending_tool_calls`。

    账本标识带上评估批次前缀：供应商给的调用标识只在一次响应内唯一，
    不同评估（重跑节点、下一条规则）之间可能重复，而账本要求 `tool_call_id`
    全局唯一且「同一次调用不重复执行」。原始标识另存为
    `provider_tool_call_id`，回填 `tool` 消息时必须用它——模型认的是自己给的那个。
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

    要求整体是一个 JSON 对象（见 `_VERDICT_RESPONSE_FORMAT`）。
    解析不出对象时返回 None，由调用方按「模型没有给出可解析的结论」处理（BLOCKED），
    不在文本里扫描「最后一行像 JSON 的东西」——那会把说明文字里的片段当成结论。
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
            resolved.append({**{key: fact.get(key) for key in ("fact_id", "field_key", "fact_code", "material_key", "period_scope", "statement_scope", "entity_scope")},
                             "evidence_ids": list(fact.get("evidence_ids") or [])})
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


def _fact_values(db: Session, task: AuditTask, rows: list[Any]) -> dict[int, dict[str, Any]]:
    """这批事实索引行在任务 CSV 里的取值；表里不再存值。"""
    return {row.id: task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, row.id)
            for row in rows}


def _task_facts(db: Session, task: AuditTask, materials: list[dict[str, Any]], *,
                rule_inputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """从统一事实读取可用取值，不再逐条与字段展示副本对账。"""
    from ...entity import TaskFact
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
    from .state import _material_states
    from ...entity import DocumentUpload, TaskDocument
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
    references = fact_projection.declared_inputs_of(rule)
    facts = _task_facts(db, task, materials, rule_inputs=None if rule.get("kind") == "REPORT" else references)
    from ...entity import TaskFact
    definition = rule.get("calculation_definition") or {}
    result_codes = set(rule.get("source_rule_codes") or []) | set(definition.get("depends_on_result_codes") or [])
    result_codes.update(p["source_code"] for p in definition.get("parameters") or []
                        if p.get("source_type") == "CALC_RESULT" and p.get("source_code"))
    query = select(TaskFact).where(TaskFact.audit_task_id == task.id,
                                   TaskFact.is_current.is_(True), TaskFact.fact_kind == "DERIVED")
    if rule.get("kind") != "REPORT":
        query = query.where(TaskFact.fact_code.in_(result_codes))
    derived = list(db.scalars(query.order_by(TaskFact.id)))
    derived_facts = fact_projection.facts_of_task_fact_rows(derived, values=_fact_values(db, task, derived))
    from .evidence import fact_evidence_ids
    for row, fact in zip(derived, derived_facts):
        fact["evidence_ids"] = fact_evidence_ids(db, task, row)
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
