"""场景配置的版本身份；业务字段和规则由 LLM 生成的 config_json 定义。"""

from typing import Any

from app.common.exceptions import BusinessError
from app.domain.node_profile import NODE_PROFILE_VERSION

DEFAULT_SCHEMA_KEY = "scenario_json"
DEFAULT_SCHEMA_VERSION = "1"


def default_schema() -> dict[str, Any]:
    return {
        "schema_key": DEFAULT_SCHEMA_KEY,
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "profile_version": NODE_PROFILE_VERSION,
    }


def normalize_schema(value: Any | None) -> dict[str, Any]:
    """核对场景版本身份；不定义或匹配 Excel 工作表及业务列。"""
    if value is None:
        return default_schema()
    if not isinstance(value, dict):
        raise BusinessError("SCENARIO_SCHEMA_INVALID", "场景 schema 必须是 JSON 对象", status_code=400)
    # 已存场景可以保留空的旧字段；非空模板覆盖不能静默忽略。
    unknown = sorted(set(value) - {"schema_key", "schema_version", "profile_version", "sheet_overrides"})
    if unknown:
        raise BusinessError(
            "SCENARIO_SCHEMA_PROPERTY_UNKNOWN", "场景 schema 包含不支持的属性：" + "、".join(unknown),
            status_code=400,
        )
    if "sheet_overrides" in value and value["sheet_overrides"] != {}:
        raise BusinessError(
            "SCENARIO_SCHEMA_TEMPLATE_REMOVED", "固定模板的表名和列别名覆盖已移除，请使用实际规则库重新定义场景配置",
            status_code=400,
        )
    schema_key, schema_version = value.get("schema_key"), value.get("schema_version")
    if not isinstance(schema_key, str) or not schema_key.strip():
        raise BusinessError("SCENARIO_SCHEMA_KEY_REQUIRED", "场景 schema 缺少 schema_key", status_code=400)
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise BusinessError("SCENARIO_SCHEMA_VERSION_REQUIRED", "场景 schema 缺少 schema_version", status_code=400)
    profile_version = value.get("profile_version", NODE_PROFILE_VERSION)
    return {
        "schema_key": schema_key.strip(), "schema_version": schema_version.strip(),
        "profile_version": profile_version,
    }


def identity(schema: dict[str, Any] | None = None) -> dict[str, str]:
    normalized = normalize_schema(schema)
    return {"schema_key": normalized["schema_key"], "schema_version": normalized["schema_version"]}
