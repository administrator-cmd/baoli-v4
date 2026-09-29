"""工具执行服务：校验工具、参数与权限范围，然后调用真实 handler。

这个模块是「模型请求调用工具」与「真实 Python 实现」之间的唯一接缝。
模型给出的字符串**不直接**进 handler：每次调用都要先过下面这条链，
任一项不成立就抛明确错误，不落到别的分支、不返回空结果、不静默降级：

    解析调用 → 校验 tool_id 与版本 → 校验场景/节点范围
      → 校验参数 Schema → 解析 handler 引用 → 执行真实 handler

为什么 handler 引用在这里解析
-----------------------------
`domain/tool_catalog.py` 只声明 `handler_ref`（模块名 + 函数名 + 算子码），
不导入 handler：`domain` 层不允许依赖 `operators`，否则领域层会被执行层拉进去。
真实解析发生在本模块——它是 services 层，导入 `operators` 是允许的方向。

未注册任何实现时明确报错，**不**退回默认 handler 或空结果。
这与 `services/audit/document_ports.py` 的端口注册是同一手法。

本轮边界（重要）
----------------
本模块**不做持久化**，也**不做幂等去重**：那需要 `tool_call_id` 记录表，
属于阶段五。因此本轮的 `invoke` 是「校验后执行真实 handler」，
重复调用会重复执行——调用方若要避免重复，必须自己保证不重复请求
（工具的 `idempotent=True` 说明同一输入得到同一结果，但不代表调用不会被执行两次）。
"""

from __future__ import annotations

import importlib
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain import capabilities, tool_catalog
from ..domain.tool_context import ToolExecutionContext
from ..errors import BusinessError


logger = logging.getLogger(__name__)


# 参数 Schema 里被支持的 JSON Schema 关键字。**本模块只实现这个子集**：
# `jsonschema` 不在项目依赖里（`requirements.txt` 与 conda `dd` 环境都没有），
# 而引入一个新依赖只为校验这几项并不划算。子集之外的关键字**显式报错**，
# 不静默忽略——静默忽略会让「schema 里写了约束但没生效」变成看不见的漏洞。
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "minimum",
    }
)

# 支持的 JSON 类型名 → Python 类型判据。
_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    # 布尔必须先于 number 判断：Python 的 bool 是 int 的子类，
    # 不排除它会让 True 通过 number 校验。
    "boolean": lambda value: isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "null": lambda value: value is None,
}


def _schema_error(message: str, *, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(
        "TOOL_ARGUMENTS_INVALID",
        message,
        details=details or {},
    )


def _assert_supported_schema(schema: Any, *, path: str) -> None:
    """Schema 自身只能用受支持的子集；出现别的关键字当即报错。

    这条校验针对的是**目录里的 schema**（本系统自己写的），因此失败说明
    工具目录用了未实现的约束——那是必须修的代码问题，不是模型调用错误。
    """
    if not isinstance(schema, dict):
        raise BusinessError(
            "TOOL_SCHEMA_UNSUPPORTED",
            f"工具目录中 {path} 的 Schema 不是对象，无法用于参数校验",
            status_code=500,
            details={"path": path},
        )
    unsupported = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYS)
    if unsupported:
        raise BusinessError(
            "TOOL_SCHEMA_UNSUPPORTED",
            f"工具目录中 {path} 使用了尚未实现的 JSON Schema 关键字：{'、'.join(unsupported)}",
            status_code=500,
            details={"path": path, "unsupported": unsupported},
        )
    if "type" in schema:
        declared = schema["type"]
        if not isinstance(declared, str) or declared not in _TYPE_CHECKS:
            raise BusinessError(
                "TOOL_SCHEMA_UNSUPPORTED",
                f"工具目录中 {path} 声明了不受支持的 JSON 类型",
                status_code=500,
                details={"path": path, "type": declared},
            )
    for key, value in (schema.get("properties") or {}).items():
        _assert_supported_schema(value, path=f"{path}.properties.{key}")
    if "items" in schema:
        _assert_supported_schema(schema["items"], path=f"{path}.items")


def _validate_against_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """按受支持的 Schema 子集校验一个值。失败抛 TOOL_ARGUMENTS_INVALID。"""
    if "enum" in schema:
        allowed = schema["enum"]
        if value not in allowed:
            raise _schema_error(
                f"参数 {path} 的取值不在允许范围内",
                details={"path": path, "allowed": list(allowed)},
            )
    declared = schema.get("type")
    if declared is not None:
        check = _TYPE_CHECKS[declared]
        if not check(value):
            raise _schema_error(
                f"参数 {path} 的类型应为 {declared}",
                details={"path": path, "expected": declared},
            )
    if declared == "string":
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise _schema_error(
                f"参数 {path} 不能为空",
                details={"path": path, "minLength": minimum},
            )
    if declared in {"number", "integer"}:
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise _schema_error(
                f"参数 {path} 不能小于 {minimum}",
                details={"path": path, "minimum": minimum},
            )
    if declared == "array":
        items = schema.get("items")
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise _schema_error(
                f"参数 {path} 至少需要 {minimum} 项",
                details={"path": path, "minItems": minimum, "actual": len(value)},
            )
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            raise _schema_error(
                f"参数 {path} 最多允许 {maximum} 项",
                details={"path": path, "maxItems": maximum, "actual": len(value)},
            )
        if items is not None:
            for index, entry in enumerate(value):
                _validate_against_schema(entry, items, path=f"{path}[{index}]")
    if declared == "object":
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                raise _schema_error(
                    f"参数 {path} 缺少必填项 {key}",
                    details={"path": path, "missing": key},
                )
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise _schema_error(
                    f"参数 {path} 包含不允许的项：{'、'.join(unexpected)}",
                    details={"path": path, "unexpected": unexpected},
                )
        for key, entry in value.items():
            if key in properties:
                _validate_against_schema(entry, properties[key], path=f"{path}.{key}")


def _assert_spec_shape(spec: tool_catalog.ToolSpec) -> None:
    """工具目录本身的形状校验：schema 用了未实现的子集时立刻暴露。

    只在首次取到某项工具时校验，不缓存结果——目录是常量，重复校验的代价可忽略。
    """
    _assert_supported_schema(spec.input_schema, path=f"{spec.tool_id}.input_schema")
    _assert_supported_schema(spec.output_schema, path=f"{spec.tool_id}.output_schema")


def _scene_node_types(scene_code: str) -> frozenset[str] | None:
    """一个场景**实际配置**的节点能力类型。

    场景用哪些节点由用户配置（`scenario_draft_node`），不由 Profile 声明——
    统一模板协议定义的是系统能力全集，一个场景只用其中一部分。
    这里读该场景草稿里的节点实例类型；场景不存在或没有配置节点时返回 None，
    调用方据此**跳过**场景校验而不是当成「不允许」——把配置未就绪读成「越权」
    会让一次正常的调用被误拒。
    """
    from ..models import ScenarioDraft, ScenarioDraftNode
    from ..database import SessionLocal

    with SessionLocal() as session:
        draft = session.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scene_code))
        if draft is None:
            return None
        types = {
            str(item.node_type)
            for item in session.scalars(
                select(ScenarioDraftNode).where(ScenarioDraftNode.draft_id == draft.id)
            ).all()
            if item.node_type
        }
    return frozenset(types) if types else None


def _assert_scene_scope(
    spec: tool_catalog.ToolSpec,
    scene_code: str | None,
    *,
    declared_nodes: frozenset[str] | None = None,
) -> None:
    """工具是否适用于该场景。

    `allowed_scenes` 显式声明时按它判断（精确名单优先）。

    未声明时**不是「无约束」**，而是按已声明的两件事实推导：
    工具的 `allowed_nodes` 与该场景**实际配置的节点类型**是否相交。
    一个工具只能挂在某几类节点上，而某个场景根本没有配置那类节点，
    那么这个工具在该场景里没有位置——这是确定性结论，不是猜测。

    例如日期比较只挂「文件内部校验 / 文件交叉校验」，而某个场景没有配置这两类节点，
    因此该场景不能调用日期比较工具。
    """
    if scene_code is None:
        return
    if spec.allowed_scenes is not None:
        if scene_code not in spec.allowed_scenes:
            raise BusinessError(
                "TOOL_SCENE_NOT_ALLOWED",
                f"工具“{spec.label}”不适用于当前场景",
                details={"tool_id": spec.tool_id, "scene_code": scene_code},
            )
        return
    if not spec.allowed_nodes:
        # 工具本身不限定节点，场景层面因此没有可推导的约束。
        return
    declared = declared_nodes if declared_nodes is not None else _scene_node_types(scene_code)
    if declared is None:
        # 场景 Profile 不可读：配置问题，不当作越权。
        return
    if not (declared & spec.allowed_nodes):
        raise BusinessError(
            "TOOL_SCENE_NOT_ALLOWED",
            f"工具“{spec.label}”不适用于当前场景：该场景没有声明它可挂载的节点类型",
            details={
                "tool_id": spec.tool_id,
                "scene_code": scene_code,
                "tool_nodes": sorted(spec.allowed_nodes),
                "scene_nodes": sorted(declared),
            },
        )


def _resolve_policy_version(spec: tool_catalog.ToolSpec, context: ToolExecutionContext | None) -> None:
    """工具策略版本核对。

    任务创建时冻结了当时那一版**工具策略**（`tool_policy_version`）。策略变化后
    （放开或收紧工具范围、改变越权判定口径），用新策略去解释旧任务的工具调用会让
    历史结论无法复现，因此不一致即拒绝执行。

    比较对象只能是 `TOOL_POLICY_VERSION`。**不能**拿工具目录版本
    （`TOOL_CATALOG_VERSION`）来比：目录可以因为与策略无关的原因升级（新增工具、
    调整描述），拿它比会让既有任务的工具调用被整批拒掉；而真正放宽策略时又拦不住
    按旧策略冻结的任务。目录身份是另一个维度，由 `_assert_catalog_identity` 负责。

    `None` 表示「本次未声明策略版本」：旧任务与尚未冻结工具策略的调用属于这种情况。
    这里**不**把它当成「按当前版本处理」——那等于让旧任务静默改用新语义。
    """
    if context is None or context.tool_policy_version is None:
        return
    if context.tool_policy_version != tool_catalog.TOOL_POLICY_VERSION:
        raise BusinessError(
            "TOOL_POLICY_VERSION_MISMATCH",
            f"工具“{spec.label}”冻结的工具策略版本与当前策略不一致，本次调用未执行；"
            "策略变化后旧任务不能按新策略重新解释",
            details={
                "tool_id": spec.tool_id,
                "frozen_policy_version": context.tool_policy_version,
                "current_policy_version": tool_catalog.TOOL_POLICY_VERSION,
            },
        )


def _assert_catalog_identity(spec: tool_catalog.ToolSpec, context: ToolExecutionContext | None) -> None:
    """工具目录身份核对：**需要真正执行**的调用必须能证明实现与冻结目录兼容。

    当前实现没有对 handler 做版本化（`handler_ref` 指向的是当前代码里的函数），
    因此当冻结目录与当前目录不一致时，**无法证明**这次执行与冻结时的语义一致。
    这种情形必须显式报版本不兼容，不能静默按当前实现执行：

    - 已成功的旧调用不受影响：账本在真正执行之前就按 `SUCCEEDED` 复用结果，
      根本走不到这里（见 `services/agent_tool_ledger.begin_call`）；
    - 冻结身份缺失（旧任务没有这一列）同样显式报错，不按「无约束」处理。

    错误码与错误信息都指明是**目录**不一致（`TOOL_CATALOG_*`），与策略不一致
    （`TOOL_POLICY_VERSION_MISMATCH`）分开，读取方据此知道该改哪里。
    """
    if context is None:
        return
    frozen_version = context.tool_catalog_version
    frozen_hash = context.tool_catalog_hash
    if not frozen_version or not frozen_hash:
        raise BusinessError(
            "TOOL_CATALOG_IDENTITY_MISSING",
            f"工具“{spec.label}”缺少冻结的工具目录身份，无法确认本次执行与冻结时语义一致，本次调用未执行",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    current_hash = tool_catalog.catalog_fingerprint()
    if frozen_version != tool_catalog.TOOL_CATALOG_VERSION:
        raise BusinessError(
            "TOOL_CATALOG_VERSION_MISMATCH",
            f"工具“{spec.label}”冻结的工具目录版本与当前目录不一致，当前实现无法证明与冻结版本兼容，"
            "本次调用未重新执行；如需按新目录执行，请重新发布场景配置",
            details={
                "tool_id": spec.tool_id,
                "frozen_catalog_version": frozen_version,
                "current_catalog_version": tool_catalog.TOOL_CATALOG_VERSION,
            },
        )
    if frozen_hash != current_hash:
        raise BusinessError(
            "TOOL_CATALOG_HASH_MISMATCH",
            f"工具“{spec.label}”冻结的工具目录内容指纹与当前目录不一致（版本号没变但目录内容已变），"
            "当前实现无法证明与冻结版本兼容，本次调用未重新执行",
            details={
                "tool_id": spec.tool_id,
                "frozen_catalog_hash": frozen_hash,
                "current_catalog_hash": current_hash,
            },
        )


def resolve(
    tool_id: str | None,
    version: str | None = None,
    *,
    context: ToolExecutionContext | None = None,
    scene_type: str | None = None,
    node_code: str | None = None,
) -> tool_catalog.ToolSpec:
    """按标识取**可调用**的工具，并逐项校验范围。

    校验顺序是刻意的：先看工具是否存在，再看版本与实现状态，最后看场景、节点与
    策略版本。顺序反了会把「工具不存在」报成「场景不允许」，让调用方去改一个无关的地方。

    - `TOOL_NOT_REGISTERED`：标识不在目录中；
    - `TOOL_VERSION_MISMATCH`：调用方要求的版本与目录版本不一致；
    - `TOOL_NOT_PUBLISHED`：工具存在但当前不可调用（能力未实现，或只有节点级实现）；
    - `TOOL_SCENE_NOT_ALLOWED` / `TOOL_NODE_NOT_ALLOWED`：超出允许范围；
    - `TOOL_POLICY_VERSION_MISMATCH`：任务冻结的工具策略版本与当前目录不一致。

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
    effective_node = node_code if node_code is not None else (context.node_code if context else None)
    manifest = None
    if context is not None and context.task_id is not None and context.database_session is not None:
        from ..models import AuditTask

        task = context.database_session.get(AuditTask, context.task_id)
        if task is None:
            raise BusinessError("TOOL_TASK_MISSING", "工具调用关联的审核任务不存在", status_code=500)
        manifest = task.manifest_json
    node_types = None
    if isinstance(manifest, dict):
        node_types = {
            str(item["node_code"]): str(item.get("node_type") or item["node_code"])
            for item in manifest.get("nodes", [])
            if isinstance(item, dict) and item.get("node_code")
        }
    _assert_scene_scope(
        spec,
        effective_scene,
        declared_nodes=frozenset(node_types.values()) if node_types is not None else None,
    )
    if effective_node is not None and node_types is not None and effective_node not in node_types:
        raise BusinessError(
            "TOOL_NODE_NOT_ALLOWED",
            "工具调用的节点不在任务冻结的流程中",
            details={"tool_id": spec.tool_id, "node_code": effective_node},
        )
    effective_node_type = (
        node_types[effective_node]
        if effective_node is not None and node_types is not None
        else effective_node
    )
    if spec.allowed_nodes and effective_node_type is not None and effective_node_type not in spec.allowed_nodes:
        raise BusinessError(
            "TOOL_NODE_NOT_ALLOWED",
            f"工具“{spec.label}”不能挂在这个节点上",
            details={"tool_id": spec.tool_id, "node_code": effective_node, "node_type": effective_node_type, "allowed": sorted(spec.allowed_nodes)},
        )
    _resolve_policy_version(spec, context)
    _assert_catalog_identity(spec, context)
    _assert_spec_shape(spec)
    return spec


def validate_arguments(spec: tool_catalog.ToolSpec, arguments: Any) -> dict[str, Any]:
    """按工具的 `input_schema` 校验模型给出的参数。

    模型给出的字符串在这里被拒绝：`inputs` 只接受字段/材料的**身份引用**，
    金额与日期一律由调用方从事实快照解析后传入，不由模型抄写。
    """
    if not isinstance(arguments, dict):
        raise _schema_error(
            f"工具“{spec.label}”的调用参数必须是对象",
            details={"tool_id": spec.tool_id, "type": type(arguments).__name__},
        )
    _validate_against_schema(arguments, spec.input_schema, path="arguments")
    return arguments


def _load_handler(spec: tool_catalog.ToolSpec) -> Any:
    """解析 `handler_ref` 并取出真实 handler。

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
        handler = getattr(module, ref["function"])
    except (ImportError, AttributeError, KeyError) as exc:
        raise BusinessError(
            "TOOL_HANDLER_MISSING",
            f"工具“{spec.label}”的执行实现无法解析：{ref.get('module')}.{ref.get('function')}",
            status_code=500,
            details={"tool_id": spec.tool_id, "module": ref.get("module"), "function": ref.get("function")},
        ) from exc
    return handler


def _handler_arguments(
    spec: tool_catalog.ToolSpec,
    parameters: dict[str, Any],
) -> tuple[Any, ...]:
    """按 handler 的真实签名组装位置参数。

    两种 handler 形状：

    - 算子工具：`execute_operator(operator, parameters, inputs)`——算子码是第一个参数，
      由 `handler_ref` 携带，不靠猜；
    - 上下文 adapter：`adapter(parameters, inputs, *, context)`——只有两个位置参数，
      会话等运行期依赖经关键字传（见 `invoke`）。
    """
    ref = spec.handler_ref or {}
    operator = ref.get("operator")
    if operator is None:
        return (parameters,)
    return (operator, parameters)


def _effective_parameters(
    spec: tool_catalog.ToolSpec,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """把 `handler_ref` 里固定的参数并入调用参数。

    文件质量子项的检查项目由**工具身份**固定（`handler_ref["check_item"]`），
    模型不能自己选——选错检查项目就是调了另一个工具。这里由工具目录注入，
    而不是让模型填，避免模型用一个检查项目的标准去跑另一个项目的判定。
    """
    ref = spec.handler_ref or {}
    fixed = ref.get("check_item")
    if fixed is None:
        return parameters
    return {**parameters, "check_item": fixed}


def _same_published_value(key: str, requested: Any, published: Any) -> bool:
    if isinstance(requested, bool) or isinstance(published, bool):
        return type(requested) is type(published) and requested == published
    if key == "threshold" and isinstance(requested, (int, float, str)) and isinstance(published, (int, float, str)):
        try:
            return Decimal(str(requested)) == Decimal(str(published))
        except InvalidOperation:
            pass
    return requested == published


def _bind_published_parameters(
    spec: tool_catalog.ToolSpec,
    requested: dict[str, Any],
    published: dict[str, Any] | None,
) -> dict[str, Any]:
    if published is None:
        raise BusinessError(
            "TOOL_RULE_PARAMETERS_MISSING",
            f"工具“{spec.label}”缺少任务冻结的规则参数，本次调用未执行",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    if set(requested) != set(published) or any(
        not _same_published_value(key, requested[key], published[key]) for key in requested
    ):
        raise BusinessError(
            "TOOL_RULE_PARAMETERS_MISMATCH",
            f"工具“{spec.label}”的调用参数与已发布规则不一致，本次调用未执行",
            details={"tool_id": spec.tool_id, "requested": requested, "published": published},
        )
    return dict(published)


def _identity_of(item: dict[str, Any]) -> tuple[str, str | None]:
    return (str(item.get("field_key") or ""), item.get("material_key"))


def _assert_identity_match(
    spec: tool_catalog.ToolSpec,
    requested: list[Any],
    resolved: list[dict[str, Any]],
) -> None:
    """模型给的**身份引用**必须与调用方解析出的**事实**逐项对应。

    这是「模型给身份、系统解析取值」这条边界的落点：模型不得凭空多给一个输入，
    也不能少给——顺序与身份都要对上，否则算的是另一道题。
    事实里的取值（status / normalized_value / unit…）由调用方从快照取，
    模型看不到也不需要提供，因此它们**不**出现在 input_schema 里。
    """
    wanted = [
        (str(item.get("field_key") or ""), item.get("material_key"))
        for item in requested
        if isinstance(item, dict)
    ]
    actual = [_identity_of(item) for item in resolved]
    if wanted != actual:
        raise BusinessError(
            "TOOL_INPUT_MISMATCH",
            f"工具“{spec.label}”的输入身份与系统解析出的事实不一致，本次调用未执行",
            details={
                "tool_id": spec.tool_id,
                "requested": [f"{key}@{material or '-'}" for key, material in wanted],
                "resolved": [f"{key}@{material or '-'}" for key, material in actual],
            },
        )


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

    为什么需要这一层：有些工具的输入不只是字段事实——风险规则要先知道
    **各来源检查在当前有效执行批次里的真实结论**（`source_statuses`）与
    **已发布配置里的成立条件**（`risk_condition`）。这些值：

    - 不能由模型提供：它们是数据库里的真实结果与冻结的配置事实，
      模型自己写一个状态就等于让模型伪造来源结论；
    - 不能由工具自己查：`domain` 层不碰数据库，handler 也不持有会话。

    因此模型只给**身份引用**（`source_rule_codes`），调用方按身份解析后经
    `runtime` 注入。缺少必需键或出现未声明的键都明确报错——
    静默塞进去会让「谁提供了这个值」变得无法追溯。
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
    published_parameters: dict[str, Any] | None = None,
    published_tool_id: str | None = None,
) -> dict[str, Any]:
    """执行一次工具调用：校验后调真实 handler，返回它的结果。

    `arguments` 是模型给出的调用参数（只含身份引用与规则配置），
    它先过 `input_schema` 校验；`inputs` 是调用方按那些身份**解析出的真实事实**，
    由本模块核对两者逐项对应后交给 handler；`runtime` 是需要额外运行期状态的工具
    （如风险规则）由调用方注入的真实值；`context` 是本次调用的运行期信封
    （任务、规则实例、材料范围、事实快照、工具策略版本、数据库会话）。

    取值不从模型参数里取：金额、日期、文件内容与来源检查结论一律来自事实快照
    与已发布配置，避免模型抄错一个数字却算对了另一道题。

    返回的 dict 形状由工具的 `output_schema` 描述（至少含 `status` 与 `message`）。
    handler 抛出的 `BusinessError` 原样上抛：它是真实的业务错误
    （缺输入、参数非法、取值类型不对），不能在这里被吞掉或改写成空结果。
    """
    spec = resolve(tool_id, version, context=context, scene_type=scene_type, node_code=node_code)
    payload = validate_arguments(spec, arguments)
    handler = _load_handler(spec)
    requested = payload.get("inputs")
    if not isinstance(requested, list):
        raise _schema_error(
            f"工具“{spec.label}”的 inputs 必须是数组",
            details={"tool_id": spec.tool_id},
        )
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise _schema_error(
            f"工具“{spec.label}”的 parameters 必须是对象",
            details={"tool_id": spec.tool_id},
        )
    if context is not None and context.caller_identity == "audit_agent":
        if published_tool_id != spec.tool_id:
            raise BusinessError(
                "TOOL_RULE_CAPABILITY_MISMATCH",
                f"工具“{spec.label}”与当前已发布规则的能力不一致，本次调用未执行",
                details={"tool_id": spec.tool_id, "rule_tool_id": published_tool_id},
            )
        parameters = _bind_published_parameters(spec, parameters, published_parameters)
    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
        raise BusinessError(
            "TOOL_INPUT_INVALID",
            f"工具“{spec.label}”的输入事实形状无效",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    _assert_identity_match(spec, requested, inputs)
    _assert_material_scope(spec, requested, context)
    effective = _merge_runtime(spec, _effective_parameters(spec, parameters), runtime)
    # handler 的真实签名由 handler_ref 决定（见 _handler_arguments）：
    # 算子经 execute_operator(operator, parameters, inputs) 分发；
    # 上下文 adapter 额外接收 context（会话、任务、快照、策略版本都在里面）。
    if (spec.handler_ref or {}).get("context") == "true":
        result = handler(*_handler_arguments(spec, effective), inputs, context=context)
    else:
        result = handler(*_handler_arguments(spec, effective), inputs)
    if not isinstance(result, dict) or "status" not in result:
        raise BusinessError(
            "TOOL_RESULT_INVALID",
            f"工具“{spec.label}”的执行实现返回了不符合输出契约的结果",
            status_code=500,
            details={"tool_id": spec.tool_id},
        )
    validate_result(spec, result)
    return result


def validate_result(spec: tool_catalog.ToolSpec, result: dict[str, Any]) -> dict[str, Any]:
    """按工具的 `output_schema` 校验真实执行结果。

    输入已经过 Schema 校验，输出同样要过：结论状态只能取能力目录声明的枚举、
    必填输出键必须齐备。**未实现的输出被如实判为「工具结果不符合契约」**——
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


def tool_definitions(*, tool_ids: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """模型的 `tools` 参数，只包含当前可调用的工具。

    `tool_ids` 非空时按它过滤；其中不可调用或不存在的标识会被**明确报出**，
    不静默丢弃——静默丢弃会让「模型本该能用这个工具」变成它自己猜不出来。
    """
    if tool_ids is not None:
        for tool_id in tool_ids:
            resolve(tool_id)
    return tool_catalog.to_openai_tools(tool_ids=tool_ids)


def catalog_version() -> str:
    """当前工具目录版本，随工具语义变化递增。"""
    return tool_catalog.TOOL_CATALOG_VERSION


def catalog_fingerprint() -> str:
    """工具目录内容的指纹，用于发现「版本号没改但内容变了」的漂移。"""
    return tool_catalog.catalog_fingerprint()


def capability_of_tool(tool_id: str) -> capabilities.Capability | None:
    """工具对应的能力声明。用于把工具结果与能力目录的判定口径对上。"""
    return capabilities.capability_of(tool_id)
