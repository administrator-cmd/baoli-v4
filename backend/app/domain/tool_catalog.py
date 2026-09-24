"""统一工具目录：模型可以调用的工具，与它们的真实 handler 引用。

与 `capabilities.py` 的分工
---------------------------
- `capabilities.py` 声明**审核能力是什么**：业务标签、可挂节点、输入形态、
  可表达的状态、取证与判定方式、当前是否已实现。它是 22 项能力的单一事实来源，
  `manifest` 的节点约束、中文标签、路由表都从它派生。
- 本模块声明**模型可以调用什么**：工具标识、版本、描述、参数 JSON Schema、
  handler 引用、权限范围、错误码。它从能力目录派生，不另写一份能力名单。

为什么要有这一层
----------------
能力目录描述的是「这条规则由哪项能力判定」，粒度是**规则配置**；
模型需要的是「我可以调用哪个函数、参数长什么样」。两者相关但不同：

- 文件检查的 8 个子项在能力目录里是 8 项能力（`FILE_SIZE`、`FILE_FORMAT`…），
  但它们没有独立的工具入口——实现只在文件处理节点里；
- `LLM_EXTRACT` / `MATERIAL_EXISTS` / `REPORT_RENDER` 同理，只有节点级实现。

因此本目录的 `implemented` 表达的是「**这个工具当前能否被真实执行**」，
它等于「底层能力已实现」**且**「存在可独立调用的 handler」。
不满足时如实标为不可调用并写明原因，**不提供默认 handler、不伪装成已实现**。

为什么 handler 只写引用、不写函数对象
--------------------------------------
`domain` 层不允许导入 `services` 与 `operators`（否则 `manifest → capabilities →
tool_catalog → operators` 会把执行层拉进领域层，与 `domain` 零 services 依赖的
既有约束冲突）。因此这里只声明 `handler_ref`，真实解析在执行期由
`services.tool_service` 完成。这与 `services/audit/document_ports.py` 的端口注册
是同一手法：声明与实现分离，缺失时明确报错而不是静默退回。

模型调用参数的形状
------------------
工具参数**不是**让模型抄写业务取值。金额、日期与文件内容一律由系统按身份解析：

- `inputs`：字段/事实的**身份引用**列表（`field_key` + `material_key`），
  真实取值由调用方从事实快照取；
- `parameters`：规则配置（比较方向、允许差值、审核要求原文等）。

约束逐条对齐 `manifest._validate_rule_parameters`，枚举取值来自叶子模块
`parameter_contract`——两份声明漂移过一次（后端 12 个算子、页面 7 个），
因此这里只从既有来源派生，不新写取值集合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import capabilities, file_quality, node_registry
from .parameter_contract import (
    DATE_COMPARE_OPERATORS,
    DATE_THRESHOLD_UNIT,
    DECIMAL_COMPARE_OPERATORS,
    DECIMAL_SUM_COMPARE_OPERATORS,
    RISK_LEVELS,
)

# 工具目录版本：工具语义（参数、输出结构、权限范围）变化时递增。
# 已发布配置与历史任务按发布时刻冻结的版本执行，不随本模块修改被重新解释。
TOOL_CATALOG_VERSION = "1"

# 工具策略版本：**本次发布允许 Agent 在什么范围内选工具**。
# 它与目录版本分开：目录可以升级（新增工具、调整 Schema）而任务仍按旧策略执行。
# 策略变化（放开或收紧工具范围、改变越权判定口径）时递增。
TOOL_POLICY_VERSION = "1"

# handler 所在的模块与函数。解析在 services.tool_service 里完成：
# domain 层不导入 operators 与 services，避免领域层依赖执行层。
_OPERATOR_MODULE = "app.operators.registry"
_OPERATOR_FUNCTION = "execute_operator"
# 需要任务上下文与数据库会话的工具（材料存在性、文件质量）走 adapter 模块。
# adapter 不含判定逻辑：它把上下文转成共享领域服务的输入，然后调同一个实现。
_ADAPTER_MODULE = "app.services.agent_tools"

# 当前可独立调用的算子工具：它们都有 `(parameters, inputs) -> dict` 形状的 handler，
# 经 operators.registry.execute_operator 按算子码分发。
_CALLABLE_OPERATORS: tuple[str, ...] = (
    "FIELD_EQUAL",
    "FIELD_NOT_EQUAL",
    "DATE_COMPARE",
    "DECIMAL_COMPARE",
    "DECIMAL_SUM_COMPARE",
    "FIELD_NOT_EMPTY",
    "CLAUSE_ABSENT",
    "RISK_TRIGGER",
    # 大小写金额一致性：判定口径未确认，但**工具入口存在**且会如实返回
    # 「不可判定 + 缺哪条口径」。这与「没有实现、调不了」是两件事：
    # Agent 需要能调用它并拿到「这项做不了、因为口径未确认」这个真实结论，
    # 而不是在目录里看到一个不存在的工具。
    "DECIMAL_CN_EQUAL",
)

# 需要任务上下文与数据库会话的工具 → adapter 函数名。
# 它们与节点共用 `domain/quality_judgement.py` 的判定实现。
_CONTEXT_TOOLS: dict[str, str] = {
    "MATERIAL_EXISTS": "check_materials_tool",
    "FILE_QUALITY_CHECK": "check_file_quality_tool",
    "DECIMAL_CN_EQUAL": "decimal_cn_equal_tool",
}

# 只有节点级实现、当前没有独立工具入口的能力，以及真实原因。
# 这些能力**不是**未实现：它们的判定逻辑在 services.audit 的节点实现里，
# 但那个入口的签名是 (db, task, manifest, execution, node_code)，
# 需要数据库会话与任务上下文，不能作为模型可调用的工具。
_NODE_LEVEL_ONLY_REASONS: dict[str, str] = {
    "LLM_EXTRACT": "字段抽取是专用模型子流程，由字段抽取节点执行；不注册为 Agent 工具，避免模型经工具再次调用同一模型",
    "LLM_VALIDATE": "语义判断是模型推理本身，由规则执行内的专用流程完成；不注册为 Agent 工具，避免递归调用同一模型",
    "REPORT_RENDER": "报告渲染是最终结果生成动作，只允许在候选结论确认或人工审核完成后由固定节点调用",
}

# 可调用工具的真实错误码。取自 handler 实际抛出的 BusinessError（逐一核对源码），
# 不是推断：operators/registry.py 的 _require_inputs / _threshold_of / 各算子。
_OPERATOR_ERROR_CODES: dict[str, tuple[str, ...]] = {
    "FIELD_EQUAL": ("RULE_INPUT_MISSING", "RULE_VALUE_INVALID"),
    "FIELD_NOT_EQUAL": ("RULE_INPUT_MISSING", "RULE_VALUE_INVALID"),
    "DATE_COMPARE": ("RULE_INPUT_MISSING", "RULE_VALUE_INVALID", "RULE_PARAMETER_INVALID"),
    "DECIMAL_COMPARE": (
        "RULE_INPUT_MISSING",
        "RULE_VALUE_INVALID",
        "RULE_PARAMETER_INVALID",
        "RULE_PARAMETER_MISSING",
    ),
    "DECIMAL_SUM_COMPARE": (
        "RULE_INPUT_MISSING",
        "RULE_VALUE_INVALID",
        "RULE_PARAMETER_INVALID",
        "RULE_PARAMETER_MISSING",
    ),
    "FIELD_NOT_EMPTY": ("RULE_INPUT_MISSING",),
    "CLAUSE_ABSENT": ("RULE_INPUT_MISSING", "RULE_VALUE_INVALID"),
    "RISK_TRIGGER": (),
}

# 工具调用层的错误码：它们在执行前校验阶段抛出，与 handler 内部错误分开。
# 每个工具都会出现这些，因此并入其 error_codes。
_TOOL_LAYER_ERROR_CODES: tuple[str, ...] = (
    "TOOL_NOT_REGISTERED",
    "TOOL_VERSION_MISMATCH",
    "TOOL_NOT_PUBLISHED",
    "TOOL_SCENE_NOT_ALLOWED",
    "TOOL_NODE_NOT_ALLOWED",
    "TOOL_ARGUMENTS_INVALID",
)

# 输入的固定契约：模型给身份引用，系统按身份取真实取值。
# 与 operators.registry.INPUT_KEYS 同口径——那边是执行端实际读取的键。
_INPUT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field_key": {"type": "string", "minLength": 1},
        "material_key": {"type": "string", "minLength": 1},
    },
    "required": ["field_key"],
    "additionalProperties": False,
}

_MANUAL_REVIEW_SCHEMA: dict[str, Any] = {"type": "boolean"}


def _inputs_schema(*, min_items: int, max_items: int | None = None) -> dict[str, Any]:
    """`inputs` 数组的 Schema。上下界来自能力目录的 `input_kind`。"""
    schema: dict[str, Any] = {"type": "array", "items": _INPUT_ITEM_SCHEMA, "minItems": min_items}
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


# 每个可调用算子的参数 Schema。属性名与约束逐条对齐
# manifest._validate_rule_parameters；枚举取自 parameter_contract。
_OPERATOR_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "FIELD_EQUAL": {
        "parameters": {
            "type": "object",
            "properties": {"requires_manual_review": _MANUAL_REVIEW_SCHEMA},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=2, max_items=2),
    },
    "FIELD_NOT_EQUAL": {
        "parameters": {
            "type": "object",
            "properties": {"requires_manual_review": _MANUAL_REVIEW_SCHEMA},
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
                # 这一条与 manifest 的校验一致（未填差值不能单独填单位）。
                "threshold": {"type": "number", "minimum": 0},
                "threshold_unit": {"type": "string", "enum": [DATE_THRESHOLD_UNIT]},
                "requires_manual_review": _MANUAL_REVIEW_SCHEMA,
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
                "requires_manual_review": _MANUAL_REVIEW_SCHEMA,
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
                # 否则会静默放宽业务条件（与 manifest 校验同口径）。
                "threshold": {"type": "number", "minimum": 0},
                "threshold_unit": {"type": "string", "minLength": 1},
                "requires_manual_review": _MANUAL_REVIEW_SCHEMA,
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
            "properties": {"requires_manual_review": _MANUAL_REVIEW_SCHEMA},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=1, max_items=1),
    },
    "CLAUSE_ABSENT": {
        "parameters": {
            "type": "object",
            "properties": {"requires_manual_review": _MANUAL_REVIEW_SCHEMA},
            "additionalProperties": False,
        },
        "inputs": _inputs_schema(min_items=1, max_items=1),
    },
    "RISK_TRIGGER": {
        "parameters": {
            "type": "object",
            "properties": {
                "risk_level": {"type": "string", "enum": sorted(RISK_LEVELS)},
                "action": {"type": "string", "minLength": 1},
                # 来源检查的**身份引用**：模型只说明这条风险监视哪些来源规则，
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
            "properties": {"requires_manual_review": _MANUAL_REVIEW_SCHEMA},
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
# 这些值**不能**由模型提供，它们是当前有效执行批次的真实结果或已发布配置事实。
# 模型只给身份引用，调用方解析后经 `invoke(runtime=...)` 注入。
_RUNTIME_KEYS: dict[str, dict[str, str]] = {
    "RISK_TRIGGER": {
        "source_statuses": "各来源检查在当前有效执行批次里的真实结论，按 source_rule_codes 顺序给出",
        "risk_condition": "风险成立条件（ANY_ABNORMAL / ALL_ABNORMAL），来自已发布配置",
    },
    # 材料存在性检查的「适用条件」与「检查口径原文」是**已发布配置事实**，
    # 不是模型能填的业务参数：模型填一个适用条件就等于它自己决定这条规则适不适用。
    # 注入后由 `quality_judgement.material_exists_result` 与节点共用同一份实现组装结果，
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

    `tool_id` 等于能力目录的 `capability_id`：能力标识已经是「已发布配置与历史任务
    引用的稳定标识」，另造一套会让同一件事有两个名字，也让两边的漂移无从察觉。
    """

    tool_id: str
    version: str
    label: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # handler 的可解析引用；不可调用时为 None。绝不提供默认实现。
    handler_ref: dict[str, str] | None
    # 允许出现的节点实例码；空表示不受节点限制（由能力目录的 nodes 决定时非空）。
    allowed_nodes: frozenset[str]
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
    # 当前能否被真实执行：底层能力已实现 **且** 有可独立调用的 handler。
    implemented: bool
    # 是否是**模型可以选择**的工具。
    #
    # 它与 `implemented` 是两件事，必须分开表达：
    # `implemented=True` 说的是「这个工具当前能被真实执行」；本字段说的是
    # 「模型在规则评估循环里有调用它的入口」。只有挂在
    # `node_registry.AGENT_NODE_TYPES` 里的节点上的工具才是模型可选择的：
    # 材料检查、文件处理等节点由确定性实现执行，它们的工具不进模型的 tools 参数，
    # 也不进 Agent manifest（`allowed_tools_of` 据此过滤）。
    agent_selectable: bool
    error_codes: tuple[str, ...]
    unimplemented_reason: str = ""
    # `agent_selectable=False` 的真实原因。为空表示该工具就是 Agent 可选择的。
    # 页面据此区分「已注册但不能被模型调用」与「模型可以调用」，
    # 而不是把两者都显示成一个笼统的「可调用」。
    agent_unselectable_reason: str = ""
    # 规则输入数量口径，供调用方准备 inputs 时参照。
    input_count: str = ""
    # 需要调用方注入的运行期键 → 说明。模型不提供这些值（见 _RUNTIME_KEYS）。
    runtime_keys: tuple[str, ...] = ()

    def to_definition(self) -> dict[str, Any]:
        """发布期**冻结**的完整工具定义。

        模型可见的函数定义、参数与输出契约、实现身份、权限范围与目录/策略版本
        全部在这一份里，随已发布配置一起落库。恢复执行时只读它，
        不再用当前目录现场重新生成——否则目录升级后旧任务看到的定义会与首次不同。

        `catalog_version` / `catalog_hash` / `policy_version` 取当前目录与策略：
        冻结发生在发布期，那一刻的当前值就是这份定义的版本身份。
        """
        return {
            "tool_id": self.tool_id,
            "tool_version": self.version,
            # OpenAI function name：模型请求工具时用的标识。
            "name": self.tool_id,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            # 实现身份：handler 引用 + 工具版本 + 目录内容指纹，
            # 三者合起来才能回答「这次执行跑的是哪一版实现」。
            "handler_ref": dict(self.handler_ref) if self.handler_ref else None,
            # 权限范围：允许挂载的节点实例码与场景类型。
            "allowed_nodes": sorted(self.allowed_nodes),
            "allowed_scenes": sorted(self.allowed_scenes) if self.allowed_scenes is not None else None,
            "required_evidence": list(self.required_evidence),
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
            "catalog_version": TOOL_CATALOG_VERSION,
            "catalog_hash": catalog_fingerprint(),
            "policy_version": TOOL_POLICY_VERSION,
        }

    def to_openai_tool(self) -> dict[str, Any]:
        """OpenAI Chat Completions 的 `tools` 数组元素。

        描述里同时给出参数口径与输入数量要求：模型据此决定调用哪个工具、
        传几个输入引用。**不包含**业务取值——金额与日期由系统按身份解析。
        """
        return {
            "type": "function",
            "function": {
                "name": self.tool_id,
                "description": self.description,
                "parameters": self.input_schema,
            },
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
            "allowed_nodes": sorted(self.allowed_nodes),
            "allowed_scenes": sorted(self.allowed_scenes) if self.allowed_scenes is not None else None,
            "required_evidence": list(self.required_evidence),
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
            "supports_query": self.supports_query,
            # 四个口径分别如实返回，页面据此区分「已注册 / 节点级可调用 /
            # Agent 可选择 / 已实现」，不再把它们混成一个「可调用」。
            "registered": True,
            "implemented": self.implemented,
            "agent_selectable": self.agent_selectable,
            "agent_unselectable_reason": self.agent_unselectable_reason or None,
            "node_level_only": self.implemented and not self.agent_selectable,
            "error_codes": list(self.error_codes),
            "unimplemented_reason": self.unimplemented_reason or None,
            "input_count": self.input_count or None,
            "runtime_keys": list(self.runtime_keys),
        }


def _output_schema_of(capability: capabilities.Capability) -> dict[str, Any]:
    """工具输出的 Schema，从能力目录的 `statuses` 与 `output_keys` 派生。

    `status` 的枚举取自 `capabilities.RULE_STATUSES`（排序后），
    不另写一份状态集合——状态分档与中文含义由能力目录统一给出。

    `required` 只包含**每条返回路径都会给出**的键：`status`、`message`，
    以及能力目录用 `required_output_keys` 显式确认过的输出键。把「可能出现」
    写成「必填」会让输出校验放行缺字段的结果，把「保证出现」漏掉又会让校验
    拒绝合法结果，两者都必须以同一个来源为准。
    """
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": sorted(capability.statuses)},
        "message": {"type": "string"},
    }
    for key in capability.output_keys:
        properties[key] = {}
    required = ["status", "message"]
    for key in capability.required_output_keys:
        if key not in properties:
            raise BusinessError(
                "TOOL_CATALOG_OUTPUT_CONTRACT_INVALID",
                f"能力“{capability.capability_id}”把未声明的输出键标成了必填，工具目录与能力目录不一致",
                status_code=500,
                details={"capability_id": capability.capability_id, "key": key},
            )
        required.append(key)
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
    }


def _input_count_text(capability: capabilities.Capability) -> str:
    return {
        capabilities.INPUT_FIELD_ONE: "恰好一个字段输入",
        capabilities.INPUT_FIELD_PAIR: "恰好两个字段输入，顺序为左侧在前、右侧在后",
        capabilities.INPUT_FIELD_LIST: "左侧待核对总额 + 至少一个加数",
        capabilities.INPUT_NONE: "无字段输入",
        capabilities.INPUT_OUTPUT_FIELDS: "产出字段",
        capabilities.INPUT_MATERIAL_SCOPE: "按材料位判定",
    }.get(capability.input_kind, capability.input_kind)


def _description_of(capability: capabilities.Capability, spec: dict[str, Any]) -> str:
    """工具描述：能力用途 + 输入口径 + 关键约束。

    描述里写清「输入是身份引用，不是业务取值」，避免模型把金额或日期抄进参数——
    抄错一个数字后算对了另一道题，是这类工具最危险的失败方式。
    """
    parts = [capability.note or capability.label]
    parts.append(f"输入口径：{_input_count_text(capability)}；inputs 只填 field_key 与 material_key，"
                 "真实取值由系统按字段事实解析，不要传入金额或日期文本。")
    if capability.judge == capabilities.JUDGE_PROGRAM:
        parts.append("判定由确定性程序完成，结果不可被模型改写。")
    elif capability.judge == capabilities.JUDGE_HUMAN:
        parts.append("本项需要人工裁决，工具只返回证据。")
    return " ".join(part for part in parts if part)


def _unavailable_reason(capability: capabilities.Capability) -> str:
    """工具不可调用时的真实原因。

    两种来源必须分开，否则会把「能力没实现」读成「只是没接工具入口」：

    1. 底层能力本身没有判定实现（例如大小写金额一致性，换算口径未确认）；
    2. 底层能力已实现，但不注册为 Agent 工具（模型推理子流程、最终结果生成动作）。
    """
    if not capability.implemented:
        return capability.unimplemented_reason or "该能力当前没有判定实现"
    return _NODE_LEVEL_ONLY_REASONS.get(
        capability.capability_id,
        "该能力当前没有可独立调用的工具入口",
    )


def _agent_selectable_of(capability: capabilities.Capability) -> bool:
    """该工具是否是**模型可选择**的。

    判据只有一条：它可挂载的节点**全部**落在 `node_registry.AGENT_NODE_TYPES` 里。
    材料检查、文件处理等节点由确定性实现执行，它们的能力（材料齐全性、文件质量子项）
    即使底层已实现、也有可独立调用的 handler，也没有「让模型在循环里调用」的入口，
    因此不是 Agent 工具。

    扩大这份集合需要先完成副作用、输入产物、恢复与权限的完整设计，
    不能为了让目录显示得更宽就直接把节点类型加进去。
    """
    if not capability.nodes:
        return False
    return capability.nodes <= node_registry.AGENT_NODE_TYPES


def _agent_unselectable_reason(capability: capabilities.Capability) -> str:
    return (
        "该工具由确定性节点执行（" + "、".join(sorted(capability.nodes)) + "），"
        "不作为 Agent 可选工具：模型在规则评估循环里没有调用它的入口"
    )


def _handler_ref_of(capability_id: str) -> dict[str, str] | None:
    """工具的真实 handler 引用；不可调用时为 None。

    两种 handler 形状：

    - 算子工具：`operators.registry.execute_operator`，签名 `(operator, parameters, inputs)`；
    - 上下文工具：`services.agent_tools` 的 adapter，签名 `(parameters, inputs, *, context)`，
      与节点共用 `domain/quality_judgement` 的判定实现。
    """
    if capability_id in _CALLABLE_OPERATORS:
        return {"module": _OPERATOR_MODULE, "function": _OPERATOR_FUNCTION, "operator": capability_id}
    adapter = _CONTEXT_TOOLS.get(capability_id)
    if adapter is not None:
        return {"module": _ADAPTER_MODULE, "function": adapter, "context": "true"}
    return None


def _build_operator_tool(capability: capabilities.Capability) -> ToolSpec:
    # 参数 Schema 只对**可调用**的工具定义。不注册为 Agent 工具的能力
    # （LLM_EXTRACT / LLM_VALIDATE / REPORT_RENDER）仍要在目录里如实出现，
    # 因此给它们一份「不可调用」的显式形状，而不是留空或编造参数。
    schema = _OPERATOR_ARGUMENT_SCHEMAS.get(capability.capability_id)
    if schema is None:
        return _build_node_level_tool(capability)
    handler_ref = _handler_ref_of(capability.capability_id)
    callable_now = handler_ref is not None and (
        capability.implemented or capability.capability_id in _CONTEXT_TOOLS
    )
    reason = "" if callable_now else _unavailable_reason(capability)
    selectable = callable_now and _agent_selectable_of(capability)
    return ToolSpec(
        tool_id=capability.capability_id,
        version=capability.version,
        label=capability.label,
        description=_description_of(capability, schema),
        input_schema={
            "type": "object",
            "properties": {"inputs": schema["inputs"], "parameters": schema["parameters"]},
            "required": ["inputs", "parameters"],
            "additionalProperties": False,
        },
        output_schema=_output_schema_of(capability),
        handler_ref=handler_ref if callable_now else None,
        allowed_nodes=capability.nodes,
        allowed_scenes=None,
        # 算子只读字段事实：产物依赖由准备输入的那一步负责（阶段四），
        # 这里如实声明为空，不假装工具自己会去取产物。
        required_evidence=(),
        side_effect=False,
        idempotent=True,
        supports_query=False,
        implemented=callable_now,
        error_codes=tuple(
            sorted({*_TOOL_LAYER_ERROR_CODES, *_OPERATOR_ERROR_CODES.get(capability.capability_id, ())})
        ),
        unimplemented_reason=reason,
        agent_selectable=selectable,
        agent_unselectable_reason="" if selectable else _agent_unselectable_reason(capability),
        input_count=_input_count_text(capability),
        runtime_keys=tuple(sorted(_RUNTIME_KEYS.get(capability.capability_id, {}))),
    )


def _build_node_level_tool(capability: capabilities.Capability) -> ToolSpec:
    """只有节点级实现、没有独立工具入口的能力。

    它仍出现在目录里（配置引用的能力必须能在目录中查到），但
    `implemented=False` 且 `handler_ref=None`，参数 Schema 只声明
    「本工具当前不可调用」这一事实，不编造可填参数。
    """
    reason = _unavailable_reason(capability)
    return ToolSpec(
        tool_id=capability.capability_id,
        version=capability.version,
        label=capability.label,
        description=f"{capability.label}。{reason}。",
        # 空 properties + additionalProperties:false：任何参数都会被拒绝。
        # 这不是「可填但没约束」，而是「当前不接受调用」。
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        output_schema=_output_schema_of(capability),
        handler_ref=None,
        allowed_nodes=capability.nodes,
        allowed_scenes=None,
        required_evidence=(),
        side_effect=False,
        idempotent=True,
        supports_query=False,
        implemented=False,
        error_codes=tuple(sorted(_TOOL_LAYER_ERROR_CODES)),
        unimplemented_reason=reason,
        agent_selectable=False,
        agent_unselectable_reason=_agent_unselectable_reason(capability),
        input_count=_input_count_text(capability),
    )


def _build_quality_tool(capability: capabilities.Capability) -> ToolSpec:
    """文件检查子项：逐项成工具。

    每个子项都是独立工具，`check_item` 由工具身份固定，模型不需要（也不能）自己选——
    选错检查项目就是调了另一个工具。模型只需给要检查的材料位与合格标准。

    判定调 `services.agent_tools.check_file_quality_tool`，
    与文件处理节点共用 `domain/quality_judgement` 的实现。
    """
    check_item = capability.capability_id
    artifact = file_quality.QUALITY_ITEM_ARTIFACT.get(check_item)
    implemented = file_quality.is_implemented(check_item)
    # 未实现的检查项（图像清晰度、页码连续、有效签章、骑缝章）**工具入口仍然存在**：
    # Agent 需要能调用它并拿到「缺少已确认判定依据、本项不可判定」这个真实结论。
    # 「调不了」与「调了但判定不出来」是两件事，混在一起会让 Agent 无法区分
    # 「系统不支持这个检查」与「这项检查没通过」。
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
        version=capability.version,
        label=capability.label,
        description=(
            f"{capability.label}（文件质量检查）。"
            f"依赖的处理产物：{artifact}；判定按材料位逐份完成。"
            + ("" if implemented else "本项缺少已确认的判定依据，调用后会返回「不可判定」并说明缺什么。")
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
        output_schema=_output_schema_of(capability),
        handler_ref={
            "module": _ADAPTER_MODULE,
            "function": "check_file_quality_tool",
            "context": "true",
            # 检查项目由工具身份固定：adapter 据此判定，模型不能改。
            "check_item": check_item,
        },
        allowed_nodes=capability.nodes,
        allowed_scenes=None,
        required_evidence=(artifact or "",),
        side_effect=False,
        idempotent=True,
        supports_query=False,
        implemented=True,
        error_codes=tuple(sorted(_TOOL_LAYER_ERROR_CODES)),
        unimplemented_reason="",
        # 文件质量子项挂在 DOCUMENT_PREPARE 上：实现完备（节点与 adapter 共用判定），
        # 但**不作为 Agent 可选工具**——模型没有调用它的入口。
        agent_selectable=_agent_selectable_of(capability),
        agent_unselectable_reason="" if _agent_selectable_of(capability) else _agent_unselectable_reason(capability),
        input_count="按材料位逐份判定",
    )


def _build_tool_specs() -> dict[str, ToolSpec]:
    specs: dict[str, ToolSpec] = {}
    for capability in capabilities.CAPABILITIES.values():
        if capability.kind == capabilities.KIND_QUALITY_ITEM:
            specs[capability.capability_id] = _build_quality_tool(capability)
            continue
        # 父项 FILE_QUALITY_CHECK 本身不是工具：它的 8 个子项各自是独立工具，
        # 检查项目由工具身份固定。父项只作为能力目录里的分组声明存在。
        if capability.capability_id == "FILE_QUALITY_CHECK":
            continue
        specs[capability.capability_id] = _build_operator_tool(capability)
    return specs


# 工具目录：能力标识 → 工具定义。顺序按标识排序，保证序列化结果稳定。
TOOL_SPECS: dict[str, ToolSpec] = dict(sorted(_build_tool_specs().items()))


def tool_spec(tool_id: str | None) -> ToolSpec | None:
    """按标识取工具；未知标识返回 None（调用方必须显式处理，不得当作已支持）。"""
    if not isinstance(tool_id, str):
        return None
    return TOOL_SPECS.get(tool_id)


def callable_tool_specs() -> tuple[ToolSpec, ...]:
    """当前**能被真实执行**的工具，按标识排序。

    包含节点级实现与 Agent 可选择两类。要给模型生成 `tools` 参数时用
    `agent_tool_specs()`——两者不是同一个集合。
    """
    return tuple(spec for spec in TOOL_SPECS.values() if spec.implemented)


def agent_tool_specs() -> tuple[ToolSpec, ...]:
    """**模型可以选择**的工具，按标识排序。

    这是 Agent manifest 与模型 `tools` 参数的唯一来源：不可调用的工具、
    以及只由确定性节点执行的工具都不进这个集合——模型不该看到一个它调用不了、
    或调用了也进不了主链路的选项。
    """
    return tuple(spec for spec in TOOL_SPECS.values() if spec.agent_selectable)


def to_openai_tools(*, tool_ids: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """模型的 `tools` 参数（只含 Agent 可选择的工具）。

    `tool_ids` 非空时只输出其中**Agent 可选择**的工具。未注册、不可调用或
    非 Agent 可选择的标识会被**明确报出**，不静默丢弃：静默丢弃会让
    「模型本该能用这个工具」变成它自己猜不出来，也会掩盖目录与调用方的不一致。

    注意：Agent 主链路**不**用本函数构造请求（见 `services.audit.flow._openai_tools`），
    它读的是发布期冻结的完整工具定义。本函数供目录展示与尚未冻结定义的新发布使用。
    """
    specs = agent_tool_specs()
    if tool_ids is None:
        return [spec.to_openai_tool() for spec in specs]
    by_id = {spec.tool_id: spec for spec in specs}
    rejected: list[str] = []
    selected: list[ToolSpec] = []
    for tool_id in tool_ids:
        spec = by_id.get(tool_id)
        if spec is None:
            rejected.append(tool_id)
            continue
        selected.append(spec)
    if rejected:
        raise BusinessError(
            "TOOL_NOT_AGENT_SELECTABLE",
            "以下工具不是 Agent 可选择的工具，不能进模型的 tools 参数："
            + "、".join(f"“{item}”" for item in sorted(set(rejected))),
            details={"tools": sorted(set(rejected))},
        )
    return [spec.to_openai_tool() for spec in selected]


def catalog_payload() -> list[dict[str, Any]]:
    """工具目录的对外结构：全部工具（含不可调用的与节点级实现的），按标识排序。"""
    return [spec.to_payload() for spec in TOOL_SPECS.values()]


def allowed_tools_of(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本次发布允许 Agent 调用的工具范围（含**完整冻结定义**）。

    范围由**已发布规则实际引用的能力**与**规则所属节点**共同决定，
    不是把整个目录倒给模型。一条规则引用了 `DATE_COMPARE` 且挂在内部校验节点，
    那么这次审核就允许在该节点调用日期比较工具。

    返回每项含 `tool_id` / `node_code` / `rule_codes` / `rule_keys` / `definition`：

    - `definition` 是发布期冻结的完整工具定义（函数名、描述、参数与输出 Schema、
      handler 引用、目录版本与内容指纹、策略版本），恢复执行时模型看到的必须是它，
      而不是用当前目录现场重新生成的那一份；
    - `tool_id` / `node_code` / `rule_keys` 是「模型能用哪些工具」与
      「这些工具服务于哪条规则」的答案。
      `rule_codes` 保留业务编号，供阅读及旧版本任务使用；多材料分支由 `rule_keys` 区分。

    只有**Agent 可选择**的工具进范围：节点级实现的工具（材料齐全性、文件质量子项）
    即使已实现、有 handler，也没有模型调用入口，列进来会让策略看起来比实际宽，
    而后端 `_partition_rules` 又不会把它们当 Agent 规则——两处不一致。
    """
    allowed: dict[tuple[str, str], dict[str, list[str]]] = {}
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        capability_id = capabilities.capability_id_of_rule(rule)
        spec = tool_spec(capability_id)
        if spec is None or not spec.agent_selectable:
            continue
        node_code = rule.get("node")
        if not isinstance(node_code, str) or not node_code:
            continue
        key = (spec.tool_id, node_code)
        rule_code = rule.get("rule_code") or rule.get("rule_key")
        rule_key = rule.get("rule_key")
        if isinstance(rule_code, str) and rule_code and isinstance(rule_key, str) and rule_key:
            scope = allowed.setdefault(key, {"rule_codes": [], "rule_keys": []})
            scope["rule_codes"].append(rule_code)
            scope["rule_keys"].append(rule_key)
    return [
        {
            "tool_id": tool_id,
            "node_code": node_code,
            "rule_codes": sorted(set(scope["rule_codes"])),
            "rule_keys": sorted(set(scope["rule_keys"])),
            "definition": tool_spec(tool_id).to_definition(),
        }
        for (tool_id, node_code), scope in sorted(allowed.items())
    ]


def catalog_fingerprint() -> str:
    """工具目录的内容指纹：随已发布配置冻结，用于对账「模型当时看到的是哪一版工具」。

    目录内容变化（新增工具、参数 Schema 调整、实现状态变化）都会改变它，
    因此它不能只写版本号——版本号由人维护，指纹由内容决定。
    """
    import hashlib
    import json

    canonical = json.dumps(catalog_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
