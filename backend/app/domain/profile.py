"""规则库格式 profile 的加载与识别。

profile 描述的是「某一份 Excel 规则库有哪些工作表、每张表有哪些列」——这是
**导入契约**，供 workbook 解析、node_rulebook 转换、导入服务与导出服务共同引用。

这里只做「读 JSON 文件、按版本选一份、按表头签名识别版本」：
**不读取全局配置、不决定文件放在哪里**——profile 目录与默认版本由调用方传入
（`services/workbook_service` 从配置取值后传入），因此本模块是纯函数式的，
不依赖 config，也就能被 domain 内其他模块安全引用。
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import BusinessError

# profile 目录：由调用方在导入时设置一次（见 services/workbook_service）。
# 领域层不读全局配置，目录从外部注入，避免 domain 依赖 config 与运行环境。
_PROFILE_DIRECTORY: Path | None = None
_DEFAULT_VERSION: str | None = None


def configure_profile_source(directory: Path, default_version: str) -> None:
    """设置 profile 目录与默认版本。应用启动与导入入口调用；未设置时任何读取都明确报错。"""
    global _PROFILE_DIRECTORY, _DEFAULT_VERSION
    _PROFILE_DIRECTORY = directory
    _DEFAULT_VERSION = default_version
    _load_profile_file.cache_clear()
    available_profile_versions.cache_clear()


def _profile_directory() -> Path:
    if _PROFILE_DIRECTORY is None:
        raise BusinessError(
            "PROFILE_SOURCE_UNCONFIGURED",
            "规则库模板目录尚未初始化，无法读取模板配置",
            status_code=500,
        )
    return _PROFILE_DIRECTORY


@lru_cache
def _load_profile_file(version: str) -> dict[str, Any]:
    path = _profile_directory() / f"{version}.json"
    try:
        with path.open("r", encoding="utf-8") as profile_file:
            profile = json.load(profile_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            "PROFILE_LOAD_FAILED",
            "合同审核模板配置读取失败",
            status_code=500,
            details={"version": version},
        ) from exc
    if profile.get("profile_version") != version:
        raise BusinessError("PROFILE_INVALID", "合同审核模板配置版本无效", status_code=500)
    return profile


@lru_cache
def available_profile_versions() -> tuple[str, ...]:
    return _discover_versions()


def _discover_versions() -> tuple[str, ...]:
    versions = sorted(path.stem for path in _profile_directory().glob("contract_review_*.json"))
    if not versions:
        raise BusinessError("PROFILE_LOAD_FAILED", "合同审核模板配置目录为空", status_code=500)
    return tuple(versions)


def default_profile_version() -> str:
    if _DEFAULT_VERSION is None:
        raise BusinessError(
            "PROFILE_SOURCE_UNCONFIGURED",
            "规则库模板目录尚未初始化，无法确定默认模板版本",
            status_code=500,
        )
    return _DEFAULT_VERSION


def load_rulebook_profile(version: str | None = None) -> dict[str, Any]:
    selected = version or default_profile_version()
    if selected not in _discover_versions():
        raise BusinessError(
            "PROFILE_VERSION_UNKNOWN",
            "合同审核模板格式版本不受支持",
            details={"version": selected, "supported": list(_discover_versions())},
        )
    return _load_profile_file(selected)


def role_index(version: str | None = None) -> dict[str, dict[str, Any]]:
    return {item["role"]: item for item in load_rulebook_profile(version)["sheet_roles"]}


# v3（节点版）独有列名、v2 独有列名与 v1 独有列名，用于按表头签名判断实际格式版本。
# 判定必须能区分三份格式：把节点版误判成 v2 会让「检查项目 / 合格要求 / 配置状态」
# 这些列被当成不存在的列，从而报出一堆与本格式无关的错误。
_V3_MARKERS = {
    "材料编号",
    "是否必传",
    "配置状态",
    "检查项目",
    "合格要求",
    "检查口径",
    "标准值",
    "是否必抽",
    "提取要求",
    "允许差值",
    "差值单位",
    "成立条件",
    "章节顺序",
    "内容类型",
    "结果排序",
}
# v4 在 v3 之上把「材料识别类型」列为独立列。它是 v4 独有的列名，
# 用它把 v4 与 v3 区分开：两者不能共用一个解析器，否则 v4 的
# 材料识别类型会被当成未知列而拒绝。
_V4_MARKERS = {"材料识别类型"}
_V2_MARKERS = {"适用条件", "检查方式", "检查项目", "展示字段", "处理建议", "检查名称", "来源节点"}
_V1_MARKERS = {"算子", "材料编码", "字段编码", "规则编码", "章节编码", "引用字段编码"}


def detect_profile_version(headers: dict[str, list[str]]) -> str:
    """按表头签名选择解析器。

    headers 是「表名 -> 该表真实表头单元格文本」。各版本由自己独有的列名区分；
    无法区分或同时命中多份格式时返回错误，不做模糊猜测，也不重新解释已发布的 manifest。
    """
    cells = {str(cell).strip() for values in headers.values() for cell in values if str(cell).strip()}
    v4_hits = len(cells & _V4_MARKERS)
    v3_hits = len(cells & _V3_MARKERS)
    v2_hits = len(cells & _V2_MARKERS)
    v1_hits = len(cells & _V1_MARKERS)
    # v4 必须先判：它的其余表头与 v3 相同，只有「材料识别类型」能把它区分出来。
    if v4_hits:
        return "contract_review_v4"
    # 节点版有 v2 没有的列名。要求命中数严格多于 v2，避免 v2 表头（含“检查项目/检查名称”）
    # 被误判成节点版；反之节点版命中的 v2 独有列（“检查项目”）也不足以胜出。
    if v3_hits and v3_hits > v2_hits and v3_hits > v1_hits:
        return "contract_review_v3"
    if v2_hits and v2_hits >= v1_hits:
        return "contract_review_v2"
    if v1_hits:
        return "contract_review_v1"
    raise BusinessError(
        "PROFILE_VERSION_UNDETECTED",
        "无法从表头判断这份 Excel 使用的模板格式版本，请使用最新模板重新填写",
        details={"detected_headers": sorted(cells)[:40]},
    )


def match_sheet_role(sheet_name: str, version: str | None = None) -> str | None:
    """按工作表名匹配业务用途；无法确定时返回 None。

    说明页是模板自己声明的（profile 的 instruction_sheet 键），未声明说明页的格式
    （contract_review_v2 起已取消 00_填写说明）不做 INSTRUCTION 判定，继续按
    sheet_roles 匹配；未匹配到任何角色的工作表一律返回 None，不会被当成业务工作表。
    """
    normalized = sheet_name.strip()
    profile = load_rulebook_profile(version)
    instruction_sheet = profile.get("instruction_sheet")
    if instruction_sheet and normalized == instruction_sheet:
        return "INSTRUCTION"
    for item in profile["sheet_roles"]:
        if normalized == item["display_name"] or normalized in item.get("aliases", []):
            return item["role"]
    return None


@lru_cache
def column_labels(version: str | None = None) -> dict[str, str]:
    """返回「列 key → 业务列名」映射，用于把问题与错误提示写成业务语言。"""
    labels: dict[str, str] = {}
    for item in load_rulebook_profile(version)["sheet_roles"]:
        for column in item["columns"]:
            labels.setdefault(column["key"], column["name"])
    return labels
