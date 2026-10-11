"""执行记录类型目录：仅声明统一规则执行容器，没有业务阶段与预置依赖。"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..common.exceptions import BusinessError

_NODE_PROFILE_PATH: Path | None = None
# 当前规则库执行契约版本；旧任务快照不自动转换。
NODE_PROFILE_VERSION = "rule_library_v1"


def configure_node_profile(path: Path) -> None:
    global _NODE_PROFILE_PATH
    _NODE_PROFILE_PATH = path
    load_node_profile.cache_clear()


@lru_cache
def load_node_profile() -> dict[str, Any]:
    if _NODE_PROFILE_PATH is None:
        raise BusinessError("NODE_PROFILE_SOURCE_UNCONFIGURED", "执行契约目录尚未初始化", status_code=500)
    try:
        with _NODE_PROFILE_PATH.open("r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("NODE_PROFILE_LOAD_FAILED", "执行契约目录读取失败", status_code=500) from exc
    _assert_profile_shape(profile)
    return profile


def _assert_profile_shape(profile: dict[str, Any]) -> None:
    for key in ("profile_version", "display_name"):
        if not isinstance(profile.get(key), str) or not profile[key]:
            raise BusinessError("NODE_PROFILE_INVALID", f"执行契约目录缺少{key}", status_code=500)


def node_summary() -> dict[str, Any]:
    profile = load_node_profile()
    return {
        "profile_version": profile["profile_version"],
        "display_name": profile["display_name"],
    }
