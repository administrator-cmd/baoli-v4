"""场景自己的导入协议与统一执行协议之间的适配层。

`unified_v3` 仍然是系统内部的执行协议；它不再被当作每个业务场景的 Excel 业务定义。
场景保存自己的 schema 身份和外部列映射，导入时由本模块把它叠加到内部协议上，
后续候选、发布编译和执行器继续接收稳定的内部字段 key。

第一版适配器只开放 Sheet 展示名、别名和列别名的覆盖。新增业务结构时先明确增加
适配器能力，而不是把未知 JSON 直接送给执行器。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..errors import BusinessError
from . import unified_profile


DEFAULT_SCHEMA_KEY = "base_excel"
DEFAULT_SCHEMA_VERSION = "1"


def default_schema() -> dict[str, Any]:
    """返回新场景使用的内置 base Excel 适配器声明。"""
    return {
        "schema_key": DEFAULT_SCHEMA_KEY,
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "profile_version": unified_profile.UNIFIED_PROFILE_VERSION,
        "sheet_overrides": {},
    }


def normalize_schema(value: Any | None) -> dict[str, Any]:
    """校验并规范化场景 schema JSON。

    空值表示使用内置 base Excel；显式传入的未知属性会被拒绝，避免配置看似保存成功
    但执行器实际忽略了业务方填写的内容。
    """
    if value is None:
        return default_schema()
    if not isinstance(value, dict):
        raise BusinessError("SCENARIO_SCHEMA_INVALID", "场景 schema 必须是 JSON 对象", status_code=400)
    unknown_properties = sorted(set(value) - {"schema_key", "schema_version", "profile_version", "sheet_overrides"})
    if unknown_properties:
        raise BusinessError(
            "SCENARIO_SCHEMA_PROPERTY_UNKNOWN",
            "场景 schema 包含不支持的属性：" + "、".join(unknown_properties),
            status_code=400,
        )
    schema_key = value.get("schema_key")
    schema_version = value.get("schema_version")
    if not isinstance(schema_key, str) or not schema_key.strip():
        raise BusinessError("SCENARIO_SCHEMA_KEY_REQUIRED", "场景 schema 缺少 schema_key", status_code=400)
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise BusinessError("SCENARIO_SCHEMA_VERSION_REQUIRED", "场景 schema 缺少 schema_version", status_code=400)
    profile_version = value.get("profile_version", unified_profile.UNIFIED_PROFILE_VERSION)
    if profile_version != unified_profile.UNIFIED_PROFILE_VERSION:
        raise BusinessError(
            "SCENARIO_SCHEMA_PROFILE_UNSUPPORTED",
            f"场景 schema 依赖的内部协议“{profile_version}”当前不受支持",
            status_code=400,
            details={"profile_version": profile_version},
        )
    overrides = value.get("sheet_overrides", {})
    if not isinstance(overrides, dict):
        raise BusinessError("SCENARIO_SCHEMA_OVERRIDES_INVALID", "sheet_overrides 必须是 JSON 对象", status_code=400)
    profile = unified_profile.load_unified_profile()
    known_roles = set(unified_profile.sheet_index(profile))
    unknown_roles = sorted(set(overrides) - known_roles)
    if unknown_roles:
        raise BusinessError(
            "SCENARIO_SCHEMA_SHEET_UNKNOWN",
            "场景 schema 覆盖了内部协议不存在的 Sheet：" + "、".join(unknown_roles),
            status_code=400,
            details={"sheet_roles": unknown_roles},
        )
    normalized_overrides: dict[str, dict[str, Any]] = {}
    for role, raw_override in overrides.items():
        if not isinstance(raw_override, dict):
            raise BusinessError(
                "SCENARIO_SCHEMA_OVERRIDE_INVALID",
                f"Sheet“{role}”的覆盖配置必须是 JSON 对象",
                status_code=400,
            )
        allowed = {"display_name", "aliases", "column_aliases"}
        unknown = sorted(set(raw_override) - allowed)
        if unknown:
            raise BusinessError(
                "SCENARIO_SCHEMA_OVERRIDE_PROPERTY_UNKNOWN",
                f"Sheet“{role}”包含不支持的覆盖属性：" + "、".join(unknown),
                status_code=400,
            )
        override: dict[str, Any] = {}
        if "display_name" in raw_override:
            display_name = raw_override["display_name"]
            if not isinstance(display_name, str) or not display_name.strip():
                raise BusinessError("SCENARIO_SCHEMA_DISPLAY_NAME_INVALID", f"Sheet“{role}”的 display_name 无效", status_code=400)
            override["display_name"] = display_name.strip()
        if "aliases" in raw_override:
            aliases = raw_override["aliases"]
            if not isinstance(aliases, list) or any(not isinstance(item, str) or not item.strip() for item in aliases):
                raise BusinessError("SCENARIO_SCHEMA_ALIASES_INVALID", f"Sheet“{role}”的 aliases 必须是文本数组", status_code=400)
            override["aliases"] = list(dict.fromkeys(item.strip() for item in aliases))
        if "column_aliases" in raw_override:
            column_aliases = raw_override["column_aliases"]
            if not isinstance(column_aliases, dict):
                raise BusinessError("SCENARIO_SCHEMA_COLUMN_ALIASES_INVALID", f"Sheet“{role}”的 column_aliases 必须是 JSON 对象", status_code=400)
            known_keys = {column["key"] for column in unified_profile.sheet_definition(profile, role)["columns"]}
            unknown_keys = sorted(set(column_aliases) - known_keys)
            if unknown_keys:
                raise BusinessError(
                    "SCENARIO_SCHEMA_COLUMN_UNKNOWN",
                    f"Sheet“{role}”覆盖了不存在的列：" + "、".join(unknown_keys),
                    status_code=400,
                    details={"sheet_role": role, "column_keys": unknown_keys},
                )
            normalized_columns: dict[str, list[str]] = {}
            for key, aliases in column_aliases.items():
                if not isinstance(aliases, list) or any(not isinstance(item, str) or not item.strip() for item in aliases):
                    raise BusinessError("SCENARIO_SCHEMA_COLUMN_ALIASES_INVALID", f"Sheet“{role}”列“{key}”的别名必须是文本数组", status_code=400)
                normalized_columns[key] = list(dict.fromkeys(item.strip() for item in aliases))
            owner_by_label: dict[str, str] = {}
            for column in unified_profile.sheet_definition(profile, role)["columns"]:
                key = column["key"]
                for label in (column["name"], *(column.get("aliases") or []), *normalized_columns.get(key, [])):
                    normalized_label = str(label).strip()
                    owner = owner_by_label.get(normalized_label)
                    if owner is not None and owner != key:
                        raise BusinessError(
                            "SCENARIO_SCHEMA_COLUMN_ALIAS_CONFLICT",
                            f"Sheet“{role}”的列名“{normalized_label}”同时指向“{owner}”与“{key}”",
                            status_code=400,
                            details={"sheet_role": role, "alias": normalized_label, "columns": sorted({owner, key})},
                        )
                    owner_by_label[normalized_label] = key
            override["column_aliases"] = normalized_columns
        normalized_overrides[role] = override
    instruction_name = unified_profile.instruction_sheet_name(profile)
    name_owners: dict[str, str] = {instruction_name.casefold(): "填写说明"}
    for sheet in profile["sheets"]:
        role = sheet["sheet_role"]
        override = normalized_overrides.get(role, {})
        names = [
            override.get("display_name", sheet["display_name"]),
            *(sheet.get("aliases") or []),
            *(override.get("aliases") or []),
        ]
        for name in names:
            if not name or len(name) > 31 or any(char in name for char in "\\/*?:[]"):
                raise BusinessError(
                    "SCENARIO_SCHEMA_SHEET_NAME_INVALID",
                    f"Sheet“{role}”的名称或别名“{name}”不是有效的 Excel 工作表名",
                    status_code=400,
                    details={"sheet_role": role, "sheet_name": name},
                )
            owner = name_owners.setdefault(name.casefold(), role)
            if owner != role:
                raise BusinessError(
                    "SCENARIO_SCHEMA_SHEET_NAME_CONFLICT",
                    f"Sheet 名称或别名“{name}”同时指向“{owner}”与“{role}”",
                    status_code=400,
                    details={"sheet_name": name, "sheet_roles": [owner, role]},
                )
    return {
        "schema_key": schema_key.strip(),
        "schema_version": schema_version.strip(),
        "profile_version": profile_version,
        "sheet_overrides": normalized_overrides,
    }


def effective_profile(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """将场景 schema 转成一次性的内部 Profile 视图。"""
    normalized = normalize_schema(schema)
    profile = deepcopy(unified_profile.load_unified_profile())
    for role, override in normalized["sheet_overrides"].items():
        definition = unified_profile.sheet_definition(profile, role)
        if "display_name" in override:
            definition["display_name"] = override["display_name"]
        if "aliases" in override:
            definition["aliases"] = list(dict.fromkeys([*definition.get("aliases", []), *override["aliases"]]))
        aliases = override.get("column_aliases", {})
        for column in definition["columns"]:
            extra = aliases.get(column["key"])
            if extra:
                column["aliases"] = list(dict.fromkeys([*column.get("aliases", []), *extra]))
    return profile


def identity(schema: dict[str, Any] | None = None) -> dict[str, str]:
    normalized = normalize_schema(schema)
    return {"schema_key": normalized["schema_key"], "schema_version": normalized["schema_version"]}
