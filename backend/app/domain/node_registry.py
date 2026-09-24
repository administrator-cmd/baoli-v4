"""审核节点注册表：能力类型 → 节点实现。

节点分发与节点清单此前分处两处维护，新增一种节点要在两个地方改，漏一处就是
「能发布、运行时才报节点不受支持」。本模块把这两件事收敛到一处：
**能力类型 → handler** 的映射，`flow._dispatch` 只查表，不再写分支。

为什么 handler 由调用方注入
--------------------------
节点实现（`_material_check`、`_execute_rules` …）在 `services` 层，
而 `manifest` 编译也要知道「有哪些节点类型」（在 `domain` 层）。
让 domain 反向依赖 services 会形成循环，因此：

- 本模块只声明**注册接口与校验**，不导入任何 services；
- 真实 handler 在应用启动时由 `services.audit` 注册进来；
- 未注册的能力类型在分发时明确报错，不落到别的分支、也不静默跳过。

这与 `capabilities.py` 的分工一致：那里声明「能力是什么」，这里声明「谁来执行」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..errors import BusinessError


# 节点能力类型。它描述**这个节点做什么**，与节点实例码（`node_code`）是两件事：
# 同一能力类型在场景里可以有多个实例（例如两张报表各有一个字段抽取节点）。
NODE_TYPE_MATERIAL_CHECK = "MATERIAL_CHECK"
NODE_TYPE_DOCUMENT_PREPARE = "DOCUMENT_PREPARE"
NODE_TYPE_FIELD_EXTRACTION = "FIELD_EXTRACTION"
NODE_TYPE_INTERNAL_VALIDATE = "INTERNAL_VALIDATE"
NODE_TYPE_CROSS_VALIDATE = "CROSS_VALIDATE"
NODE_TYPE_CALCULATION = "CALCULATION"
NODE_TYPE_MANUAL_REVIEW = "MANUAL_REVIEW"
NODE_TYPE_RISK_RATING = "RISK_RATING"
NODE_TYPE_REPORT_OUTPUT = "REPORT_OUTPUT"

# 节点类型 → 业务中文名。页面展示与错误提示共用这一份，不再各写一遍。
NODE_TYPE_LABELS: dict[str, str] = {
    NODE_TYPE_MATERIAL_CHECK: "材料检查",
    NODE_TYPE_DOCUMENT_PREPARE: "文件处理",
    NODE_TYPE_FIELD_EXTRACTION: "字段抽取",
    NODE_TYPE_INTERNAL_VALIDATE: "文件内部校验",
    NODE_TYPE_CROSS_VALIDATE: "文件交叉校验",
    NODE_TYPE_CALCULATION: "计算",
    NODE_TYPE_MANUAL_REVIEW: "人工复核",
    NODE_TYPE_RISK_RATING: "风险评定",
    NODE_TYPE_REPORT_OUTPUT: "报告输出",
}

# 旧场景的固定节点序列。旧 manifest 里 `nodes` 是 8 个固定节点，
# 它们编译成一条线性依赖链，执行语义与旧版本完全一致。
LEGACY_NODE_SEQUENCE: tuple[tuple[str, str], ...] = (
    (NODE_TYPE_MATERIAL_CHECK, "材料检查"),
    (NODE_TYPE_DOCUMENT_PREPARE, "文件处理"),
    (NODE_TYPE_FIELD_EXTRACTION, "字段抽取"),
    (NODE_TYPE_INTERNAL_VALIDATE, "文件内部校验"),
    (NODE_TYPE_CROSS_VALIDATE, "文件交叉校验"),
    (NODE_TYPE_MANUAL_REVIEW, "人工复核"),
    (NODE_TYPE_RISK_RATING, "风险评定"),
    (NODE_TYPE_REPORT_OUTPUT, "报告输出"),
)

# 会走「规则评估 Agent」的节点能力类型。这是**唯一**的声明处：
# `services.audit.flow` 据此决定执行模式，`domain.tool_catalog` 据此判定一个工具
# 是否属于「Agent 可选择」——两处必须同源，否则会出现「目录说模型能调、
# 实际没有调用入口」这种前后不一致。
#
# 只有**按规则执行**的校验节点在范围内。材料检查、文件处理、字段抽取、计算与
# 报告输出仍由各自的确定性实现完成：它们的能力（抽取、渲染、外部服务调用）
# 不是 Agent 可选工具，也不能被模型改写成别的方式。把它们并进来需要先完成
# 副作用、输入产物、恢复与权限的完整设计，不能只扩大这份集合。
AGENT_NODE_TYPES: frozenset[str] = frozenset(
    {
        NODE_TYPE_INTERNAL_VALIDATE,
        NODE_TYPE_CROSS_VALIDATE,
        NODE_TYPE_RISK_RATING,
    }
)


class NodeHandler(Protocol):
    """一个节点实现的契约。

    签名与既有实现一致：接收数据库会话、任务、已发布 manifest 与本次执行记录，
    返回一个可 JSON 化的结果摘要。返回值里 `blocked` 为真表示节点被阻断
    （例如必传材料缺失），`skipped` 为真表示本模板未配置这类检查——
    两者都不等于通过。
    """

    def __call__(
        self,
        db: Any,
        task: Any,
        manifest: dict[str, Any],
        execution: Any,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RegisteredNode:
    node_type: str
    label: str
    handler: NodeHandler


_HANDLERS: dict[str, NodeHandler] = {}


def register_node_handler(node_type: str, handler: NodeHandler) -> None:
    """注册一个节点实现。

    重复注册**同一种能力类型**时明确报错：静默覆盖会让后注册的实现生效，
    而注册顺序在导入顺序变化时会变——那是极难定位的问题。
    """
    if node_type not in NODE_TYPE_LABELS:
        raise BusinessError(
            "NODE_TYPE_UNKNOWN",
            f"节点能力类型“{node_type}”不在节点类型目录里，无法注册执行实现",
            status_code=500,
            details={"node_type": node_type, "known": sorted(NODE_TYPE_LABELS)},
        )
    if node_type in _HANDLERS:
        raise BusinessError(
            "NODE_HANDLER_DUPLICATE",
            f"节点能力类型“{node_type}”已经注册过执行实现，不能重复注册",
            status_code=500,
            details={"node_type": node_type},
        )
    _HANDLERS[node_type] = handler


def handler_for(node_type: str) -> NodeHandler:
    """按能力类型取执行实现。未注册时明确报错，不回落成任何默认实现。"""
    handler = _HANDLERS.get(node_type)
    if handler is None:
        raise BusinessError(
            "NODE_HANDLER_MISSING",
            f"节点能力类型“{NODE_TYPE_LABELS.get(node_type, node_type)}”当前没有执行实现，"
            "无法执行该节点",
            status_code=500,
            details={"node_type": node_type, "registered": sorted(_HANDLERS)},
        )
    return handler


def registered_node_types() -> tuple[str, ...]:
    """已注册执行实现的能力类型（供启动自检使用）。"""
    return tuple(sorted(_HANDLERS))


def node_type_label(node_type: str) -> str:
    """节点能力类型的中文名；未知类型原样返回编码。"""
    return NODE_TYPE_LABELS.get(node_type, node_type)


def legacy_node_type_of(node_code: str) -> str | None:
    """旧 manifest 缺 `node_type` 时的**确定性**迁移结果。

    旧格式里实例码本身就是能力类型（`node_code == node_type`），因此当且仅当
    `node_code` 是一个已知能力类型时，迁移是唯一确定的：返回它。

    自定义实例码（例如 `INT_A`）在旧格式里不存在——它的 `node_type` 一定曾被显式写出过。
    此时无法确定它属于哪一类节点，返回 None，由调用方**明确拒绝**把这份配置带进新版本：
    用 `node_type or node_code` 静默填一个自定义实例码，会把一个协议里不存在的
    「能力类型」写进新版本，发布校验只能在更晚的环节报出一个看不出原因的错误。
    """
    return node_code if node_code in NODE_TYPE_LABELS else None
