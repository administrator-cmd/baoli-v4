"""可调用工具及其真实实现目录。

模型直接执行规则，按需选择工具；目录描述参数、实际能力与权限，不约束业务阶段。
执行时使用当前真实工具目录，工具错误如实回传，不提供模拟成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from ..common.exceptions import BusinessError
from .audit_vocab import OPERATOR_RESULT_STATUSES

from . import file_quality
from .parameter_contract import (
    DATE_COMPARE_OPERATORS,
    DATE_THRESHOLD_UNIT,
    DECIMAL_COMPARE_OPERATORS,
    DECIMAL_SUM_COMPARE_OPERATORS,
)

# 工具的名称、输出与描述元数据；输入参数以本文件的 JSON Schema 为准。
KIND_OPERATOR = "OPERATOR"
KIND_QUALITY_ITEM = "QUALITY_ITEM"
INPUT_NONE = "NONE"
INPUT_FIELD_ONE = "FIELD_ONE"
INPUT_FIELD_PAIR = "FIELD_PAIR"
INPUT_FIELD_LIST = "FIELD_LIST"
INPUT_FIELD_SET = "FIELD_SET"
INPUT_OUTPUT_FIELDS = "OUTPUT_FIELDS"
INPUT_MATERIAL_SCOPE = "MATERIAL_SCOPE"
JUDGE_PROGRAM = "PROGRAM"
JUDGE_LLM = "LLM"
JUDGE_HUMAN = "HUMAN_REVIEW"


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
        tool_id="MATERIAL_EXISTS",
        label="材料是否齐全",
        kind=KIND_OPERATOR,
        input_kind=INPUT_MATERIAL_SCOPE,
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT"}),
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("applicability", "standard", "comparison", "missing_material_keys", "missing_material_names", "checked_material_keys"),
        # 这些项由 `quality_judgement.material_exists_result` 在每条返回路径上给出：
        # 节点实现与 Agent adapter 都调它，因此「保证出现」是可核对的。
        required_output_keys=("applicability", "standard", "comparison", "missing_material_keys", "missing_material_names", "checked_material_keys"),
        note="按规则声明的材料位逐项核对是否已上传；比较对象是「已上传的材料位」与「本检查声明的材料位」。",
    ),
    _ToolMetadata(
        tool_id="FIELD_EQUAL",
        version="2",
        label="字段是否一致",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_PAIR,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
    ),
    _ToolMetadata(
        tool_id="FIELD_NOT_EQUAL",
        version="2",
        label="字段应当不同",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_PAIR,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
    ),
    _ToolMetadata(
        tool_id="DATE_COMPARE",
        label="日期先后比较",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_PAIR,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
        note="日期标准化当前只认 YYYY-MM-DD / YYYY/M/D / YYYY.M.D / YYYY年M月D日 这几种原文写法。",
    ),
    _ToolMetadata(
        tool_id="DECIMAL_COMPARE",
        version="3",
        label="数值比较",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_PAIR,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
    ),
    _ToolMetadata(
        tool_id="DECIMAL_SUM_COMPARE",
        version="3",
        label="数值加总比较",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_LIST,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison", "sum_value"),
    ),
    _ToolMetadata(
        tool_id="DECIMAL_CN_EQUAL",
        label="大小写金额一致性",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_PAIR,
        statuses=frozenset({"EVIDENCE_INSUFFICIENT", "PENDING_REVIEW"}),
        # 该工具尚无确定性换算实现，调用时返回具体不可执行原因。
        # 此限制仅描述工具，不限制模型直接按规则原文执行。
        implemented=False,
        unimplemented_reason=(
            "把大写金额换算成有证据的数值需要业务先确认金额单位、币种与大写金额换算口径；"
            "系统不代填默认口径，本项当前不可判定"
        ),
        judge=JUDGE_HUMAN,
        output_keys=("extracted_values",),
        note="换算口径确认后接入真实换算实现；在此之前规则可以配置，但不能作为可执行结论。",
    ),
    _ToolMetadata(
        tool_id="FIELD_NOT_EMPTY",
        label="字段非空",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_ONE,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
        note="只判断有效事实是否存在且非空；抽取失败与未执行都不等于「为空」。",
    ),
    _ToolMetadata(
        tool_id="CLAUSE_ABSENT",
        label="条款不应存在",
        kind=KIND_OPERATOR,
        input_kind=INPUT_FIELD_ONE,
        statuses=OPERATOR_RESULT_STATUSES,
        implemented=True,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison", "clause_exists"),
        note="判断条款本身是否存在，不按「抽取到的文字是否非空」判断；只能引用条款类型字段。",
    ),
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
OPERATOR_LABELS = {item.tool_id: item.label for item in _TOOL_METADATA.values()}


# handler 所在的模块与函数。解析在 services.tool_service 里完成：
# domain 层不导入 operators 与 services，避免领域层依赖执行层。
_OPERATOR_MODULE = "app.operators.registry"
_OPERATOR_FUNCTION = "execute_operator"
# 需要任务上下文与数据库会话的工具（材料存在性、文件质量）走 adapter 模块。
# adapter 不含判定逻辑：它把上下文转成共享领域服务的输入，然后调同一个实现。
_ADAPTER_MODULE = "app.services.agent_tools"

# 当前可独立调用的算子工具：原函数签名为 `(parameters, inputs) -> dict`，
# 标准 @tool 对象保存在 operators.registry.REGISTRY 中，调用通过 .invoke() 完成。
_CALLABLE_OPERATORS: tuple[str, ...] = (
    "FIELD_EQUAL",
    "FIELD_NOT_EQUAL",
    "DATE_COMPARE",
    "DECIMAL_COMPARE",
    "DECIMAL_SUM_COMPARE",
    "FIELD_NOT_EMPTY",
    "CLAUSE_ABSENT",
    "RISK_TRIGGER",
    # 大小写金额一致性：判定口径未确认，但工具入口存在且会如实返回
    # 「不可判定 + 缺哪条口径」。这与「没有实现、调不了」是两件事：
    # Agent 需要能调用它并拿到「这项做不了、因为口径未确认」这个真实结论，
    # 而不是在目录里看到一个不存在的工具。
    "DECIMAL_CN_EQUAL",
)

# 需要任务上下文与数据库会话的工具 → adapter 函数名。
# 它们使用 `domain/quality_judgement.py` 的判定实现。
_CONTEXT_TOOLS: dict[str, str] = {
    "MATERIAL_EXISTS": "check_materials_tool",
    "FILE_QUALITY_CHECK": "check_file_quality_tool",
    "DECIMAL_CN_EQUAL": "decimal_cn_equal_tool",
}

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


# 每个可调用工具的真实参数 Schema；枚举取自 parameter_contract。
_OPERATOR_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "FIELD_EQUAL": {
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=2, max_items=2),
    },
    "FIELD_NOT_EQUAL": {
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=2, max_items=2),
    },
    "DATE_COMPARE": {
        "parameters": {
            "type": "object",
            "properties": {
                "operator": {"type": "string", "enum": sorted(DATE_COMPARE_OPERATORS)},
                # 非负整数天；单位只能是「天」。填写差值时必须同时给单位，
                # 参数由工具实现按实际调用核对。
                "threshold": {"type": "number", "minimum": 0},
                "threshold_unit": {"type": "string", "enum": [DATE_THRESHOLD_UNIT]},
            },
            "required": ["operator"],
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=2, max_items=2),
    },
    "DECIMAL_COMPARE": {
        "parameters": {
            "type": "object",
            "properties": {
                "operator": {"type": "string", "enum": sorted(DECIMAL_COMPARE_OPERATORS)},
                "threshold": {"type": "number", "minimum": 0},
                "threshold_unit": {"type": "string", "minLength": 1},
            },
            "required": ["operator"],
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=2, max_items=2),
    },
    "DECIMAL_SUM_COMPARE": {
        "parameters": {
            "type": "object",
            "properties": {
                "operator": {"type": "string", "enum": sorted(DECIMAL_SUM_COMPARE_OPERATORS)},
                # 加总比较的允许差值必须显式配置：缺省不能当作 0，
                # 工具不得把缺失参数当作业务默认值。
                "threshold": {"type": "number", "minimum": 0},
                "threshold_unit": {"type": "string", "minLength": 1},
            },
            "required": ["operator", "threshold"],
            "additionalProperties": False,
        },
        # 左侧待核对总额 + 至少一个加数。
        "inputs": _inputs_schema(min_items=2),
    },
    "FIELD_NOT_EMPTY": {
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=1, max_items=1),
    },
    "CLAUSE_ABSENT": {
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=1, max_items=1),
    },
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
    # 大小写金额一致性：需要左右两个字段的原文（大写金额、小写金额）。
    "DECIMAL_CN_EQUAL": {
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=2, max_items=2),
    },
    # 材料存在性：模型只声明要核对哪些材料位（业务口径），实传情况由运行时读取。
    "MATERIAL_EXISTS": {
        "parameters": {
            "type": "object",
            "properties": {
                "material_refs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                },
            },
            "required": ["material_refs"],
            "additionalProperties": False,
        },
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
    # 材料存在性检查的「适用条件」与「检查口径原文」是已发布配置事实，
    # 不是模型能填的业务参数：模型填一个适用条件就等于它自己决定这条规则适不适用。
    # 注入后由 `quality_judgement.material_exists_result` 使用同一份实现组装结果，
    # 目录声明的 applicability / standard 因此在 Agent 路径上也真实存在。
    "MATERIAL_EXISTS": {
        "applicability": "本规则的适用条件（kind / label / material_keys），来自已发布配置",
        "requirement": "本条检查的审核要求原文，来自已发布配置的规则行",
        "check_level": "本条检查的检查等级原文，来自已发布配置的规则行",
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
    # 当前能否被真实执行：底层能力已实现 且 有可独立调用的 handler。
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
    # 质量判断是否有真实实现；None 表示该工具不是质量判断项。
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

    `status` 与输出键取自本工具的元数据，规则结果状态另由 audit_vocab 说明。
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
        INPUT_FIELD_ONE: "恰好一个字段输入",
        INPUT_FIELD_PAIR: "恰好两个字段输入，顺序为左侧在前、右侧在后",
        INPUT_FIELD_LIST: "左侧待核对总额 + 至少一个加数",
        INPUT_FIELD_SET: "业务规则声明的一个或多个字段输入",
        INPUT_NONE: "无字段输入",
        INPUT_OUTPUT_FIELDS: "产出字段",
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
    elif metadata.judge == JUDGE_HUMAN:
        parts.append("本项需要人工裁决，工具只返回证据。")
    return " ".join(part for part in parts if part)


def _agent_selectable_of(metadata: _ToolMetadata) -> bool:
    """真实调用入口决定工具是否可选。"""
    return _handler_ref_of(metadata.tool_id) is not None


def _agent_unselectable_reason(metadata: _ToolMetadata) -> str:
    return (
        "该能力没有独立工具 handler；模型可以按本条原文直接执行，实际产出由规则结果记录"
    )


def _handler_ref_of(tool_id: str) -> dict[str, str] | None:
    """工具的真实 handler 引用；不可调用时为 None。

    两种工具引用：

    - 算子工具：保留 `execute_operator` 分发引用与算子码；工具服务按算子码取得
      `operators.registry.REGISTRY` 中的标准工具对象，由工具服务按调用参数执行；
    - 上下文工具：`services.agent_tools` 的标准工具对象，原函数接收
      `(parameters, inputs, *, context)`，使用 `domain/quality_judgement` 的判定实现。
    """
    if tool_id in _CALLABLE_OPERATORS:
        return {"module": _OPERATOR_MODULE, "function": _OPERATOR_FUNCTION, "operator": tool_id}
    adapter = _CONTEXT_TOOLS.get(tool_id)
    if adapter is not None:
        return {"module": _ADAPTER_MODULE, "function": adapter, "context": "true"}
    return None


def _build_operator_tool(metadata: _ToolMetadata) -> ToolSpec:
    schema = _OPERATOR_ARGUMENT_SCHEMAS[metadata.tool_id]
    handler_ref = _handler_ref_of(metadata.tool_id)
    callable_now = handler_ref is not None and (
        metadata.implemented or metadata.tool_id in _CONTEXT_TOOLS
    )
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
    )


def _build_quality_tool(metadata: _ToolMetadata) -> ToolSpec:
    """文件检查子项：逐项成工具。

    每个子项都是独立工具，`check_item` 由工具身份固定，模型不需要（也不能）自己选——
    选错检查项目就是调了另一个工具。模型只需给要检查的材料位与合格标准。

    判定调 `services.agent_tools.check_file_quality_tool`，
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
            f"依赖的处理产物：{artifact}；判定按材料位逐份完成。"
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
        required_evidence=(artifact or "",),
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
        description="只读查询本次审核任务冻结的生效规则及计算规则，返回完整配置和来源原文。query 在完整配置中匹配关键词，rule_codes 精确匹配规则或结果编号，rule_keys 精确匹配内部或候选身份。多个条件取交集，无条件返回全部生效项；查询成功不代表审核通过。",
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
                     if process else "读取已经完成前置处理的材料产物。正文使用artifact_type=OCR，返回去章文件的完整Markdown；印章检验使用SEAL，返回原文件的印章主体等信息；材料类型校验使用MATERIAL_CLASSIFY。未指定类型时读取OCR正文。"),
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
