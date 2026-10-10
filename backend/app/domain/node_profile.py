"""节点能力目录：只声明能力类型与新场景预置依赖，不包含 Excel 模板。"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..common.exceptions import BusinessError

_NODE_PROFILE_PATH: Path | None = None
# 沿用已存场景的能力版本身份，不改变数据库中的历史版本。
NODE_PROFILE_VERSION = "unified_v3"


def configure_node_profile(path: Path) -> None:
    global _NODE_PROFILE_PATH
    _NODE_PROFILE_PATH = path
    load_node_profile.cache_clear()


@lru_cache
def load_node_profile() -> dict[str, Any]:
    if _NODE_PROFILE_PATH is None:
        raise BusinessError("NODE_PROFILE_SOURCE_UNCONFIGURED", "节点能力目录尚未初始化", status_code=500)
    try:
        with _NODE_PROFILE_PATH.open("r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("NODE_PROFILE_LOAD_FAILED", "节点能力目录读取失败", status_code=500) from exc
    _assert_profile_shape(profile)
    return profile


def _assert_profile_shape(profile: dict[str, Any]) -> None:
    node_types = profile.get("node_types")
    if not isinstance(node_types, list) or not node_types:
        raise BusinessError(
            "NODE_PROFILE_INVALID",
            "节点能力目录没有声明任何节点类型",
            status_code=500,
        )
    seen_types: set[str] = set()
    for node in node_types:
        node_type = node.get("node_type")
        if not isinstance(node_type, str) or not node_type:
            raise BusinessError(
                "NODE_PROFILE_INVALID",
                "节点能力目录里有节点类型缺少 node_type",
                status_code=500,
            )
        if node_type in seen_types:
            raise BusinessError(
                "NODE_PROFILE_INVALID",
                f"节点能力目录里节点类型“{node_type}”重复声明",
                status_code=500,
            )
        seen_types.add(node_type)
        depends = node.get("depends_on_types", [])
        if not isinstance(depends, list) or any(not isinstance(item, str) or not item for item in depends):
            raise BusinessError(
                "NODE_PROFILE_INVALID",
                f"节点类型“{node_type}”的 depends_on_types 必须是非空文本的数组",
                status_code=500,
            )
        unknown_depends = sorted(set(depends) - {item.get("node_type") for item in node_types})
        if unknown_depends:
            raise BusinessError(
                "NODE_PROFILE_INVALID",
                f"节点类型“{node_type}”声明了能力目录里不存在的上游节点类型："
                + "、".join(f"“{item}”" for item in unknown_depends),
                status_code=500,
            )
        if node_type in depends:
            raise BusinessError(
                "NODE_PROFILE_INVALID",
                f"节点类型“{node_type}”把自身声明为上游，无法确定执行顺序",
                status_code=500,
            )
    _assert_node_types_acyclic(node_types)


def _assert_node_types_acyclic(node_types: list[dict[str, Any]]) -> None:
    """`depends_on_types` 不能成环。

    这份声明是新建场景预置节点集的依赖来源，环会让预置出来的流程永远等不到就绪节点——
    那种失败要到任务执行时才暴露，而原因在节点能力文件里。因此在这里就明确拒绝。
    """
    depends_of = {node["node_type"]: tuple(node.get("depends_on_types") or ()) for node in node_types}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node_type: str) -> None:
        marker = state.get(node_type, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = stack[stack.index(node_type) :] + [node_type]
            raise BusinessError(
                "NODE_PROFILE_INVALID",
                "节点能力目录的节点依赖形成环：" + " → ".join(cycle),
                status_code=500,
                details={"cycle": cycle},
            )
        state[node_type] = 1
        stack.append(node_type)
        for upstream in depends_of[node_type]:
            visit(upstream)
        stack.pop()
        state[node_type] = 2

    for node_type in depends_of:
        visit(node_type)


def node_summary() -> dict[str, Any]:
    profile = load_node_profile()
    return {
        "profile_version": profile["profile_version"],
        "display_name": profile["display_name"],
        "node_types": profile["node_types"],
    }
