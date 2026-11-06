"""可调用工具及其真实实现目录。

模型直接执行规则，按需选择工具；目录描述参数、实际能力与权限，不约束业务阶段。
执行时使用当前真实工具目录，工具错误如实回传，不提供模拟成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.common.exceptions import BusinessError
from app.domain import file_quality

# 工具的名称、输出与描述元数据；输入参数以本文件的 JSON Schema 为准。
KIND_OPERATOR = "OPERATOR"
KIND_QUALITY_ITEM = "QUALITY_ITEM"
INPUT_NONE = "NONE"
INPUT_MATERIAL_SCOPE = "MATERIAL_SCOPE"
JUDGE_PROGRAM = "PROGRAM"
JUDGE_LLM = "LLM"


@dataclass(frozen=True)
class _ToolMetadata:
    tool_id: str
    label: str
    kind: str
    input_kind: str
    statuses: frozenset[str]
    implemented: bool
    judge: str
    output_keys: tuple[str, ...] = ()
    required_output_keys: tuple[str, ...] = ()
    version: str = "1"
    note: str = ""
    unimplemented_reason: str = ""


_OPERATOR_METADATA: tuple[_ToolMetadata, ...] = (
    _ToolMetadata(
        tool_id="RISK_TRIGGER",
        version="2",
        label="按场景条件判断风险",
        kind=KIND_OPERATOR,
        input_kind=INPUT_NONE,
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT", "NOT_EXECUTED", "PENDING_REVIEW"}),
        implemented=True,
        judge=JUDGE_LLM,
        output_keys=("source_rules", "source_instances", "risk_condition", "risk_level", "action", "evidence_sources"),
        note="来源缺项或判断依据不足时明确不可判定，不默认条件或风险等级。",
    ),
)

_QUALITY_METADATA: tuple[_ToolMetadata, ...] = tuple(
    _ToolMetadata(
        tool_id=check_item,
        label=file_quality.label_of(check_item),
        kind=KIND_QUALITY_ITEM,
        input_kind=INPUT_MATERIAL_SCOPE,
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT"}),
        implemented=file_quality.is_implemented(check_item),
        unimplemented_reason=(
            ""
            if file_quality.is_implemented(check_item)
            else (
                "印章识别只返回印章数量与位置，不能证明盖章正确"
                if file_quality.gap_code_for(check_item) == file_quality.GAP_SEAL_COUNT_IS_NOT_QUALITY
                else "缺少已确认的判定口径（判定算法、阈值或多页汇总方式）"
            )
        ),
        judge=JUDGE_PROGRAM,
        output_keys=("per_material", "comparison", "judgement_supported")
        + (("actual_bytes", "limit_bytes") if check_item == "FILE_SIZE" else ())
        + (("duplicates",) if check_item == "FILE_DUPLICATE" else ())
        + (("declared_content_type",) if check_item == "FILE_FORMAT" else ()),
    )
    for check_item in sorted(file_quality.KNOWN_QUALITY_ITEMS)
)

_TOOL_METADATA = {item.tool_id: item for item in (*_OPERATOR_METADATA, *_QUALITY_METADATA)}


# handler 所在的模块与函数。解析在 modules.audit.field.service.tools.runtime 里完成：
# domain 层不导入 operators 与 services，避免领域层依赖执行层。
_OPERATOR_MODULE = "app.operators.registry"
_OPERATOR_FUNCTION = "execute_operator"
# 需要任务上下文与数据库会话的文件质量检查走 adapter 模块。
# adapter 不含判定逻辑：它把上下文转成共享领域服务的输入，然后调同一个实现。
_ADAPTER_MODULE = "app.modules.audit.field.service.tools.handlers"

# 当前可独立调用的算子工具：原函数签名为 `(parameters, inputs) -> dict`，
# 标准 @tool 对象保存在 operators.registry.REGISTRY 中，调用通过 .invoke() 完成。
_CALLABLE_OPERATORS: tuple[str, ...] = ("RISK_TRIGGER",)

# 工具输入只提交身份引用，真实取值由系统按身份读取。
_INPUT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field_key": {"type": "string", "minLength": 1},
        "material_key": {"type": "string", "minLength": 1},
    },
    "required": ["field_key"],
    "additionalProperties": False,
}


def _inputs_schema(*, min_items: int, max_items: int | None = None) -> dict[str, Any]:
    """`inputs` 数组的 Schema。上下界来自能力目录的 `input_kind`。"""
    schema: dict[str, Any] = {"type": "array", "items": _INPUT_ITEM_SCHEMA, "minItems": min_items}
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


# 每个可调用工具的真实参数 Schema；风险来源由调用方按编号注入。
_OPERATOR_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "RISK_TRIGGER": {
        "parameters": {
            "type": "object",
            "properties": {
                "risk_level": {"type": "string", "pattern": r"\S"},
                "action": {"type": "string", "minLength": 1},
                # 来源检查的身份引用：模型只说明这条风险监视哪些来源规则，
                # 各来源当时的真实结论由调用方按业务编号解析后经 runtime 注入。
                # 模型不得自己填写来源状态——那是当前有效执行批次的真实结果。
                "source_rule_codes": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                },
            },
            "required": ["risk_level", "action", "source_rule_codes"],
            "additionalProperties": False,
        },
        # 风险不直接吃字段输入：来源检查的当前有效结果由调用方按 source_rule_codes 解析。
        "inputs": {"type": "array", "items": _INPUT_ITEM_SCHEMA, "maxItems": 0},
    },
}

# 文件质量工具的参数 Schema：检查项目决定要不要标准值。
# 模型填的是业务口径（查哪个项目、查哪些材料、合格标准是多少），
# 真实文件由运行时按材料位读取。
_FILE_QUALITY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "check_item": {"type": "string", "minLength": 1},
        "material_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        # 标准值与单位来自业务配置。缺标准值时判定会明确报错，不代填默认阈值。
        "standard_value": {"type": "string", "minLength": 1},
        "unit": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
    },
    "required": ["check_item", "material_refs"],
    "additionalProperties": False,
}

# 需要调用方注入运行期状态的工具：键名 → 说明。
# 这些值不能由模型提供，它们是当前有效执行批次的真实结果或已发布配置事实。
# 模型只给身份引用，调用方解析后经 `invoke(runtime=...)` 注入。
_RUNTIME_KEYS: dict[str, dict[str, str]] = {
    "RISK_TRIGGER": {
        "source_results": "各来源检查和计算的真实结果、字段输入及判断依据，按来源引用顺序给出",
        "risk_condition": "风险成立条件完整原文，来自已发布配置",
        "requirement": "风险业务要求原文，来自已发布配置",
    },
}


@dataclass(frozen=True)
class ToolSpec:
    """一项可被模型调用的工具。

    参数 Schema、真实 handler 与实现版本在本目录统一声明。
    """

    tool_id: str
    version: str
    label: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # handler 的可解析引用；不可调用时为 None。绝不提供默认实现。
    handler_ref: dict[str, str] | None
    # 允许的场景类型；None 表示全部场景（当前场景 Profile 未声明工具范围，不编造约束）。
    allowed_scenes: frozenset[str] | None
    # 执行前必须准备好的真实依据（产物类型等）。空表示只需字段事实。
    required_evidence: tuple[str, ...]
    # 是否有外部副作用。全部算子都是纯计算，无副作用。
    side_effect: bool
    # 同一组参数与输入重复调用是否得到同一结果。
    idempotent: bool
    # 是否支持按作业号查询执行进度（外部作业才需要）。
    supports_query: bool
    # 当前能否进入真实 handler：有可独立调用的 handler；业务判定是否有口径由
    # judgement_supported 单独表达。这样“可调用但只能返回不可判定”不会被误报成无入口。
    implemented: bool
    # 模型是否可以选择该工具，由实际调用入口决定。
    agent_selectable: bool
    unimplemented_reason: str = ""
    # `agent_selectable=False` 的真实原因。为空表示该工具就是 Agent 可选择的。
    # 页面据此区分「已注册但不能被模型调用」与「模型可以调用」，
    # 而不是把两者都显示成一个笼统的「可调用」。
    agent_unselectable_reason: str = ""
    # 规则输入数量口径，供调用方准备 inputs 时参照。
    input_count: str = ""
    # 需要调用方注入的运行期键 → 说明。模型不提供这些值（见 _RUNTIME_KEYS）。
    runtime_keys: tuple[str, ...] = ()
    # 业务判定是否有真实实现；None 表示该工具只读取数据，不作判定。
    judgement_supported: bool | None = None
    judgement_unsupported_reason: str = ""

    def to_definition(self) -> dict[str, Any]:
        """当前工具的调用定义与实际实现信息。"""
        return {
            "tool_id": self.tool_id,
            "tool_version": self.version,
            # OpenAI function name：模型请求工具时用的标识。
            "name": self.tool_id,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            # 保存实现引用与工具版本，便于执行时定位真实处理器。
            "handler_ref": dict(self.handler_ref) if self.handler_ref else None,
            # 工具的场景权限范围。
            "allowed_scenes": sorted(self.allowed_scenes) if self.allowed_scenes is not None else None,
            "required_evidence": list(self.required_evidence),
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
            "judgement_supported": self.judgement_supported,
            "judgement_unsupported_reason": self.judgement_unsupported_reason or None,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "handler_ref": dict(self.handler_ref) if self.handler_ref else None,
            "allowed_scenes": sorted(self.allowed_scenes) if self.allowed_scenes is not None else None,
            "required_evidence": list(self.required_evidence),
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
            "supports_query": self.supports_query,
            # 四个口径分别如实返回，页面据此区分「已注册 / 节点级可调用 /
            # Agent 可选择 / 已实现」，不再把它们混成一个「可调用」。
            "registered": True,
            "implemented": self.implemented,
            "judgement_supported": self.judgement_supported,
            "judgement_unsupported_reason": self.judgement_unsupported_reason or None,
            "agent_selectable": self.agent_selectable,
            "agent_unselectable_reason": self.agent_unselectable_reason or None,
            "unimplemented_reason": self.unimplemented_reason or None,
            "input_count": self.input_count or None,
            "runtime_keys": list(self.runtime_keys),
        }


def _output_schema_of(metadata: _ToolMetadata) -> dict[str, Any]:
    """工具输出的 Schema，从能力目录的 `statuses` 与 `output_keys` 派生。

    `status` 与输出键取自本工具的元数据，规则结果状态另由 `constants/status.py` 说明。
    `required` 只含每条返回路径都会给出的键（`status`、`message`，以及能力目录用
    `required_output_keys` 确认过的）。把「可能出现」写成「必填」会放行缺字段的结果，
    把「保证出现」漏掉又会拒绝合法结果，两者必须以同一个来源为准。
    """
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": sorted(metadata.statuses)},
        "message": {"type": "string"},
    }
    for key in metadata.output_keys:
        properties[key] = {}
    required = ["status", "message"]
    for key in metadata.required_output_keys:
        if key not in properties:
            raise BusinessError(
                "TOOL_CATALOG_OUTPUT_CONTRACT_INVALID",
                f"能力“{metadata.tool_id}”把未声明的输出键标成了必填，工具目录与能力目录不一致",
                status_code=500,
                details={"tool_id": metadata.tool_id, "key": key},
            )
        required.append(key)
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
    }


def _input_count_text(metadata: _ToolMetadata) -> str:
    return {
        INPUT_NONE: "无字段输入",
        INPUT_MATERIAL_SCOPE: "按材料位判定",
    }.get(metadata.input_kind, metadata.input_kind)


def _description_of(metadata: _ToolMetadata) -> str:
    """工具描述：能力用途 + 输入口径 + 关键约束。

    描述里写清「输入是身份引用，不是业务取值」，避免模型把金额或日期抄进参数——
    抄错一个数字后算对了另一道题，是这类工具最危险的失败方式。
    """
    parts = [metadata.note or metadata.label]
    parts.append(f"输入口径：{_input_count_text(metadata)}；inputs 只填 field_key 与 material_key，"
                 "真实取值由系统按字段事实解析，不要传入金额或日期文本。")
    if metadata.judge == JUDGE_PROGRAM:
        parts.append("判定由确定性程序完成，结果不可被模型改写。")
    return " ".join(part for part in parts if part)


def _agent_selectable_of(metadata: _ToolMetadata) -> bool:
    """真实调用入口决定工具是否可选。"""
    return _handler_ref_of(metadata.tool_id) is not None


def _agent_unselectable_reason(metadata: _ToolMetadata) -> str:
    return (
        "该能力没有独立工具 handler；模型可以按本条原文直接执行，实际产出由规则结果记录"
    )


def _handler_ref_of(tool_id: str) -> dict[str, str] | None:
    """算子工具的真实 handler 引用；不可调用时为 None。

    标准 @tool 对象保存在 `operators.registry.REGISTRY` 中，由工具服务按算子码执行。
    文件质量与数据读取工具在各自的 ToolSpec 中直接声明 adapter，不经过这里。
    """
    if tool_id in _CALLABLE_OPERATORS:
        return {"module": _OPERATOR_MODULE, "function": _OPERATOR_FUNCTION, "operator": tool_id}
    return None


def _build_operator_tool(metadata: _ToolMetadata) -> ToolSpec:
    schema = _OPERATOR_ARGUMENT_SCHEMAS[metadata.tool_id]
    handler_ref = _handler_ref_of(metadata.tool_id)
    # handler 决定能否调用；判定能力由 judgement_supported 独立表达。
    callable_now = handler_ref is not None
    reason = "" if callable_now else metadata.unimplemented_reason
    selectable = callable_now and _agent_selectable_of(metadata)
    return ToolSpec(
        tool_id=metadata.tool_id,
        version=metadata.version,
        label=metadata.label,
        description=_description_of(metadata),
        input_schema={
            "type": "object",
            "properties": {"inputs": schema["inputs"], "parameters": schema["parameters"]},
            "required": ["inputs", "parameters"],
            "additionalProperties": False,
        },
        output_schema=_output_schema_of(metadata),
        handler_ref=handler_ref if callable_now else None,
        allowed_scenes=None,
        # 算子只读字段事实：产物依赖由准备输入的步骤负责，
        # 这里如实声明为空，不假装工具自己会去取产物。
        required_evidence=(),
        side_effect=False,
        idempotent=True,
        supports_query=False,
        implemented=callable_now,
        unimplemented_reason=reason,
        agent_selectable=selectable,
        agent_unselectable_reason="" if selectable else _agent_unselectable_reason(metadata),
        input_count=_input_count_text(metadata),
        runtime_keys=tuple(sorted(_RUNTIME_KEYS.get(metadata.tool_id, {}))),
        judgement_supported=metadata.implemented,
        judgement_unsupported_reason=metadata.unimplemented_reason,
    )


def _build_quality_tool(metadata: _ToolMetadata) -> ToolSpec:
    """文件检查子项：逐项成工具。

    每个子项都是独立工具，`check_item` 由工具身份固定，模型不需要（也不能）自己选——
    选错检查项目就是调了另一个工具。模型只需给要检查的材料位与合格标准。

    判定调 `services.audit.tools.handlers.check_file_quality_tool`，
    使用 `domain/quality_judgement` 的实现。
    """
    check_item = metadata.tool_id
    artifact = file_quality.QUALITY_ITEM_ARTIFACT.get(check_item)
    implemented = file_quality.is_implemented(check_item)
    # 没有实际判定实现的项目只在目录说明，不提供给模型调用。
    parameters = {
        "type": "object",
        "properties": {
            "material_refs": _FILE_QUALITY_PARAMETERS["properties"]["material_refs"],
            "standard_value": _FILE_QUALITY_PARAMETERS["properties"]["standard_value"],
            "unit": _FILE_QUALITY_PARAMETERS["properties"]["unit"],
            "requirement": _FILE_QUALITY_PARAMETERS["properties"]["requirement"],
        },
        "required": ["material_refs"],
        "additionalProperties": False,
    }
    return ToolSpec(
        tool_id=check_item,
        version=metadata.version,
        label=metadata.label,
        description=(
            f"{metadata.label}（文件质量检查）。"
            + (f"依赖的处理产物：{artifact}。" if artifact else "只读取原始上传的格式声明、字节数或内容哈希，无需处理产物。")
            + "判定按材料位逐份完成。"
            + ("" if implemented else "本项没有实际判定实现，不能作为工具调用。")
        ),
        input_schema={
            "type": "object",
            "properties": {
                "inputs": _inputs_schema(min_items=0),
                "parameters": parameters,
            },
            "required": ["inputs", "parameters"],
            "additionalProperties": False,
        },
        output_schema=_output_schema_of(metadata),
        handler_ref={
            "module": _ADAPTER_MODULE,
            "function": "check_file_quality_tool",
            "context": "true",
            # 检查项目由工具身份固定：adapter 据此判定，模型不能改。
            "check_item": check_item,
        },
        allowed_scenes=None,
        required_evidence=(artifact,) if artifact else (),
        side_effect=False,
        idempotent=True,
        supports_query=False,
        implemented=implemented,
        unimplemented_reason="" if implemented else "本项没有实际判定实现",
        agent_selectable=implemented,
        agent_unselectable_reason="" if implemented else "本项没有实际判定实现",
        input_count="按材料位逐份判定",
        judgement_supported=implemented,
        judgement_unsupported_reason="" if implemented else "本项没有实际判定实现",
    )


def _build_rule_library_query_tool() -> ToolSpec:
    """审核 Agent 查询任务冻结规则库的只读工具。"""
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["ALL", "RULE", "CALCULATION"]},
            "rule_codes": {"type": "array", "items": {"type": "string"}},
            "rule_keys": {"type": "array", "items": {"type": "string"}},
        },
        "required": [],
        "additionalProperties": False,
    }
    return ToolSpec(
        tool_id="RULE_LIBRARY_QUERY",
        version="1",
        label="查询规则库",
        description="只读查询本次审核任务冻结的生效规则及计算规则。不填 rule_codes/rule_keys 时返回目录（编号、名称、执行方式、业务要求），不返回完整配置；按 rule_codes 或 rule_keys 精确查询时返回完整配置和来源原文。query 在完整配置中匹配关键词；多个条件取交集，查询成功不代表审核通过。",
        input_schema={
            "type": "object",
            "properties": {"inputs": _inputs_schema(min_items=0, max_items=0), "parameters": parameters},
            "required": ["inputs", "parameters"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["SUCCEEDED"]},
                "message": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
                "source": {"type": "string"},
                "scenario_version_no": {},
                "plan_scope_hash": {},
            },
            "required": ["status", "message", "items", "source"],
        },
        handler_ref={"module": _ADAPTER_MODULE, "function": "query_rule_library_tool", "context": "true"},
        allowed_scenes=None,
        required_evidence=(),
        side_effect=False,
        idempotent=True,
        supports_query=False,
        implemented=True,
        agent_selectable=True,
        agent_unselectable_reason="",
        input_count="查询条件可选",
    )


def _build_task_data_tool(process: bool = False) -> ToolSpec:
    properties = {"material_keys": {"type": "array", "items": {"type": "string"}}}
    if process:
        properties["service"] = {"type": "string", "enum": ["DOCUMENT_PARSE", "OCR", "SEAL", "MATERIAL_CLASSIFY"]}
    else:
        properties["artifact_type"] = {"type": "string", "enum": ["OCR", "SEAL", "MATERIAL_CLASSIFY"]}
    return ToolSpec(
        tool_id="DOCUMENT_PROCESS" if process else "TASK_DATA_READ", version="1",
        label="材料前置处理" if process else "读取材料内容",
        description=("文件处理/OCR/印章/材料识别由程序在业务审核前执行，模型不负责触发。"
                     if process else "读取已经完成前置处理的材料产物。正文使用artifact_type=OCR，返回去章文件的逐页正文（带页码）；印章检验使用SEAL，返回原文件的印章主体等信息；材料类型识别结论使用MATERIAL_CLASSIFY，仅供参考。未指定类型时读取OCR正文。"),
        input_schema={"type": "object", "properties": {"inputs": _inputs_schema(min_items=0, max_items=0),
                      "parameters": {"type": "object", "properties": properties,
                                     "required": ["material_keys", "service"] if process else ["material_keys"], "additionalProperties": False}},
                      "required": ["inputs", "parameters"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"status": {"type": "string"}, "message": {"type": "string"}, "items": {"type": "array"}}, "required": ["status", "message", "items"]},
        handler_ref={"module": _ADAPTER_MODULE, "function": "process_documents_tool" if process else "read_task_data_tool", "context": "true"},
        allowed_scenes=None, required_evidence=(), side_effect=process,
        idempotent=True, supports_query=False, implemented=True, agent_selectable=not process,
        agent_unselectable_reason="由程序在规则执行前统一完成，模型只读取处理产物" if process else "",
        input_count="材料身份列表，inputs填空数组",
    )


def _build_tool_specs() -> dict[str, ToolSpec]:
    specs: dict[str, ToolSpec] = {}
    for metadata in _TOOL_METADATA.values():
        if metadata.kind == KIND_QUALITY_ITEM:
            specs[metadata.tool_id] = _build_quality_tool(metadata)
            continue
        specs[metadata.tool_id] = _build_operator_tool(metadata)
    specs["RULE_LIBRARY_QUERY"] = _build_rule_library_query_tool()
    specs["TASK_DATA_READ"] = _build_task_data_tool()
    specs["DOCUMENT_PROCESS"] = _build_task_data_tool(process=True)
    return specs


# 工具目录：工具标识 → 工具定义。顺序按标识排序，保证序列化结果稳定。
TOOL_SPECS: dict[str, ToolSpec] = dict(sorted(_build_tool_specs().items()))


def tool_spec(tool_id: str | None) -> ToolSpec | None:
    """按标识取工具；未知标识返回 None（调用方必须显式处理，不得当作已支持）。"""
    if not isinstance(tool_id, str):
        return None
    return TOOL_SPECS.get(tool_id)


def catalog_payload() -> list[dict[str, Any]]:
    """实际工具目录的对外结构，按标识排序。"""
    return [spec.to_payload() for spec in TOOL_SPECS.values()]
