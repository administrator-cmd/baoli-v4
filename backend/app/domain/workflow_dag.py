"""Workflow 的 DAG 校验与就绪节点选择。

调度器只做两件事：

1. **校验**：发布期检查缺失依赖、循环依赖、依赖节点未启用；
2. **选择**：运行期按已完成集合算出「就绪节点」——依赖全部已完成、自己尚未执行的节点。

首期只要求**串行**执行就绪节点，因此这里按 (sequence_no, node_code) 排序后取第一个。
排序口径是确定的：`sequence_no` 只用于展示与确定性排序，**不代替**依赖关系；
真正决定能不能执行的是 `depends_on`。

旧场景（线性流程）由编译期写成「依赖前一个节点」，语义与旧的 `nodes[index+1]`
完全一致，因此旧任务的执行顺序不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import BusinessError
from .node_registry import NODE_TYPE_LABELS, legacy_node_type_of


@dataclass(frozen=True)
class NodePlan:
    """一个节点的执行计划视图。"""

    node_instance_code: str
    node_type: str
    name: str
    sequence_no: int
    depends_on: tuple[str, ...]
    enabled: bool
    config: dict[str, Any]

    @property
    def label(self) -> str:
        """供报错与日志阅读的节点名。

        名称与能力类型同名时只写一次：新建场景预置的节点名称就是能力类型名，
        无条件拼成「材料检查（材料检查）」是纯噪音。用户改过名的节点才需要标出能力类型。
        """
        from .node_registry import node_type_label

        type_label = node_type_label(self.node_type)
        return self.name if self.name == type_label else f"{self.name}（{type_label}）"


def plans_of(manifest: dict[str, Any]) -> list[NodePlan]:
    """从已发布 manifest 里取出节点计划，按 sequence_no 排序。

    旧 manifest 的节点没有 `node_type` / `depends_on`：那时节点就是能力类型本身，
    依赖就是前一个节点。这里按同一口径补出等价的计划视图，让新旧 manifest
    共用一套调度代码，而不是在调度器里分叉。
    """
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise BusinessError("MANIFEST_NO_NODE", "已发布配置里没有任何节点，无法执行", status_code=500)
    plans: list[NodePlan] = []
    ordered = sorted(nodes, key=lambda item: (item.get("sequence_no", 0), str(item.get("node_code"))))
    previous: str | None = None
    for item in ordered:
        node_code = item.get("node_code")
        if not isinstance(node_code, str) or not node_code:
            raise BusinessError("MANIFEST_NODE_INVALID", "已发布配置里的节点缺少实例码", status_code=500)
        node_type = item.get("node_type")
        if not isinstance(node_type, str) or not node_type:
            # 旧 manifest：`node_code` 就是能力类型。只有当它本身是一个已知能力类型时，
            # 迁移才是确定性；自定义实例码（例如 INT_A）无法确定属于哪一类节点，
            # 此时明确拒绝，不拿实例码冒充能力类型去执行。
            node_type = legacy_node_type_of(node_code)
            if node_type is None:
                raise BusinessError(
                    "MANIFEST_NODE_TYPE_UNRESOLVED",
                    f"已发布配置里的节点“{node_code}”没有能力类型，且它本身不是已知的能力类型，"
                    "无法确定该由哪个实现执行",
                    status_code=500,
                    details={"node_code": node_code, "known_types": sorted(NODE_TYPE_LABELS)},
                )
        depends_on = item.get("depends_on")
        if not isinstance(depends_on, list):
            # 旧 manifest：线性依赖。第一个节点无依赖，其余依赖前一个。
            depends_on = [] if previous is None else [previous]
        plans.append(
            NodePlan(
                node_instance_code=node_code,
                node_type=str(node_type),
                name=str(item.get("name") or node_code),
                sequence_no=int(item.get("sequence_no") or 0),
                depends_on=tuple(str(value) for value in depends_on),
                enabled=bool(item.get("enabled", True)),
                config=item.get("config_json") if isinstance(item.get("config_json"), dict) else {},
            )
        )
        previous = node_code
    return plans


def validate_dag(plans: list[NodePlan]) -> None:
    """发布期校验依赖图。任一项不成立都抛 BusinessError，不静默修正。

    校验三件事，顺序固定：先看依赖是否存在（缺依赖时谈环没有意义），
    再看是否成环，最后看依赖的节点是否启用。
    """
    if not plans:
        raise BusinessError("MANIFEST_NO_NODE", "已发布配置里没有任何节点，无法执行", status_code=500)
    codes = [plan.node_instance_code for plan in plans]
    duplicated = sorted({code for code in codes if codes.count(code) > 1})
    if duplicated:
        raise BusinessError(
            "MANIFEST_NODE_DUPLICATE",
            "节点实例码在场景版本内必须唯一，以下实例码重复：" + "、".join(duplicated),
            details={"node_codes": duplicated},
        )
    by_code = {plan.node_instance_code: plan for plan in plans}
    for plan in plans:
        missing = [code for code in plan.depends_on if code not in by_code]
        if missing:
            raise BusinessError(
                "MANIFEST_NODE_DEPENDENCY_MISSING",
                f"节点“{plan.label}”依赖的节点不存在："
                + "、".join(f"“{code}”" for code in missing)
                + "。请补齐依赖或删除该依赖后重新发布",
                details={"node_code": plan.node_instance_code, "missing": missing},
            )
        if plan.node_instance_code in plan.depends_on:
            raise BusinessError(
                "MANIFEST_NODE_SELF_DEPENDENCY",
                f"节点“{plan.label}”依赖了自己，无法执行",
                details={"node_code": plan.node_instance_code},
            )
    _assert_acyclic(by_code)
    for plan in plans:
        disabled = [
            code for code in plan.depends_on if not by_code[code].enabled
        ]
        if plan.enabled and disabled:
            raise BusinessError(
                "MANIFEST_NODE_DEPENDENCY_DISABLED",
                f"节点“{plan.label}”依赖的节点已停用："
                + "、".join(f"“{by_code[code].label}”" for code in disabled)
                + "。请启用被依赖的节点，或同时停用本节点",
                details={"node_code": plan.node_instance_code, "disabled": disabled},
            )


def _assert_acyclic(by_code: dict[str, NodePlan]) -> None:
    """循环依赖检查。

    环意味着环上的节点永远等不到依赖完成：调度器会报告「没有就绪节点」，
    而那与「全部完成」在调用方看来可能只差一个布尔值。因此在发布期阻断。
    """
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(code: str) -> None:
        marker = state.get(code, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = stack[stack.index(code) :] + [code]
            raise BusinessError(
                "MANIFEST_NODE_DEPENDENCY_CYCLE",
                "节点依赖形成环，无法确定执行顺序："
                + " → ".join(by_code[item].name if item in by_code else item for item in cycle),
                details={"cycle": cycle},
            )
        state[code] = 1
        stack.append(code)
        for target in by_code[code].depends_on:
            visit(target)
        stack.pop()
        state[code] = 2

    for code in sorted(by_code):
        visit(code)


def ready_node(
    plans: list[NodePlan],
    *,
    completed: set[str],
) -> NodePlan | None:
    """下一个可执行的节点；没有就绪节点时返回 None。

    就绪 = 已启用 且 未在 completed 里 且 全部依赖都在 completed 里。

    返回 None 有两种含义，调用方**必须**自己区分（见 `pending_nodes`）：
    「全部执行完」与「有节点因依赖未满足而卡住」。把它们合并成一个布尔值
    会让卡住的任务显示成已完成。
    """
    for plan in plans:
        if not plan.enabled or plan.node_instance_code in completed:
            continue
        if all(code in completed for code in plan.depends_on):
            return plan
    return None


def ready_node_for_pending_review(
    plans: list[NodePlan],
    *,
    completed: set[str],
) -> NodePlan | None:
    """有待办时，只选择能够到达人工复核的就绪节点。"""
    review_nodes = [
        plan for plan in plans
        if plan.enabled and plan.node_type == "MANUAL_REVIEW" and plan.node_instance_code not in completed
    ]
    if len(review_nodes) != 1:
        return None
    review = review_nodes[0]
    by_code = {plan.node_instance_code: plan for plan in plans}
    path = {review.node_instance_code}
    pending = list(review.depends_on)
    while pending:
        code = pending.pop()
        if code in path:
            continue
        path.add(code)
        pending.extend(by_code[code].depends_on)
    if any(by_code[code].node_type in {"RISK_RATING", "REPORT_OUTPUT"} for code in path):
        return None

    ready = [
        plan for plan in plans
        if plan.enabled
        and plan.node_instance_code in path
        and plan.node_instance_code not in completed
        and all(code in completed for code in plan.depends_on)
    ]
    return next((plan for plan in ready if plan.node_type == "MANUAL_REVIEW"), ready[0] if ready else None)


def pending_nodes(plans: list[NodePlan], *, completed: set[str]) -> list[NodePlan]:
    """仍未完成且已启用的节点。"""
    return [plan for plan in plans if plan.enabled and plan.node_instance_code not in completed]


def is_finished(plans: list[NodePlan], *, completed: set[str]) -> bool:
    """全部已启用节点都已完成。"""
    return not pending_nodes(plans, completed=completed)


def blocked_nodes(plans: list[NodePlan], *, completed: set[str]) -> list[NodePlan]:
    """因依赖未完成而无法执行、且当前没有别的就绪节点的节点。

    它的存在说明 DAG 卡住了。发布期已排除环与缺失依赖，因此这里通常为空；
    真出现时（例如依赖节点被运行时阻断）必须让调用方看到，而不是报告「已完成」。
    """
    ready = ready_node(plans, completed=completed)
    if ready is not None:
        return []
    return [
        plan
        for plan in pending_nodes(plans, completed=completed)
        if not all(code in completed for code in plan.depends_on)
    ]
