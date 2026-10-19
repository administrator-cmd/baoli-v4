"""真实工具的接口参数检查、输入解析和调用，不维护快照指纹或独立账本。"""

from __future__ import annotations
import importlib
import logging
from typing import Any
from langchain_core.tools import BaseTool
from ....domain import tool_catalog
from ....domain.tool_context import ToolExecutionContext
from ....common.exceptions import BusinessError
from jsonschema import Draft202012Validator, SchemaError, ValidationError


logger = logging.getLogger(__name__)


_SCHEMA_VALIDATORS: dict[int, Draft202012Validator] = {}


def _schema_error(message: str, *, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(
        "TOOL_ARGUMENTS_INVALID",
        message,
        details=details or {},
    )


def _validate_against_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    try:
        _SCHEMA_VALIDATORS[id(schema)].validate(value)
    except ValidationError as exc:
        location = path + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in exc.absolute_path)
        raise _schema_error(f"参数 {location} 不符合工具接口：{exc.message}",
                            details={"path": location, "keyword": exc.validator}) from exc


def _assert_spec_shape(spec: tool_catalog.ToolSpec) -> None:
    for schema in (spec.input_schema, spec.output_schema):
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise BusinessError("TOOL_SCHEMA_INVALID", f"工具“{spec.tool_id}”接口结构有误：{exc.message}",
                                status_code=500) from exc
        _SCHEMA_VALIDATORS[id(schema)] = Draft202012Validator(schema)


def _assert_scene_scope(spec: tool_catalog.ToolSpec, scene_code: str | None) -> None:
    """仅遵守工具明确声明的场景权限，不根据业务节点推导权限。"""
    if spec.allowed_scenes is not None and scene_code is not None and scene_code not in spec.allowed_scenes:
        raise BusinessError("TOOL_SCENE_NOT_ALLOWED", f"工具“{spec.label}”不适用于当前场景",
                            details={"tool_id": spec.tool_id, "scene_code": scene_code})


def resolve(
    tool_id: str | None,
    version: str | None = None,
    *,
    context: ToolExecutionContext | None = None,
    scene_type: str | None = None,
    node_code: str | None = None,
) -> tool_catalog.ToolSpec:
    """按标识取可调用的工具，并逐项校验范围。

    检查工具是否存在、调用方明确指定的实现版本、实际实现和场景范围。

    错误码：`TOOL_NOT_REGISTERED`（标识不在目录中）、`TOOL_VERSION_MISMATCH`（版本不一致）、
    `TOOL_NOT_PUBLISHED`（工具存在但当前不可调用：能力未实现，或只有节点级实现）、
    `TOOL_SCENE_NOT_ALLOWED`（超出工具明确声明的场景范围）。

    `context` 提供时，场景与节点取自它；显式参数优先（便于尚未建任务的调用方使用）。
    """
    spec = tool_catalog.tool_spec(tool_id)
    if spec is None:
        raise BusinessError(
            "TOOL_NOT_REGISTERED",
            f"工具“{tool_id}”不在工具目录中，无法执行",
            details={"tool_id": tool_id},
        )
    if version is not None and version != spec.version:
        raise BusinessError(
            "TOOL_VERSION_MISMATCH",
            f"工具“{spec.tool_id}”的版本与调用要求不一致",
            details={"tool_id": spec.tool_id, "expected": spec.version, "requested": version},
        )
    if not spec.implemented:
        raise BusinessError(
            "TOOL_NOT_PUBLISHED",
            f"工具“{spec.label}”当前不可调用：{spec.unimplemented_reason}",
            details={"tool_id": spec.tool_id},
        )
    effective_scene = scene_type if scene_type is not None else (context.scene_code if context else None)
    _assert_scene_scope(spec, effective_scene)
    return spec


def validate_arguments(spec: tool_catalog.ToolSpec, arguments: Any) -> dict[str, Any]:
    """按工具的 `input_schema` 校验模型给出的参数。

    模型给出的字符串在这里被拒绝：`inputs` 只接受字段/材料的身份引用，
    金额与日期一律由调用方从当前任务事实解析后传入，不由模型抄写。
    """
    if not isinstance(arguments, dict):
        raise _schema_error(
            f"工具“{spec.label}”的调用参数必须是对象",
            details={"tool_id": spec.tool_id, "type": type(arguments).__name__},
        )
    _validate_against_schema(arguments, spec.input_schema, path="arguments")
    return arguments


def _load_handler(spec: tool_catalog.ToolSpec) -> BaseTool:
    """解析 `handler_ref`，取得标准 `@tool` 创建的工具对象。

    引用解析失败（模块不存在、函数改名、算子未注册）时明确报错——
    这类失败说明工具目录与实现已经漂移，必须当场暴露，不能拖到真实任务上。
    """
    ref = spec.handler_ref
    if not ref:
        # 只有 implemented=True 的工具才会走到这里，因此这里为空说明目录自相矛盾。
        raise BusinessError(
            "TOOL_HANDLER_MISSING",
            f"工具“{spec.label}”标记为可调用但没有 handler 引用，工具目录与实现不一致",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    try:
        module = importlib.import_module(ref["module"])
        if "operator" in ref:
            handler = module.REGISTRY[ref["operator"]]
        else:
            handler = getattr(module, ref["function"])
    except (ImportError, AttributeError, KeyError) as exc:
        raise BusinessError(
            "TOOL_HANDLER_MISSING",
            f"工具“{spec.label}”的执行实现无法解析：{ref.get('module')}.{ref.get('function')}",
            status_code=500,
            details={"tool_id": spec.tool_id, "module": ref.get("module"), "function": ref.get("function")},
        ) from exc
    return handler


def _effective_parameters(
    spec: tool_catalog.ToolSpec,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """把 `handler_ref` 里固定的参数并入调用参数。

    文件质量子项的检查项目由工具身份固定（`handler_ref["check_item"]`），
    模型不能自己选——选错检查项目就是调了另一个工具。这里由工具目录注入，
    而不是让模型填，避免模型用一个检查项目的标准去跑另一个项目的判定。
    """
    ref = spec.handler_ref or {}
    fixed = ref.get("check_item")
    if fixed is None:
        return parameters
    return {**parameters, "check_item": fixed}


def _identity_of(item: dict[str, Any]) -> tuple[str, str | None]:
    return (str(item.get("field_key") or ""), item.get("material_key"))


def _assert_material_scope(
    spec: tool_catalog.ToolSpec,
    requested: list[Any],
    context: ToolExecutionContext | None,
) -> None:
    """模型请求的输入不得超出本次限定的材料范围。

    `context.material_refs` 为空表示本次未限定范围，此时不做这项校验——
    范围由调用方决定，工具层不默认放开也不默认收紧。
    """
    if context is None:
        return
    scope = context.scoped_materials
    if not scope:
        return
    outside = sorted(
        {
            str(item.get("material_key"))
            for item in requested
            if isinstance(item, dict) and item.get("material_key") and str(item["material_key"]) not in scope
        }
    )
    if outside:
        raise BusinessError(
            "TOOL_MATERIAL_OUT_OF_SCOPE",
            f"工具“{spec.label}”的输入超出本次材料范围，本次调用未执行",
            details={"tool_id": spec.tool_id, "outside": outside, "allowed": sorted(scope)},
        )


def _merge_runtime(
    spec: tool_catalog.ToolSpec,
    parameters: dict[str, Any],
    runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    """把调用方注入的运行期状态并入 handler 参数。

    有些工具的输入不只是字段事实——风险规则要先知道各来源检查在当前有效执行批次里的真实结论
    （`source_results`，含取值与判断依据）与已发布配置里的成立条件（`risk_condition`）。这些值不能由模型提供
    （它们是数据库里的真实结果与冻结的配置事实，模型自己写一个状态就等于伪造来源结论），也不能
    由工具自己查（`domain` 层不碰数据库，handler 也不持有会话）。

    因此模型只给身份引用（`source_rule_codes`），调用方按身份解析后经 `runtime` 注入。缺少必需
    键或出现未声明的键都明确报错——静默塞进去会让「谁提供了这个值」变得无法追溯。
    """
    provided = dict(runtime or {})
    if not provided:
        if spec.runtime_keys:
            raise BusinessError(
                "TOOL_RUNTIME_MISSING",
                f"工具“{spec.label}”需要调用方提供运行期状态（{'、'.join(spec.runtime_keys)}），本次未提供",
                details={"tool_id": spec.tool_id, "required": list(spec.runtime_keys)},
            )
        return parameters
    unexpected = sorted(set(provided) - set(spec.runtime_keys))
    if unexpected:
        raise BusinessError(
            "TOOL_RUNTIME_UNEXPECTED",
            f"工具“{spec.label}”不接受这些运行期状态：{'、'.join(unexpected)}",
            details={"tool_id": spec.tool_id, "unexpected": unexpected, "accepted": list(spec.runtime_keys)},
        )
    return {**parameters, **provided}


def invoke(
    tool_id: str,
    arguments: Any,
    *,
    inputs: list[dict[str, Any]],
    runtime: dict[str, Any] | None = None,
    context: ToolExecutionContext | None = None,
    version: str | None = None,
    scene_type: str | None = None,
    node_code: str | None = None,
) -> dict[str, Any]:
    """执行一次工具调用：校验后调真实 handler，返回它的结果。

    `arguments` 是模型给出的调用参数（只含身份引用与规则配置），先过 `input_schema` 校验；
    `inputs` 是调用方按那些身份解析出的真实事实，由本模块核对两者逐项对应后交给 handler；
    `runtime` 是需要额外运行期状态的工具（如风险规则）由调用方注入的真实值；`context` 是本次
    调用的运行期上下文（任务、规则实例、材料范围和数据库会话）。

    取值不从模型参数里取：金额、日期、文件内容与来源检查结论一律来自当前任务事实与已发布配置，
    避免模型抄错一个数字却算对了另一道题。

    返回的 dict 形状由工具的 `output_schema` 描述（至少含 `status` 与 `message`）。handler
    抛出的 `BusinessError` 原样上抛——它是真实的业务错误，不能在这里被吞掉或改写成空结果。
    """
    spec = resolve(tool_id, version, context=context, scene_type=scene_type, node_code=node_code)
    payload = validate_arguments(spec, arguments)
    handler = _load_handler(spec)
    requested = payload["inputs"]
    parameters = payload["parameters"]
    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
        raise BusinessError(
            "TOOL_INPUT_INVALID",
            f"工具“{spec.label}”的输入事实形状无效",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    by_identity = {_identity_of(item): item for item in inputs}
    selected_inputs = []
    for reference in requested:
        identity = _identity_of(reference)
        matches = [item for key, item in by_identity.items()
                   if key[0] == identity[0] and (identity[1] is None or key[1] == identity[1])]
        if len(matches) != 1:
            raise BusinessError("TOOL_INPUT_INVALID", "工具引用的字段事实不存在或不能唯一定位", details={"reference": reference})
        selected_inputs.append(matches[0])
    inputs = selected_inputs
    _assert_material_scope(spec, requested, context)
    effective = _merge_runtime(spec, _effective_parameters(spec, parameters), runtime)
    # 经标准工具接口执行，传入已解析的真实事实；context 是系统注入参数。
    execution_arguments: dict[str, Any] = {"parameters": effective, "inputs": inputs}
    if (spec.handler_ref or {}).get("context") == "true":
        execution_arguments["context"] = context
    result = handler.invoke(execution_arguments)
    if not isinstance(result, dict) or "status" not in result:
        raise BusinessError(
            "TOOL_RESULT_INVALID",
            f"工具“{spec.label}”的执行实现返回了不符合输出契约的结果",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    validate_result(spec, result)
    # 展示依据来自本次真正解析的输入，不让模型抄写取值或让页面猜输入顺序。
    if isinstance(result.get("comparison"), dict):
        comparison = dict(result["comparison"])
        for side in ("left", "right"):
            index = comparison.get(f"{side}_index")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(inputs):
                source = inputs[index]
                comparison[f"{side}_label"] = " · ".join(str(source[key]) for key in ("material_name", "field_name") if source.get(key))
                comparison[f"{side}_raw_value"] = source.get("raw_value")
        if comparison.get("addends"):
            comparison["addend_labels"] = [" · ".join(str(source[key]) for key in ("material_name", "field_name") if source.get(key)) for source in inputs[1:]]
        result = {**result, "comparison": comparison, "input_snapshot": inputs}
    return result


def validate_result(spec: tool_catalog.ToolSpec, result: dict[str, Any]) -> dict[str, Any]:
    """按工具的 `output_schema` 校验真实执行结果。

    输入已经过 Schema 校验，输出同样要过：结论状态只能取能力目录声明的枚举、
    必填输出键必须齐备。未实现的输出被如实判为「工具结果不符合契约」——
    工具实现返回了目录里没声明的状态，说明工具目录与实现已经漂移，
    这与「模型传错参数」是两类问题，因此错误码分开（`TOOL_RESULT_INVALID`）。
    """
    try:
        _validate_against_schema(result, spec.output_schema, path="result")
    except BusinessError as exc:
        raise BusinessError(
            "TOOL_RESULT_INVALID",
            f"工具“{spec.label}”的执行结果不符合输出契约：{exc.message}",
            status_code=500,
            details={"tool_id": spec.tool_id, "reason": exc.details},
        ) from exc
    return result


for _spec in tool_catalog.TOOL_SPECS.values():
    _assert_spec_shape(_spec)
