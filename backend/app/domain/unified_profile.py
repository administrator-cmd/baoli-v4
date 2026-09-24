"""统一模板协议 Profile：全系统**唯一**的一份模板协议。

它定义三件事，三件事都在同一份 JSON 里，不再按场景或格式版本拆分：

1. **系统支持哪些 Sheet**（`sheets[].sheet_role`）：全部业务 Sheet 的角色、中文名、
   别名、表头签名、编号前缀与列协议；
2. **每张 Sheet 的列协议**（`sheets[].columns`）：列的内部 key、业务列名、别名、
   是否必填、当前业务行是否必填、数据类型与允许取值；
3. **系统声明哪些节点类型**（`node_types`）：每种能力类型的中文名与上游依赖
   （`depends_on_types`）。它有两处用途：一是每张 Sheet 可绑定的节点类型
   （`sheets[].node_types`），系统据此给出绑定**建议**；二是新建场景时预置的
   标准节点集（每种能力类型一个实例）——预置只是**起点**，用户仍可增删改，
   Sheet→节点的绑定关系始终由用户确认后落库。

职责分离
--------
- 本文件（统一 JSON）负责「系统支持哪些 Sheet、每张 Sheet 的结构、有哪些节点类型」；
- 场景配置负责「这个场景用哪些 Sheet、Sheet 对应哪些节点及其流程关系」——
  预置给出默认节点集，之后的节点实例、顺序、依赖与 Sheet→节点绑定都由用户在
  场景配置里确认后落库（`scenario_draft_node` 与 `import_sheet.bound_node_code`）。

一份 base Excel
---------------
「只有一份协议」说的是**系统只有一份模板**：一次下载、一次上传、一次导入都针对它。
场景（合同审查 / 财报分析）的差异体现在「选了哪些 Sheet、绑了哪些节点、填了什么内容」上，
**不体现在表结构上**——不存在「合同版表」与「财报版表」，因此也没有业务族（family）这一层。
同一张「01_材料位」表，合同场景填它的材料位，财报场景填它自己的。

Sheet 名由协议固定，用户不得改名：角色由 Sheet 名直接确定，绑定步骤只用来指定
「这张表绑到哪个节点实例」，不再需要猜这张表是什么。

每张表声明一个**编号前缀**（`code_prefix`）。填写编号时，前缀让跨表引用无歧义；填写名称时，
系统只在当前规则库内做精确匹配，名称重复或不存在会明确报错，不按相似度猜测。

未绑定的 Sheet 留空即可：不进模板校验、不进转换、不进已发布配置、不进任务执行范围。
合同场景不配计算，留空 06/07/08 三张表即可。

说明页
------
`instruction_sheet` 声明模板说明页的名称（例如 `00_填写说明`）。模板生成、导出与导入
读同一声明，只有明确命中这个名称的 Sheet 才固定排除。**不按「隐藏」判断元数据**：
隐藏只是 Excel 的展示属性，隐藏的业务 Sheet 仍然可以由用户绑定与导入。

设计边界
--------
本模块只做「读 JSON、按角色取定义、给绑定建议」。**不读取全局配置、不决定文件位置**——
路径由调用方注入（services 层从 config 取值），因此 domain 内其他模块可以安全引用它。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import BusinessError

# 统一 Profile 路径由调用方注入。未注入时任何读取都明确报错。
_PROFILE_PATH: Path | None = None

# 统一协议的版本标识。它随文件内容演进，不再是「格式版本」——
# 系统只有一份协议，版本只用于记录「这份配置按哪一版协议解释」。
UNIFIED_PROFILE_VERSION = "unified_v3"


def configure_unified_profile(path: Path) -> None:
    """注入统一 Profile 的路径。应用启动时调用一次。"""
    global _PROFILE_PATH
    _PROFILE_PATH = path
    load_unified_profile.cache_clear()


def _profile_path() -> Path:
    if _PROFILE_PATH is None:
        raise BusinessError(
            "UNIFIED_PROFILE_SOURCE_UNCONFIGURED",
            "统一模板协议尚未初始化，无法读取模板配置",
            status_code=500,
        )
    return _PROFILE_PATH


@lru_cache
def load_unified_profile() -> dict[str, Any]:
    """加载唯一一份统一模板协议。

    读不到、解析失败或形状不合法时明确报错，**不回落**到任何旧目录或旧版本：
    回落到旧协议会让新列被当成未知列而拒绝整份规则库，也会让「系统支持哪些 Sheet」
    在两个地方各说一套。
    """
    path = _profile_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            "UNIFIED_PROFILE_LOAD_FAILED",
            "统一模板协议读取失败",
            status_code=500,
            details={"path": str(path)},
        ) from exc
    _assert_profile_shape(profile)
    return profile


def _assert_profile_shape(profile: dict[str, Any]) -> None:
    """协议本身的形状校验：缺字段当即报错，不在使用处逐个兜底。"""
    instruction = profile.get("instruction_sheet")
    if not isinstance(instruction, str) or not instruction.strip():
        raise BusinessError(
            "UNIFIED_PROFILE_INVALID",
            "统一模板协议没有声明说明页名称（instruction_sheet）：模板生成、导出与导入必须读同一声明",
            status_code=500,
        )
    node_types = profile.get("node_types")
    if not isinstance(node_types, list) or not node_types:
        raise BusinessError(
            "UNIFIED_PROFILE_INVALID",
            "统一模板协议没有声明任何节点类型",
            status_code=500,
        )
    seen_types: set[str] = set()
    for node in node_types:
        node_type = node.get("node_type")
        if not isinstance(node_type, str) or not node_type:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                "统一模板协议里有节点类型缺少 node_type",
                status_code=500,
            )
        if node_type in seen_types:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"统一模板协议里节点类型“{node_type}”重复声明",
                status_code=500,
            )
        seen_types.add(node_type)
        depends = node.get("depends_on_types", [])
        if not isinstance(depends, list) or any(not isinstance(item, str) or not item for item in depends):
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"节点类型“{node_type}”的 depends_on_types 必须是非空文本的数组",
                status_code=500,
            )
        unknown_depends = sorted(set(depends) - {item.get("node_type") for item in node_types})
        if unknown_depends:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"节点类型“{node_type}”声明了协议里不存在的上游节点类型："
                + "、".join(f"“{item}”" for item in unknown_depends),
                status_code=500,
            )
        if node_type in depends:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"节点类型“{node_type}”把自身声明为上游，无法确定执行顺序",
                status_code=500,
            )
    _assert_node_types_acyclic(node_types)
    sheets = profile.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise BusinessError(
            "UNIFIED_PROFILE_INVALID",
            "统一模板协议没有声明任何 Sheet",
            status_code=500,
        )
    seen_roles: set[str] = set()
    for sheet in sheets:
        role = sheet.get("sheet_role")
        if not isinstance(role, str) or not role:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                "统一模板协议里有 Sheet 缺少 sheet_role",
                status_code=500,
            )
        if role in seen_roles:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"统一模板协议里 Sheet 角色“{role}”重复声明；同一角色只能声明一次",
                status_code=500,
            )
        seen_roles.add(role)
        prefix = sheet.get("code_prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"Sheet“{role}”没有声明编号前缀（code_prefix）："
                "跨表引用填写编号时必须能由编号本身判断它指向哪张表；名称引用只能精确匹配，不能按相似度猜",
                status_code=500,
            )
        bound_types = sheet.get("node_types")
        if not isinstance(bound_types, list) or not bound_types:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"Sheet“{role}”没有声明可绑定的节点类型",
                status_code=500,
            )
        unknown = sorted(set(bound_types) - seen_types)
        if unknown:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"Sheet“{role}”声明了协议里不存在的节点类型：" + "、".join(f"“{item}”" for item in unknown),
                status_code=500,
            )
        columns = sheet.get("columns")
        if not isinstance(columns, list) or not columns:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"Sheet“{role}”没有声明任何列",
                status_code=500,
            )
        keys = [column.get("key") for column in columns]
        if any(not isinstance(key, str) or not key for key in keys):
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"Sheet“{role}”有列缺少内部 key",
                status_code=500,
            )
        if len(set(keys)) != len(keys):
            duplicated = sorted({key for key in keys if keys.count(key) > 1})
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"Sheet“{role}”有重复的列 key：{'、'.join(duplicated)}",
                status_code=500,
            )


def _assert_node_types_acyclic(node_types: list[dict[str, Any]]) -> None:
    """`depends_on_types` 不能成环。

    这份声明是新建场景预置节点集的依赖来源，环会让预置出来的流程永远等不到就绪节点——
    那种失败要到任务执行时才暴露，而原因在协议文件里。因此在这里就明确拒绝。
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
                "UNIFIED_PROFILE_INVALID",
                "统一模板协议的节点依赖形成环：" + " → ".join(cycle),
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


def sheet_index(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Sheet 角色 → Sheet 定义。转换器与模板生成都按它取列协议。"""
    return {sheet["sheet_role"]: sheet for sheet in profile["sheets"]}


def sheet_definition(profile: dict[str, Any], sheet_role: str) -> dict[str, Any]:
    """按 Sheet 角色取它的定义；角色未在协议里声明时明确报错。"""
    definition = sheet_index(profile).get(sheet_role)
    if definition is None:
        raise BusinessError(
            "UNIFIED_PROFILE_ROLE_UNKNOWN",
            f"Sheet 角色“{sheet_role}”不在统一模板协议里",
            details={"sheet_role": sheet_role},
        )
    return definition


def map_sheet_columns(
    definition: dict[str, Any],
    header_values: dict[str, Any],
    *,
    sheet_key: str,
    sheet_name: str,
    header_row: int,
    required_keys: set[str],
) -> dict[str, str]:
    """按协议列名及别名，把每个列 key 唯一映射到表头单元格。"""
    details = {
        "sheet_key": sheet_key,
        "sheet_name": sheet_name,
        "sheet_role": definition["sheet_role"],
        "row_no": header_row,
    }
    label_to_key: dict[str, str] = {}
    for column in definition["columns"]:
        for raw_label in (column["name"], *(column.get("aliases") or [])):
            label = str(raw_label).strip()
            if not label:
                continue
            owner = label_to_key.get(label)
            if owner is not None and owner != column["key"]:
                raise BusinessError(
                    "SHEET_HEADER_ALIAS_CONFLICT",
                    f"工作表「{sheet_name}」的表头别名「{label}」同时指向「{owner}」与「{column['key']}」",
                    details={**details, "alias": label, "columns": sorted({owner, column["key"]})},
                )
            label_to_key[label] = column["key"]

    seen_labels: dict[str, str] = {}
    mapping: dict[str, str] = {}
    duplicate_labels: set[str] = set()
    duplicate_keys: set[str] = set()
    duplicate_cells: set[str] = set()
    unknown: list[str] = []
    unknown_cells: list[dict[str, str]] = []
    for address, value in header_values.items():
        label = str(value).strip()
        if not label:
            continue
        if label in seen_labels:
            duplicate_labels.add(label)
            duplicate_cells.update({seen_labels[label], address})
        else:
            seen_labels[label] = address
        key = label_to_key.get(label)
        if key is None:
            unknown.append(label)
            unknown_cells.append({"cell": address, "value": label})
        elif key in mapping:
            duplicate_keys.add(key)
            duplicate_cells.update({mapping[key], address})
        else:
            mapping[key] = address
    if duplicate_labels or duplicate_keys:
        raise BusinessError(
            "SHEET_HEADER_DUPLICATED",
            f"工作表「{sheet_name}」第 {header_row} 行存在重复表头或多个表头指向同一列",
            details={
                **details,
                "duplicated": sorted(duplicate_labels),
                "duplicated_keys": sorted(duplicate_keys),
                "cells": sorted(duplicate_cells),
            },
        )

    missing = [
        column["name"]
        for column in definition["columns"]
        if column["key"] in required_keys and column["key"] not in mapping
    ]
    if missing or unknown:
        reasons = []
        if missing:
            reasons.append("缺少列：" + "、".join(f"「{name}」" for name in missing))
        if unknown:
            reasons.append("无法识别的列：" + "、".join(f"「{name}」" for name in unknown))
        raise BusinessError(
            "SHEET_HEADER_MISMATCH",
            f"工作表「{sheet_name}」第 {header_row} 行的表头不符合模板（{'；'.join(reasons)}）",
            details={**details, "missing": missing, "unknown": unknown, "unknown_cells": unknown_cells},
        )
    return mapping


def node_type_definition(profile: dict[str, Any], node_type: str) -> dict[str, Any]:
    """按节点类型取它的定义；类型未声明时明确报错。"""
    for node in profile["node_types"]:
        if node["node_type"] == node_type:
            return node
    raise BusinessError(
        "UNIFIED_PROFILE_NODE_TYPE_UNKNOWN",
        f"节点类型“{node_type}”不在统一模板协议里",
        details={"node_type": node_type},
    )


def node_types_of_sheet_role(profile: dict[str, Any], sheet_role: str) -> tuple[str, ...]:
    """一张 Sheet 可以绑定的节点类型。

    这是**建议范围**，不是自动绑定：用户必须显式选择节点实例并确认绑定关系。
    """
    return tuple(sheet_definition(profile, sheet_role)["node_types"])


def instruction_sheet_name(profile: dict[str, Any]) -> str:
    """模板说明页的名称。

    模板生成、导出与导入读的是**同一个声明**。不按「隐藏」识别说明页：
    隐藏只是 Excel 的展示属性，隐藏的业务 Sheet 仍可以由用户绑定与导入。
    """
    return str(profile["instruction_sheet"])


def code_prefix_of_sheet_role(profile: dict[str, Any], sheet_role: str) -> str:
    """一张 Sheet 的编号前缀（例如 `RSK-`）。

    跨表引用填写编号时靠它判断指向：`关联风险` 列里出现 `RSK-003` 就一定是风险编号，
    出现 `FLD-012` 就一定是字段编号；填写名称时按当前列允许的对象做精确匹配。
    """
    return str(sheet_definition(profile, sheet_role)["code_prefix"])


def sheet_roles_of_prefix(profile: dict[str, Any]) -> dict[str, str]:
    """编号前缀 → Sheet 角色。前缀重复时明确报错（两张表共用前缀会让引用无法判定）。"""
    result: dict[str, str] = {}
    for sheet in profile["sheets"]:
        prefix = str(sheet["code_prefix"])
        if prefix in result:
            raise BusinessError(
                "UNIFIED_PROFILE_INVALID",
                f"编号前缀“{prefix}”被多张 Sheet 声明（{result[prefix]} / {sheet['sheet_role']}），"
                "跨表引用将无法判断指向",
                status_code=500,
                details={"code_prefix": prefix},
            )
        result[prefix] = str(sheet["sheet_role"])
    return result


def role_of_legacy_role(
    profile: dict[str, Any],
    legacy_role: str,
    *,
    node_type: str | None = None,
) -> str | None:
    """旧格式 role → 统一协议的 sheet_role。

    统一协议给合同审核的七张表加了 `CONTRACT_` 前缀，因为它们的业务含义与财报的
    同名 role 不同（合同「材料清单」是材料位定义，财报「材料清单」是材料清单规则库）。
    历史导入作业按旧 role 保存绑定，读取时必须能映射回统一角色；映射是显式声明的
    `legacy_role` 字段，不按名称相似度猜。

    **同一个旧 role 会命中多张表**（`MATERIAL_LIST` 同时是合同表与财报表的旧名），
    因此单独一个旧 role 不足以定位到唯一角色。此时必须给出 `node_type`：
    它把候选收窄到该节点类型能绑定的那几张表。仍然无法唯一确定时返回 None，
    由调用方明确报出「需要用户重新确认绑定」——静默取第一个会把一份合同规则库
    按财报的列协议去解释，报出一堆与真实问题无关的缺列错误。
    """
    candidates = [sheet["sheet_role"] for sheet in profile["sheets"] if sheet.get("legacy_role") == legacy_role]
    if node_type is not None:
        index = sheet_index(profile)
        candidates = [role for role in candidates if node_type in index[role]["node_types"]]
    if len(candidates) == 1:
        return candidates[0]
    return None


def suggest_sheet_role(profile: dict[str, Any], sheet_name: str, header_cells: set[str]) -> str | None:
    """按表头签名给 Sheet 一个**建议**角色。

    `header_cells` 必须是**这一张 Sheet 自己**的表头单元格，不是整份工作簿的列名并集：
    用并集会让一张表因为别的表有某些列而被误判。

    判定顺序是刻意的：

    1. 表头签名：它比名称可靠——「04_勾稽核算规则库」与「05_经营指标计算规则库」
       在名称上都像计算，只有列结构能区分；
    2. 名称与别名：签名无命中、**或签名命中多张表**时用名称作为决胜依据。
       05 与 06 的列结构完全相同，签名无法区分，但它们的 display_name 与 aliases
       是各自声明的、互不相同，此时用名称是**确定性**的判定，不是猜测；
    3. 都无法确定时返回 None，由用户在绑定步骤选择。

    只返回建议，不写库。绑定结果由 `import_sheet.bound_node_code` / `bound_role` 承载。
    """
    signature_matches: list[str] = []
    for sheet in profile["sheets"]:
        signature = {str(item).strip() for item in sheet.get("header_signature", [])}
        if signature and signature.issubset(header_cells):
            signature_matches.append(sheet["sheet_role"])
    if len(signature_matches) == 1:
        return signature_matches[0]
    normalized = sheet_name.strip()
    name_matches: list[str] = []
    for sheet in profile["sheets"]:
        names = {sheet.get("display_name"), *sheet.get("aliases", [])}
        if normalized in {str(item).strip() for item in names if item}:
            name_matches.append(sheet["sheet_role"])
    if len(name_matches) == 1:
        return name_matches[0]
    # 签名与名称都无法唯一确定：交用户决定，不猜。
    return None


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """统一协议的只读摘要：Sheet 数量、名称与可绑定的节点类型。

    页面的「模板要求哪几类工作表」必须读它，不能在组件里写死数量或名称——
    统一模板覆盖的是系统能力全集，写死的那句话在任何具体场景上都是错的。
    """
    return {
        "profile_version": profile.get("profile_version"),
        "display_name": profile.get("display_name"),
        "schema_version": profile.get("schema_version"),
        # 说明页名称由协议声明：模板生成、导出与导入读同一声明。
        "instruction_sheet": instruction_sheet_name(profile),
        "sheet_count": len(profile["sheets"]),
        "node_types": [
            {
                "node_type": node["node_type"],
                "display_name": node.get("display_name", node["node_type"]),
                "depends_on_types": list(node.get("depends_on_types", [])),
            }
            for node in profile["node_types"]
        ],
        "sheets": [
            {
                "sheet_role": sheet["sheet_role"],
                # 编号前缀：跨表引用靠它判断指向。
                "code_prefix": str(sheet["code_prefix"]),
                "display_name": sheet.get("display_name", sheet["sheet_role"]),
                # 一张 Sheet 可能被多个节点类型使用（财报的计算三表都归 CALCULATION），
                # 因此这里是列表；页面据此提示用户该把这张表绑到哪类节点上。
                "node_types": list(sheet["node_types"]),
                "column_count": len(sheet["columns"]),
            }
            for sheet in profile["sheets"]
        ],
    }


def sheet_role_label(profile: dict[str, Any], sheet_role: str | None) -> str:
    """Sheet 角色的业务中文名；未声明时如实回报未识别，不猜。"""
    if not sheet_role:
        return "未指定用途"
    sheet = sheet_index(profile).get(sheet_role)
    return str(sheet.get("display_name") or sheet_role) if sheet else f"未识别的用途（{sheet_role}）"
