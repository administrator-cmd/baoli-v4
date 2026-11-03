"""规则执行 Agent：直接抽取、计算和判断，按需调用真实工具；输入和消息由图状态保存。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import FACT_KIND_DERIVED
from app.constants.status import (
    EXECUTABLE_FIELD_STATUSES,
    FIELD_STATUS_PRESENT,
    LOOP_DONE,
    MODEL_VERDICT_STATUSES,
)
from app.domain import fact_projection, tool_catalog
from app.domain.execution_mode import (
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
from app.domain.llm_failure import channel_failure_detail, is_channel_failure
from app.domain.tool_context import ToolExecutionContext
from app.integrations.llm.gateway import ModelTurn, RequestLogContext, call_chat
from app.modules.audit.execution.service.state import _material_states
from app.modules.audit.field.service.prompts import (
    BATCH_PROMPT_SUFFIX,
    EXTRACTION_BATCH_SCHEMA,
    SYSTEM_PROMPT,
    VERDICT_RESPONSE_FORMAT,
    VERDICT_SCHEMA,
)
from app.modules.audit.field.service.tools import runtime as tool_service
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.rule.service.input_validation import (
    missing_rule_inputs,
    same_fact_value,
    validate_input_resolutions,
)
from app.modules.audit.shared import _current_uploads, _locatable_text, _material_entries
from app.modules.audit.task.model import AuditTask

logger = logging.getLogger(__name__)

EXECUTION_MODE_GUIDANCE: dict[str, str] = {
    MODE_MATERIAL_CHECK: "只检查本条声明的材料/文件本身；材料清单与真实上传状态已在下方提供，直接判断声明的材料位是否齐全，不调用材料存在性工具。格式、大小、哈希重复等需要程序读取文件时，再调用对应的文件质量工具。",
    MODE_VALUE_COMPARE: "直接使用下方列出的输入字段事实进行比较并给出结论；一致、日期先后、数值大小、是否为空等都由你直接判断，不调用比较工具。",
    MODE_CROSS_SCOPE_COMPARE: "直接比较下方列出的输入事实，按期间、报表口径和主体范围区分比较对象；不调用比较工具。",
    MODE_AGGREGATE: "只汇总下方来源规则结果；不得重新执行子规则、不得读取材料或字段、不得新增子规则未给出的结论。",
    MODE_DERIVE_RISK: "只根据下方来源规则结果与风险成立条件推导风险；不得重新检查材料或字段。",
    MODE_EXTRACT: "只抽取本条声明的字段；读取材料后直接抽取，不调用比较工具。",
    MODE_CALCULATE: "按本条公式与参数直接计算，数值比较和加总也直接判断；不调用比较或加总工具。",
    MODE_REPORT: "按报告章节要求生成正文；不重新执行上游规则。",
    MODE_UNKNOWN: "执行方式未确定；不得猜测执行动作，按证据不足说明原因。",
}


# 上游规则结果里下游真正需要的身份与业务结论。
# evidence、tool_findings、report、risk_context 等属于该条结果的诊断与展示，不在规则之间重复传递。
_UPSTREAM_RESULT_KEYS = ("rule_result_id", "rule_key", "rule_code", "rule_name", "status", "message", "node_code")


# 工具实现或运行环境错误不能当成模型参数错误交回模型继续猜结论。
_TOOL_PROGRAM_ERRORS = frozenset({
    "TOOL_CATALOG_OUTPUT_CONTRACT_INVALID", "TOOL_CONTEXT_MISSING", "TOOL_HANDLER_MISSING",
    "TOOL_NOT_REGISTERED", "TOOL_RESULT_INVALID", "TOOL_RUNTIME_MISSING",
    "TOOL_RUNTIME_UNEXPECTED", "TOOL_SCHEMA_INVALID", "TOOL_TASK_MISSING", "TOOL_VERSION_MISMATCH",
})


class AuditRuleAgentService:
    """组装规则上下文、调用模型并校验候选结论。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _without_origin(self, value: Any) -> Any:
        """去掉模型不需要的来源快照（Excel 工作表名、行号和原始单元格）。

        来源信息只用于页面溯源与导入回查，模型执行只依赖规则要求、输入身份与真实取值。
        """
        if isinstance(value, dict):
            return {key: self._without_origin(item) for key, item in value.items() if key != "origin"}
        if isinstance(value, list):
            return [self._without_origin(item) for item in value]
        return value


    def _upstream_result_view(self, item: dict[str, Any]) -> dict[str, Any]:
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
            # 保留规则明确配置的风险等级和处置要求，不传播模型生成的后续操作建议。
            "risk_level": item.get("risk_level") or payload.get("risk_level"),
            "action": payload.get("action"),
        }


    def _artifact_content_text(self, item: dict[str, Any]) -> str:
        """预读产物正文：OCR 按页输出，其他产物按原始结构输出。"""
        content = item.get("content")
        if item.get("artifact_type") == "OCR" and isinstance(content, dict):
            pages = content.get("pages")
            if isinstance(pages, list) and pages:
                rendered = []
                for page in pages:
                    if not isinstance(page, dict):
                        continue
                    text = page.get("text")
                    if not isinstance(text, str):
                        continue
                    number = page.get("page")
                    rendered.append(f"--- 第 {number} 页 ---\n{text}" if number is not None else text)
                return "\n\n".join(rendered)
        return json.dumps(content, ensure_ascii=False, default=str)


    def _preloaded_artifact_lines(self, artifacts: list[dict[str, Any]] | None,
                                  material_names: dict[str, str]) -> list[str]:
        """预读产物的身份与正文；不可用产物保留真实原因，不伪造正文。"""
        if not artifacts:
            return []
        lines = [
            "",
            "本次上下文已按规则声明读取以下真实材料产物，直接依据这些内容抽取；"
            "不要再调用 TASK_DATA_READ，也不要为字段重复读取同一产物：",
        ]
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            material_key = str(item.get("material_key") or "")
            material_name = material_names.get(material_key, material_key)
            artifact_type = str(item.get("artifact_type") or "")
            unavailable = item.get("unavailable")
            if isinstance(unavailable, dict):
                reason = " ".join(
                    str(part) for part in (unavailable.get("code"), unavailable.get("message")) if part
                )
                lines.append(
                    f"【材料要求：{material_name}（material_key={material_key}），产物：{artifact_type}】不可用："
                    f"{reason}。无法据此抽取时按 EVIDENCE_INSUFFICIENT 说明原因，不要编造正文。"
                )
                continue
            lines.append(
                f"【来源文件：{item.get('filename') or '未记录来源文件'}，"
                f"材料要求：{material_name}（material_key={material_key}），产物：{artifact_type}，"
                f"artifact_id={item.get('artifact_id')}】"
            )
            lines.append(self._artifact_content_text(item))
        return lines


    def _calculation_input_lines(self, rule: dict[str, Any],
                                 inputs: list[dict[str, Any]]) -> list[str]:
        """计算规则的逐参数输入状态；缺项不得按 0 或相近科目替代。"""
        calculation = rule.get("calculation_definition") or {}
        parameters = [item for item in calculation.get("parameters") or [] if isinstance(item, dict)]
        gaps = [item for item in [*(calculation.get("input_gaps") or []), *(rule.get("input_gaps") or [])]
                if isinstance(item, dict)]
        if not parameters and not gaps:
            return []
        field_inputs = {
            (str(item.get("field_key") or ""), item.get("material_key")): item
            for item in inputs or [] if isinstance(item, dict)
        }
        derived = {
            str(item.get("fact_code") or ""): item
            for item in rule.get("upstream_facts") or []
            if isinstance(item, dict) and item.get("fact_kind") == FACT_KIND_DERIVED
        }
        lines = [
            "",
            "计算输入完整性（逐个参数核对；缺失输入不得按 0 代入，也不得用其他科目替代）：",
        ]
        for parameter in parameters:
            code = parameter.get("parameter_code") or "未命名参数"
            source_type = str(parameter.get("source_type") or "")
            source_code = str(parameter.get("source_code") or "")
            if source_type == "FIELD":
                field_key = str(parameter.get("resolved_field_key") or source_code)
                item = field_inputs.get((field_key, parameter.get("resolved_material_key"))) or {}
                lines.append(
                    f"- {code}：字段={item.get('field_name') or field_key}，"
                    f"状态={item.get('status') or 'NOT_EXECUTED'}，取值={item.get('normalized_value')!r}，"
                    f"期间={item.get('actual_period_scope') or (item.get('period_scope') if item.get('period_scope') != 'NOT_APPLICABLE' else '未声明/不适用')}，"
                    f"单位={item.get('actual_unit') or item.get('unit') or '未声明/不适用'}"
                )
            elif source_type == "CALC_RESULT":
                fact = derived.get(source_code) or {}
                lines.append(
                    f"- {code}：上游计算结果={source_code}，状态={fact.get('status') or 'NOT_EXECUTED'}，"
                    f"取值={fact.get('normalized_value')!r}"
                )
            else:
                lines.append(f"- {code}：常量={parameter.get('constant_value')!r}")
        lines.extend([
            "",
            "计算要求：",
            "- 只有所有声明输入都取得真实值时，才能按完整公式给出正式结果。",
            "- 未知、未提供、未抽取到的输入不得当作 0；不得用相近科目、其他期间或其他口径的值替代。",
            "- 缺失输入按下方 input_id 逐项填写 input_resolutions；每项提供取值与独立的真实来源，没有真实来源时返回 EVIDENCE_INSUFFICIENT。",
            "- 不得自行给出估算值、下限或按缺项公式计算的值；原要求没有授权估算时不降级。",
        ])
        return lines


    def build_rule_context(self, rule: dict[str, Any],
        inputs: list[dict[str, Any]],
        *,
        manifest: dict[str, Any],
        material_names: dict[str, str],
        preloaded_artifacts: list[dict[str, Any]] | None = None,
    ) -> str:
        """规则的评估上下文：执行方式 + 规则要求 + 该动作真正需要的事实。"""
        tool_id = "LLM 与可用工具"
        mode = str(rule.get("execution_mode") or MODE_UNKNOWN)
        source_only = mode in SOURCE_ONLY_MODES
        material_only = mode == MODE_MATERIAL_CHECK
        published_parameters = dict(rule.get("parameters") or {})
        configuration = self._without_origin({key: value for key, value in rule.items()
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
            "阈值、单位与小数按发布原值直接比较或计算，不要自行换算精度或单位。",
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
            declared_field_names = {
                str(field.get("field_key")): str(field.get("name"))
                for field in manifest.get("fields") or []
                if isinstance(field, dict) and field.get("field_key") and field.get("name")
            }
            for item in inputs:
                field_key = item.get("field_key")
                material_key = item.get("material_key")
                # 事实已带中文名时直接用；未形成事实时取冻结配置的声明名称，不按编码猜。
                field_name = item.get("field_name") or declared_field_names.get(str(field_key))
                name_part = f"字段名称={field_name}，" if field_name else "字段名称=未声明，"
                fact_id = item.get("fact_id")
                fact_part = f"fact_id={fact_id}，" if fact_id is not None else ""
                evidence = item.get("evidence") or []
                evidence_part = json.dumps(evidence, ensure_ascii=False, default=str) if evidence else "无"
                lines.append(
                    f"- {name_part}field_key={field_key}，material_key={material_key}"
                    f"（材料要求：{material_names.get(material_key, material_key)}）："
                    f"{fact_part}"
                    f"来源文件={json.dumps(item.get('source_files') or [], ensure_ascii=False)}，"
                    f"原文={item.get('raw_value')!r}，标准化值={item.get('normalized_value')!r}，"
                    f"状态={item.get('status')}，类型={item.get('value_type')}，"
                    f"单位={item.get('unit') or '未声明'}，币种={item.get('currency') or '未声明'}，"
                    f"期间={item.get('period_scope') or '未声明'}，"
                    f"报表口径={item.get('statement_scope') or '未声明'}，"
                    f"主体范围={item.get('entity_scope') or '未声明'}，"
                    f"实际识别期间={item.get('actual_period_scope') or '未识别'}，"
                    f"实际报表口径={item.get('actual_statement_scope') or '未识别'}，"
                    f"实际主体范围={item.get('actual_entity_scope') or '未识别'}，"
                    f"实际单位={item.get('actual_unit') or '未识别'}，"
                    f"证据={evidence_part}"
                )
            if any(item.get("status") == "NOT_EXECUTED" for item in inputs):
                lines.append(
                    "状态为 NOT_EXECUTED 的输入表示上游没有形成取值，不能据此认定材料未载明，"
                    "也不能把它当作有效取值引用；需要时在允许的材料范围内补取，未补齐则返回 EVIDENCE_INSUFFICIENT。"
                )
        elif mode in {MODE_VALUE_COMPARE, MODE_CROSS_SCOPE_COMPARE}:
            lines.append("本条规则没有声明字段输入；只使用本条允许的材料/产物，按规则要求判断，不读取未声明材料。")
        if mode == MODE_CALCULATE:
            lines.extend(self._calculation_input_lines(rule, inputs))
        missing_inputs = missing_rule_inputs(rule, inputs, rule.get("upstream_facts") or [])
        if missing_inputs:
            lines.extend([
                "", "本次缺失输入（input_id 仅在本条评估内有效）：",
                json.dumps(missing_inputs, ensure_ascii=False),
                "逐项补取后填写 input_resolutions：input_id、value、raw_value、evidence。"
                "evidence 是单条真实引用，不填 output_code；材料来源的 raw_value 必须出现在 quote_text 中。"
                "每项都要独立说明科目、期间和单位；basis 解释补取值与计算/判断的关系。"
                "未补齐全部必要输入时返回 EVIDENCE_INSUFFICIENT，不得用一条无关引文解除其他缺项。",
            ])
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
                lines.extend(json.dumps(self._upstream_result_view(item), ensure_ascii=False, default=str)
                             for item in upstream_results)
            else:
                lines.append("（没有可用的来源规则结果；不得自行补做来源检查，按证据不足说明缺少哪些来源结果。）")
        if rule.get("report_data") is not None:
            lines.extend(["", "按报告章节关联的业务数据：" + json.dumps(rule["report_data"], ensure_ascii=False, default=str)])
        if rule.get("available_materials") and not source_only:
            lines.extend([
                "",
                "本条规则允许的材料及上传状态（正文读取 OCR，印章读取 SEAL；未上传也如实保留）：",
                *[
                    json.dumps({
                        "material_key": item.get("material_key"),
                        "name": item.get("material_name"),
                        "required": item.get("required"),
                        "document_status": item.get("document_status"),
                        "material_state": item.get("material_state"),
                        "uploaded": item.get("upload_id") is not None,
                        "filename": item.get("upload_filename"),
                        "content_type": item.get("upload_content_type"),
                        "size_bytes": item.get("upload_size_bytes"),
                    }, ensure_ascii=False)
                    for item in rule["available_materials"]
                    if isinstance(item, dict)
                ],
            ])
        lines.extend(self._preloaded_artifact_lines(preloaded_artifacts, material_names))
        return "\n".join(lines)


    def initial_messages(self, rule: dict[str, Any],
        inputs: list[dict[str, Any]],
        *,
        manifest: dict[str, Any],
        material_names: dict[str, str],
        preloaded_artifacts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """一条规则实例的初始消息历史。只在 `prepare_agent_context` 里生成一次。"""
        return [
            {"role": "system", "content": SYSTEM_PROMPT + "\n最终结论的数据结构：" + json.dumps(VERDICT_SCHEMA, ensure_ascii=False)},
            {"role": "user", "content": self.build_rule_context(
                rule, inputs, manifest=manifest, material_names=material_names,
                preloaded_artifacts=preloaded_artifacts,
            )},
        ]


    def initial_batch_messages(self, rules: list[dict[str, Any]],
        *,
        material_key: str,
        material_name: str,
        field_contexts: dict[str, dict[str, Any]],
        field_codes: dict[str, str],
        preloaded_artifacts: list[dict[str, Any]] | None = None,
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
        ]
        if preloaded_artifacts:
            lines.append("系统已读取下方列出的真实产物，直接据此抽取；不要再次调用 TASK_DATA_READ，也不要为每个字段重复读取材料。")
        else:
            lines.append("先使用 TASK_DATA_READ 读取字段定义指定的产物：artifact_source=SEAL 时读取 SEAL，其余读取 OCR。")
            lines.append("同一次读取覆盖全部字段，不要为每个字段重复读取材料。")
        lines.extend([
            "",
            "待抽取字段：",
        ])
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
        lines.extend(self._preloaded_artifact_lines(
            preloaded_artifacts, {material_key: material_name},
        ))
        lines.extend([
            "",
            "输出要求：",
            "- 只返回 fields 数组，每个字段一项，code 逐字取自上面列出的短编号，不遗漏、不重复、不自造。",
            "- status 是本字段的业务结论：取到真实值为 COMPUTED，材料中确认没有该内容为 ABNORMAL，无法判断为 EVIDENCE_INSUFFICIENT。",
            "- field_status=PRESENT 时 value 填按字段类型标准化后的真实取值、raw_value 只填字段本身的原始写法；NOT_PRESENT 时 value 必须是 null 并在 message 说明未载明；"
            "EVIDENCE_INSUFFICIENT 时 value 必须是 null 并说明缺少什么。PRESENT 的字段必须至少有一条本字段的产物引文，quote_text 非空；人工核对值除外。",
            "- evidence 每一项都属于本字段：本次上下文提供的预读产物给 artifact_id、artifact_type、material_key、quote_text，"
            "不要为预读产物编造 tool_call_id；真实工具读取的引用给 tool_call_id、artifact_id、material_key、quote_text；"
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


    def call_model_once(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]], tool_choice: Any = "auto",
        idempotency_key: str | None = None, log_context: RequestLogContext | None = None,
    ) -> ModelTurn:
        """调用模型一次，返回这一次的响应契约。

        这里是整个 Agent 循环里唯一的模型调用点。工具执行不由本函数完成——
        模型请求了工具就由 Graph 的 `execute_tool_calls` 节点执行，再回到 `call_model`。
        `log_context` 只用于日志展示，不影响请求内容。
        """
        return call_chat(
            messages,
            tools=tools or None,
            tool_choice=tool_choice,
            # 工具轮次允许原生函数调用；JSON 格式只约束没有工具的直接结论请求。
            response_format=None if tools else VERDICT_RESPONSE_FORMAT,
            idempotency_key=idempotency_key,
            log_context=log_context,
        )


    def assistant_message_of(self, turn: ModelTurn) -> dict[str, Any]:
        """把模型这一轮完整组装的响应并入消息历史。

        `tool_calls` 使用网关按流式 index 组装的原始标识和参数字符串，不重新生成标识或序列化参数。
        这里取 `raw_message`，保留模型返回的推理内容等消息字段，只补 role。
        """
        message = dict(turn.raw_message or {})
        message["role"] = "assistant"
        return message


    def pending_calls_of(self, turn: ModelTurn) -> list[dict[str, Any]]:
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


    def tool_message(self, call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
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


    def parse_verdict(self, content: str | None) -> dict[str, Any] | None:
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
            logger.warning("模型结论使用 Markdown 代码块包裹，已剥离围栏后解析")
            text = fenced.group(1).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed


    def _schema_error(self, message: str, **details: Any) -> BusinessError:
        return BusinessError("AGENT_VERDICT_INVALID", message, details=details)


    def _assert_verdict_shape(self, verdict: dict[str, Any]) -> None:
        """核对结果结构；格式错误交回模型修正。"""
        missing = [key for key in VERDICT_SCHEMA["required"] if key not in verdict]
        if missing:
            raise self._schema_error("候选结论缺少必填字段，本次未采信", missing=missing)
        status = verdict.get("status")
        if not isinstance(status, str) or status not in MODEL_VERDICT_STATUSES:
            raise self._schema_error(
                "候选结论的状态不在允许范围内，本次未采信",
                status=status,
                allowed=sorted(MODEL_VERDICT_STATUSES),
            )
        message = verdict.get("message")
        if not isinstance(message, str) or not message.strip():
            raise self._schema_error("候选结论必须给出非空的判断说明，本次未采信")
        if not isinstance(verdict.get("evidence"), list):
            raise self._schema_error("候选结论的 evidence 必须是数组，本次未采信")
        basis = verdict.get("basis")
        if not isinstance(basis, list) or any(
            not isinstance(item, dict) or set(item) != {"label", "value", "explanation"}
            or not isinstance(item["label"], str) or not item["label"].strip()
            or not isinstance(item["explanation"], str) for item in basis
        ):
            raise self._schema_error("basis 必须为包含中文业务 label、value 和 explanation 的对象数组")
        missing_items = verdict.get("missing_items")
        if not isinstance(missing_items, list) or any(not isinstance(item, str) or not item.strip() for item in missing_items):
            raise self._schema_error("missing_items 必须为最终业务缺项的非空字符串数组")
        if not isinstance(verdict.get("next_action"), str):
            raise self._schema_error("next_action 必须为字符串，无处理建议时填空字符串")


    def _allowed_artifact_materials(self, rule: dict[str, Any]) -> set[str]:
        return {str(item) for item in rule.get("materials") or [] if item}

    def _quote_fragment(self, item: dict[str, Any], key: str) -> str | None:
        value = item.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise self._schema_error(f"证据的 {key} 必须是非空字符串", field=key)
        return value

    def _check_quote_fragments(self, item: dict[str, Any], content: Any, **details: Any) -> dict[str, str]:
        """逐段核对真实原文；目标值、科目、表头、期间分别验证，不拼接成伪原文。"""
        from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
        texts = AuditFieldEvidenceService().artifact_content_texts(content)
        checked: dict[str, str] = {}
        for key in ("quote_text", "quote_label", "quote_header", "quote_period"):
            value = self._quote_fragment(item, key)
            if value is None:
                continue
            if AuditFieldEvidenceService()._check_evidence_content(texts, value) != "FOUND":
                raise self._schema_error(f"证据的 {key} 在所引用的材料内容中找不到", **details)
            checked[key] = value
        return checked

    def _artifact_page(self, content: Any, quote: str | None) -> int | None:
        """OCR 产物内定位引文所在页；只作为产物位置展示，不冒充原 PDF 页码。"""
        if not isinstance(quote, str) or not quote.strip() or not isinstance(content, dict):
            return None
        pages = content.get("pages")
        if not isinstance(pages, list):
            return None
        quoted = _locatable_text(quote)
        for page in pages:
            if not isinstance(page, dict):
                continue
            text = page.get("text")
            if isinstance(text, str) and quoted in _locatable_text(text):
                number = page.get("page")
                return number if isinstance(number, int) and not isinstance(number, bool) else None
        return None

    def _evidence_refs(self, verdict: dict[str, Any], *, rule: dict[str, Any],
        tool_results: list[dict[str, Any]], facts: list[dict[str, Any]],
        upstream_results: list[dict[str, Any]], preloaded_artifacts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """候选结论的证据引用，逐条核对是否真的属于本次评估。

        现在接受四种引用：

        - `tool_call_id`：必须是本次评估真实执行过的工具调用；
        - 预读产物 `artifact_id` + `material_key`：必须是本次上下文实际提供的产物；
        - `fact_id` 或完整字段身份：必须唯一对应本次评估提供的事实；
        - `rule_result_id`（也可同时给出 rule_key/rule_code）：必须是本次上下文提供的真实上游结果。

        预读产物没有工具调用，不能编造 tool_call_id；引用不到的依据一律拒绝。
        """
        executed = {
            str(item.get("tool_call_id")): item
            for item in tool_results
            if item.get("tool_call_id")
        }
        preloaded = {
            (str(item.get("material_key")), item.get("artifact_id")): item
            for item in preloaded_artifacts or []
            if isinstance(item, dict) and item.get("material_key") and item.get("artifact_id") is not None
        }
        result_index = {
            str(item.get("rule_result_id")): item
            for item in upstream_results
            if isinstance(item, dict) and item.get("rule_result_id") is not None
        }
        resolved: list[dict[str, Any]] = []
        for index, item in enumerate(verdict.get("evidence") or []):
            if not isinstance(item, dict):
                raise self._schema_error("候选结论的证据引用必须是对象", index=index, value=item)
            if isinstance(item.get("output_code"), str) and not item["output_code"].strip():
                item = {key: value for key, value in item.items() if key != "output_code"}
            tool_call_id = item.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                if tool_call_id not in executed:
                    raise self._schema_error(
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
                        raise self._schema_error("证据引用的产物不在本次工具读取结果中", tool_call_id=tool_call_id)
                    allowed_materials = self._allowed_artifact_materials(rule)
                    outside = [source for source in matched
                               if allowed_materials and source.get("material_key") not in allowed_materials]
                    if outside:
                        raise self._schema_error(
                            "工具读取结果的材料不在本条规则允许范围内",
                            tool_call_id=tool_call_id,
                            material_keys=sorted(str(source.get("material_key")) for source in outside),
                        )
                    checked_items: list[tuple[dict[str, Any], dict[str, str]]] = []
                    fragment_error: BusinessError | None = None
                    for source in matched:
                        try:
                            checked_items.append((source, self._check_quote_fragments(
                                item, source.get("content"), tool_call_id=tool_call_id,
                            )))
                        except BusinessError as exc:
                            fragment_error = exc
                    if not checked_items:
                        raise fragment_error or self._schema_error(
                            "证据引文在所引用的工具读取内容中找不到", tool_call_id=tool_call_id,
                        )
                    for source, checked in checked_items:
                        resolved.append({
                            "tool_call_id": tool_call_id, "artifact_id": source["artifact_id"],
                            "material_key": source["material_key"],
                            **checked,
                            "artifact_page": self._artifact_page(
                                source.get("content"), checked.get("quote_text") or checked.get("quote_label"),
                            ),
                            "output_code": item.get("output_code"),
                        })
                else:
                    resolved.append({"tool_call_id": tool_call_id})
                continue
            fact_code = item.get("fact_code")
            material_key = item.get("material_key")
            has_fact_reference = bool(item.get("field_key")) or item.get("fact_id") is not None or (
                isinstance(fact_code, str) and fact_code
            )
            artifact_id = item.get("artifact_id")
            if artifact_id is not None and not has_fact_reference and item.get("rule_result_id") is None:
                # 预读产物不是工具调用：按后端实际读取的 artifact_id 与 material_key 核对，
                # 引文必须在该产物正文中找得到；不要求也不接受 tool_call_id。
                if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
                    raise self._schema_error("证据引用的 artifact_id 必须是本次提供的产物编号", index=index)
                if not isinstance(material_key, str) or not material_key:
                    raise self._schema_error("引用预读产物时必须给出 material_key", index=index)
                source = preloaded.get((material_key, artifact_id))
                if source is None:
                    raise self._schema_error(
                        "证据引用的产物不是本次上下文提供的材料产物",
                        artifact_id=artifact_id, material_key=material_key,
                    )
                artifact_type = item.get("artifact_type")
                if artifact_type is not None and artifact_type != source.get("artifact_type"):
                    raise self._schema_error("证据引用的产物类型与本次提供的不一致", artifact_id=artifact_id)
                allowed_materials = self._allowed_artifact_materials(rule)
                if allowed_materials and material_key not in allowed_materials:
                    raise self._schema_error(
                        "证据引用的产物材料不在本条规则允许范围内",
                        artifact_id=artifact_id, material_key=material_key,
                    )
                checked = self._check_quote_fragments(item, source.get("content"), artifact_id=artifact_id)
                resolved.append({
                    "artifact_id": source["artifact_id"],
                    "artifact_type": source.get("artifact_type"),
                    "material_key": source["material_key"],
                    "upload_id": source.get("upload_id"),
                    **checked,
                    "artifact_page": self._artifact_page(
                        source.get("content"), checked.get("quote_text") or checked.get("quote_label"),
                    ),
                    "output_code": item.get("output_code"),
                })
                continue
            if has_fact_reference:
                fact_id = item.get("fact_id")
                if fact_id is not None and (isinstance(fact_id, bool) or not isinstance(fact_id, int)):
                    raise self._schema_error("证据引用里的 fact_id 必须是上下文提供的整数", index=index)
                if material_key is not None and not isinstance(material_key, str):
                    raise self._schema_error("证据引用里的 material_key 必须是字符串或为空", index=index)
                references = {key: item[key] for key in ("fact_id", "field_key", "fact_code", "material_key", "period_scope", "statement_scope", "entity_scope") if key in item}
                matched = [fact for fact in facts if all(fact.get(key) == value for key, value in references.items())]
                if len(matched) != 1:
                    raise self._schema_error(
                        "事实证据引用必须唯一对应本次上下文的完整字段身份，请使用 fact_id 或 field_key 与完整口径",
                        reference=references, matched_count=len(matched),
                    )
                fact = matched[0]
                output_code = item.get("output_code")
                if output_code is not None and (
                    output_code != fact.get("field_key")
                    or fact.get("status") not in EXECUTABLE_FIELD_STATUSES
                    or fact.get("normalized_value") is None
                ):
                    raise self._schema_error("字段产出的事实引用必须对应本字段的有效取值", output_code=output_code)
                source = {key: fact[key] for key in ("source_field_value_id", "source_rule_result_id") if key in fact}
                if len(source) != 1:
                    raise self._schema_error("事实没有唯一的已保存取值来源", fact_id=fact.get("fact_id"))
                resolved.append({**{key: fact.get(key) for key in ("fact_id", "field_key", "fact_code", "material_key", "period_scope", "statement_scope", "entity_scope")},
                                 **source, "output_code": output_code})
                continue
            result_id = item.get("rule_result_id")
            if result_id is not None:
                if isinstance(result_id, bool) or not isinstance(result_id, int) or str(result_id) not in result_index:
                    raise self._schema_error("候选结论引用了本次上下文之外的规则结果", rule_result_id=result_id)
                upstream = result_index[str(result_id)]
                for identity in ("rule_key", "rule_code"):
                    if identity in item and item[identity] != upstream.get(identity):
                        raise self._schema_error("上游规则证据的身份与结果编号不一致", rule_result_id=result_id, identity=identity)
                output_code = item.get("output_code")
                if output_code is not None:
                    source_outputs = (upstream.get("result") or upstream).get("outputs") or []
                    if not any(
                        output.get("code") == output_code
                        and output.get("field_status") == FIELD_STATUS_PRESENT
                        and output.get("value") is not None
                        for output in source_outputs
                    ):
                        raise self._schema_error("上游结果没有产出本字段的有效取值", output_code=output_code)
                resolved.append({
                    "rule_result_id": upstream.get("rule_result_id"),
                    "rule_key": upstream.get("rule_key"),
                    "rule_code": upstream.get("rule_code"),
                    "output_code": output_code,
                })
                continue
            raise self._schema_error(
                "候选结论的证据引用必须给出 tool_call_id、预读产物的 artifact_id 与 material_key、"
                "fact_id、field_key、完整事实身份或本次上下文的整数 rule_result_id",
                index=index,
            )
        return resolved


    def _has_matching_field_source(
        self, refs: list[dict[str, Any]], output: dict[str, Any], definition: dict[str, Any],
        facts: list[dict[str, Any]], upstream_results: list[dict[str, Any]],
    ) -> bool:
        """只有同字段、同取值的真实来源能替代产物引文。"""
        code = output["code"]
        value = output.get("value")
        for reference in refs:
            if reference.get("artifact_id") is not None or reference.get("output_code") != code:
                continue
            if reference.get("fact_id") is not None:
                source = next(item for item in facts if item.get("fact_id") == reference["fact_id"])
                if (source.get("status") in EXECUTABLE_FIELD_STATUSES
                        and same_fact_value(
                            value, fact_projection.decode_value(source.get("normalized_value")), definition,
                        )):
                    return True
            if reference.get("rule_result_id") is not None:
                source = next(item for item in upstream_results if item.get("rule_result_id") == reference["rule_result_id"])
                for item in (source.get("result") or source).get("outputs") or []:
                    if (item.get("code") == code and item.get("field_status") == FIELD_STATUS_PRESENT
                            and same_fact_value(value, item.get("value"), definition)):
                        return True
        return False


    def _assert_recognized_scope(self, raw: dict[str, Any], *, rule: dict[str, Any],
                                 preloaded_artifacts: list[dict[str, Any]] | None,
                                 tool_results: list[dict[str, Any]]) -> None:
        """实际识别到的期间/口径/主体/单位必须能在读取正文中找到，不能凭字段名猜。"""
        if str(rule.get("execution_mode") or "") != MODE_EXTRACT:
            return
        from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
        texts: list[str] = []
        for artifact in preloaded_artifacts or []:
            if isinstance(artifact, dict) and artifact.get("content") is not None:
                texts.extend(AuditFieldEvidenceService().artifact_content_texts(artifact["content"]))
        for call in tool_results:
            if not isinstance(call, dict) or call.get("tool_name") != "TASK_DATA_READ":
                continue
            result = call.get("result") if isinstance(call.get("result"), dict) else {}
            for item in result.get("items") or []:
                if isinstance(item, dict) and item.get("content") is not None:
                    texts.extend(AuditFieldEvidenceService().artifact_content_texts(item["content"]))
        if not texts:
            return
        labels = {
            "recognized_period": "期间",
            "recognized_statement_scope": "报表口径",
            "recognized_entity_scope": "主体范围",
            "recognized_unit": "单位",
        }
        for item in raw.get("outputs") or []:
            if not isinstance(item, dict):
                continue
            for key, label in labels.items():
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                if AuditFieldEvidenceService()._check_evidence_content(texts, value) != "FOUND":
                    raise self._schema_error(
                        f"字段识别到的{label}在读取的材料正文中找不到，不能作为实际口径",
                        output_code=item.get("code"), field=key, value=value,
                    )

    def _assert_extraction_evidence(self, raw: dict[str, Any], refs: list[dict[str, Any]], *,
        rule: dict[str, Any], definitions: dict[str, dict[str, Any]],
        human_field_keys: set[str] | frozenset[str] | None,
        facts: list[dict[str, Any]], upstream_results: list[dict[str, Any]],
    ) -> None:
        """成功字段须有原文，或同字段同取值的真实来源；人工值沿用核对结论。"""
        if str(rule.get("execution_mode") or "") != MODE_EXTRACT:
            return
        human = set(human_field_keys or ())
        artifact_refs = [reference for reference in refs if reference.get("artifact_id") is not None]
        for item in raw.get("outputs") or []:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not isinstance(code, str) or code not in definitions:
                continue
            if item.get("field_status") != FIELD_STATUS_PRESENT:
                continue
            if code in human:
                continue
            if self._has_matching_field_source(refs, item, definitions[code], facts, upstream_results):
                continue
            matched = [
                reference for reference in artifact_refs
                if reference.get("output_code") == code
                and isinstance(reference.get("quote_text"), str) and reference["quote_text"].strip()
            ]
            if not matched:
                raise self._schema_error(
                    "成功抽取的字段缺少本次材料产物中的非空原文引文，本次未采信",
                    output_code=code,
                )


    def validate_verdict(self, raw: dict[str, Any],
        *,
        rule: dict[str, Any],
        tool_results: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        upstream_results: list[dict[str, Any]],
        preloaded_artifacts: list[dict[str, Any]] | None = None,
        human_field_keys: set[str] | frozenset[str] | None = None,
        inputs: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        """仅检查结果形状与真实引用；业务判断照模型结果保存。"""
        self._assert_verdict_shape(raw)
        refs = self._evidence_refs(
            raw, rule=rule, tool_results=tool_results, facts=facts, upstream_results=upstream_results,
            preloaded_artifacts=preloaded_artifacts,
        )
        input_resolutions, input_refs = validate_input_resolutions(
            raw, missing_rule_inputs(rule, list(inputs or []), facts), facts,
            lambda candidate: self._evidence_refs(
                candidate, rule=rule, tool_results=tool_results, facts=facts,
                upstream_results=upstream_results, preloaded_artifacts=preloaded_artifacts,
            ),
        )
        self._assert_recognized_scope(
            raw, rule=rule, preloaded_artifacts=preloaded_artifacts, tool_results=tool_results,
        )
        definitions = {field["field_key"]: field for field in rule.get("output_definitions") or []}
        # 单字段抽取只有一处可归属的字段：模型漏填 output_code 时按声明补齐，
        # 让本次预读或工具引文进入字段证据链；多字段规则不猜归属。
        delivery_field = (
            next(iter(definitions))
            if len(definitions) == 1 and str(rule.get("execution_mode") or "") == MODE_EXTRACT
            else None
        )
        if delivery_field is not None:
            for reference in refs:
                if (not reference.get("output_code") and (
                    reference.get("artifact_id") is not None
                    or reference.get("field_key") == delivery_field
                )):
                    reference["output_code"] = delivery_field
        for reference in refs:
            code = reference.get("output_code")
            # 模型对非字段证据常填空字符串；它不是声明产出，按未填写处理。
            if isinstance(code, str) and not code.strip():
                code = None
                reference.pop("output_code", None)
            if code is not None:
                if not isinstance(code, str) or code not in definitions:
                    raise self._schema_error("字段证据引用的 output_code 不属于本规则的输出", output_code=code)
                if (reference.get("rule_result_id") is None
                        and reference.get("material_key") != definitions[code]["material_key"]):
                    raise self._schema_error("字段证据的来源材料与声明不一致", output_code=code)
                if reference.get("artifact_id") is None:
                    output = next((
                        item for item in raw.get("outputs") or []
                        if isinstance(item, dict) and item.get("code") == code
                    ), None)
                    if (output is None or not self._has_matching_field_source(
                        [reference], output, definitions[code], facts, upstream_results,
                    )):
                        raise self._schema_error("字段取值与所引用的事实或上游产出不一致", output_code=code)
        self._assert_extraction_evidence(
            raw, refs, rule=rule, definitions=definitions,
            human_field_keys=human_field_keys,
            facts=facts, upstream_results=upstream_results,
        )
        verdict = {
            "presentation_version": 2,
            "status": raw["status"],
            "message": raw["message"].strip(),
            "basis": raw["basis"],
            "missing_items": raw["missing_items"],
            "next_action": raw["next_action"].strip(),
            "evidence": [*refs, *input_refs],
            "input_resolutions": input_resolutions,
            "outputs": raw.get("outputs", []),
            "report": raw.get("report"),
            "rule_key": rule.get("rule_key"),
            "rule_name": rule.get("name"),
        }
        referenced_calls = {item.get("tool_call_id") for item in verdict["evidence"] if item.get("tool_call_id")}
        verdict["tool_findings"] = [
            {"tool_call_id": item["tool_call_id"], "tool_name": item["tool_name"],
             **{key: item["result"][key] for key in ("comparison", "per_material", "input_snapshot") if key in item["result"]}}
            for item in tool_results if item.get("tool_call_id") in referenced_calls
            and any(key in item.get("result", {}) for key in ("comparison", "per_material"))
        ]
        # DONE 表示本条模型执行形成结果，不代表业务通过；材料不足也必须保存真实原因。
        return verdict, LOOP_DONE, [{"kind": "BUSINESS_DATA_MISSING", "message": item} for item in raw["missing_items"]]


    def _task_facts(self, task: AuditTask, materials: list[dict[str, Any]], *,
                    rule_inputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """从统一事实读取可用取值，不再逐条与字段展示副本对账。"""
        definitions = {f["field_key"]: f for f in task.task_schema_json["fields"]}
        if rule_inputs is not None:
            selected = {r["field_key"] for r in rule_inputs}
            definitions = {k: f for k, f in definitions.items() if k in selected}
        from app.modules.audit.field.service.fact_context import AuditFieldFactContextService
        rows = AuditRuleCRUD(self.db).current_facts_for_definitions(
            task.id, definitions, AuditFieldFactContextService(self.db).fact_owner_field_keys(task),
        )
        return AuditFieldFactContextService(self.db).project_task_facts(
             task, rows, definitions=definitions,
            material_states={m["material_key"]: m.get("material_state") for m in materials},
        )


    def _task_materials(self, task: AuditTask) -> list[dict[str, Any]]:
        """当前任务材料与上传状态，包括尚未上传的材料。"""
        pairs = _current_uploads(self.db, task)
        return _material_entries(pairs, _material_states(pairs))


    def rule_data(self, task: AuditTask, rule: dict[str, Any]) -> dict[str, Any]:
        """直接读取当前规则的实际输入；内容由图状态保存，不另建规则输入。"""
        materials = self._task_materials( task)
        mode = str(rule.get("execution_mode") or MODE_UNKNOWN)
        # 汇总/风险规则只消费来源结果；材料检查只读材料本身，都不加载字段事实。
        source_only = mode in SOURCE_ONLY_MODES or mode == MODE_MATERIAL_CHECK
        references = [] if source_only else fact_projection.declared_inputs_of(rule)
        facts = self._task_facts( task, materials,
                            rule_inputs=[] if source_only else (None if rule.get("kind") == "REPORT" else references))
        definition = rule.get("calculation_definition") or {}
        result_codes = set(rule.get("source_rule_codes") or []) | set(definition.get("depends_on_result_codes") or [])
        result_codes.update(p["source_code"] for p in definition.get("parameters") or []
                            if p.get("source_type") == "CALC_RESULT" and p.get("source_code"))
        derived = AuditRuleCRUD(self.db).current_derived_facts(
            task.id, result_codes, rule.get("kind") == "REPORT",
        )
        from app.modules.audit.field.service.fact_context import AuditFieldFactContextService
        derived_facts = AuditFieldFactContextService(self.db).project_task_facts( task, derived)
        facts.extend(derived_facts)
        return {"materials": materials, "facts": facts,
                "inputs": fact_projection.resolve_rule_inputs(facts, references)}


    def tool_definitions(self, allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """根据当前循环的工具列表生成实际调用定义。"""
        return [tool_service.resolve(item["tool_id"]).to_definition() for item in allowed_tools or []]


    def available_tools(self) -> list[dict[str, Any]]:
        """当前有真实实现、可由模型选择的工具。"""
        return [{"tool_id": spec.tool_id} for spec in tool_catalog.TOOL_SPECS.values()
                if spec.agent_selectable and spec.implemented]


    def execution_context(self, *, task: AuditTask, scene_code: str,
                          node_code: str, rule_instance_id: str,
                          material_refs: tuple[str, ...]) -> ToolExecutionContext:
        """工具只需要任务身份、规则与材料范围，以及运行期会话。"""
        return ToolExecutionContext(task_id=task.id, scene_code=scene_code, node_code=node_code,
                                    rule_instance_id=rule_instance_id, material_refs=material_refs,
                                    database_session=self.db)


    def invoke_tool(self, *, call: dict[str, Any], inputs: list[dict[str, Any]],
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
            AuditRuleCRUD(self.db).commit()
            return result
        except BusinessError as exc:
            if exc.code in _TOOL_PROGRAM_ERRORS:
                logger.error("工具实现或运行环境错误 tool=%s code=%s message=%s",
                             call["tool_name"], exc.code, exc.message)
                raise
            if is_channel_failure(exc.code):
                # 工具内部的大模型请求（如风险判断）遇到渠道故障：作为渠道故障带回，
                # 由调度方暂停后续请求；不能转成普通工具失败让模型继续尝试，也不能抛异常，
                # 否则外层会先把批次落成通用失败，走不到渠道故障的暂停流程。
                logger.warning("工具内部模型渠道故障 tool=%s code=%s message=%s",
                               call["tool_name"], exc.code, exc.message)
                return {
                    "status": "FAILED",
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "channel_failure": channel_failure_detail(
                        exc.code, exc.message, rule_key=rule.get("rule_key"),
                        result_unknown=bool((exc.details or {}).get("result_unknown")),
                        details=exc.details,
                    ),
                }
            logger.warning("工具调用失败 tool=%s code=%s message=%s details=%s",
                           call["tool_name"], exc.code, exc.message, exc.details)
            return {"status": "FAILED", "code": exc.code, "message": exc.message, "details": exc.details}


    def tool_result_entry(self, call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
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
