"""字段抽取策略及结构化值契约。

Excel 只声明业务字段类型。普通文本与确定性类型由系统自动选择策略；只有需要
多属性结果的字段才由场景或方案显式声明 STRUCTURED，并提供受支持的 JSON Schema。
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import BusinessError


RAW_TEXT = "RAW_TEXT"
DETERMINISTIC = "DETERMINISTIC"
STRUCTURED = "STRUCTURED"
EXTRACTION_STRATEGIES = frozenset({RAW_TEXT, DETERMINISTIC, STRUCTURED})

_RAW_VALUE_TYPES = frozenset({"STRING", "CLAUSE", "DECIMAL_CN"})
_DETERMINISTIC_VALUE_TYPES = frozenset({"DECIMAL", "DATE", "BOOLEAN"})
_STRUCTURED_VALUE_TYPES = frozenset({"STRING", "CLAUSE"})
_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "title",
        "description",
    }
)


def default_extraction_strategy(value_type: str) -> str:
    return RAW_TEXT if value_type in _RAW_VALUE_TYPES else DETERMINISTIC


def strategy_of(field: dict[str, Any]) -> str:
    value = field.get("extraction_strategy")
    return str(value) if isinstance(value, str) and value else default_extraction_strategy(str(field.get("value_type") or ""))


def validate_field_strategy(
    *,
    value_type: str,
    extraction_strategy: str | None,
    structure_schema: Any,
    field_name: str,
) -> tuple[str, dict[str, Any] | None]:
    strategy = extraction_strategy or default_extraction_strategy(value_type)
    if strategy not in EXTRACTION_STRATEGIES:
        raise BusinessError(
            "FIELD_EXTRACTION_STRATEGY_INVALID",
            f"字段“{field_name}”的抽取策略不受支持",
            details={"strategy": strategy, "allowed": sorted(EXTRACTION_STRATEGIES)},
        )
    compatible_types = {
        RAW_TEXT: _RAW_VALUE_TYPES,
        DETERMINISTIC: _DETERMINISTIC_VALUE_TYPES,
        STRUCTURED: _STRUCTURED_VALUE_TYPES,
    }[strategy]
    if value_type not in compatible_types:
        raise BusinessError(
            "FIELD_EXTRACTION_STRATEGY_TYPE_MISMATCH",
            f"字段“{field_name}”的数据类型与抽取策略不匹配",
            details={
                "value_type": value_type,
                "strategy": strategy,
                "allowed_value_types": sorted(compatible_types),
            },
        )
    if strategy != STRUCTURED:
        if structure_schema not in (None, {}):
            raise BusinessError(
                "FIELD_STRUCTURE_SCHEMA_UNEXPECTED",
                f"字段“{field_name}”不是结构化抽取，不能配置结构 Schema",
            )
        return strategy, None
    if not isinstance(structure_schema, dict) or not structure_schema:
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_REQUIRED",
            f"字段“{field_name}”使用结构化抽取时必须配置结构 Schema",
        )
    _assert_schema(structure_schema, path="$", field_name=field_name)
    if structure_schema.get("type") != "object":
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_ROOT_INVALID",
            f"字段“{field_name}”的结构 Schema 根类型必须是 object",
        )
    properties = structure_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_PROPERTIES_REQUIRED",
            f"字段“{field_name}”的结构 Schema 必须声明至少一个属性",
        )
    return strategy, structure_schema


def normalize_structured_value(value: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"结构化字段不是有效 JSON：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("结构化字段的值必须是 JSON 对象")
    _validate_value(value, schema, path="$")
    return value


def structured_property_keys(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    return {str(key) for key, item in value.items() if _has_evidence_value(item)}


def _has_evidence_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _assert_schema(schema: Any, *, path: str, field_name: str) -> None:
    if not isinstance(schema, dict):
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_INVALID",
            f"字段“{field_name}”的结构 Schema 在 {path} 处不是对象",
        )
    unsupported = sorted(set(schema) - _SCHEMA_KEYS)
    if unsupported:
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_UNSUPPORTED",
            f"字段“{field_name}”的结构 Schema 使用了尚未实现的关键字：{'、'.join(unsupported)}",
            details={"path": path, "unsupported": unsupported},
        )
    declared = schema.get("type")
    declared_types = [declared] if isinstance(declared, str) else declared
    if (
        not isinstance(declared_types, list)
        or not declared_types
        or any(not isinstance(item, str) or item not in _JSON_TYPES for item in declared_types)
    ):
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_TYPE_INVALID",
            f"字段“{field_name}”的结构 Schema 在 {path} 处缺少有效 type",
            details={"path": path, "type": declared},
        )
    properties = schema.get("properties")
    if properties is not None:
        if "object" not in declared_types or not isinstance(properties, dict):
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 properties 无效")
        for key, child in properties.items():
            if not isinstance(key, str) or not key or len(key) > 255:
                raise BusinessError(
                    "FIELD_STRUCTURE_SCHEMA_INVALID",
                    f"字段“{field_name}”在 {path} 的属性名必须是 1 到 255 个字符",
                )
            _assert_schema(child, path=f"{path}.{key}", field_name=field_name)
    required = schema.get("required")
    if required is not None:
        if (
            "object" not in declared_types
            or not isinstance(required, list)
            or any(not isinstance(key, str) or not key for key in required)
            or len(required) != len(set(required))
        ):
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 required 无效")
        unknown = sorted(set(required) - set(properties or {}))
        if unknown:
            raise BusinessError(
                "FIELD_STRUCTURE_SCHEMA_INVALID",
                f"字段“{field_name}”在 {path} 的必填属性未在 properties 中声明",
                details={"path": path, "properties": unknown},
            )
    if "items" in schema:
        if "array" not in declared_types:
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 items 只能用于 array")
        _assert_schema(schema["items"], path=f"{path}[]", field_name=field_name)
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 additionalProperties 必须是布尔值")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 enum 必须是非空数组")
    for minimum_key, maximum_key, allowed_type in (
        ("minItems", "maxItems", "array"),
        ("minLength", "maxLength", "string"),
    ):
        minimum = schema.get(minimum_key)
        maximum = schema.get(maximum_key)
        if minimum is not None and (not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0):
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 {minimum_key} 无效")
        if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0):
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 {maximum_key} 无效")
        if (minimum is not None or maximum is not None) and allowed_type not in declared_types:
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的长度限制与 type 不匹配")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的最小值不能大于最大值")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    for key, value in (("minimum", minimum), ("maximum", maximum)):
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 {key} 无效")
    if (minimum is not None or maximum is not None) and not ({"number", "integer"} & set(declared_types)):
        raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的数值范围与 type 不匹配")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise BusinessError("FIELD_STRUCTURE_SCHEMA_INVALID", f"字段“{field_name}”在 {path} 的 minimum 不能大于 maximum")


def _matches_type(value: Any, declared: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return checks[declared](value)


def _validate_value(value: Any, schema: dict[str, Any], *, path: str) -> None:
    declared = schema["type"]
    declared_types = [declared] if isinstance(declared, str) else declared
    if not any(_matches_type(value, item) for item in declared_types):
        raise ValueError(f"{path} 的类型应为 {' 或 '.join(declared_types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 的值不在允许范围内")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        missing = [key for key in schema.get("required") or [] if key not in value]
        if missing:
            raise ValueError(f"{path} 缺少必填属性：{'、'.join(missing)}")
        if schema.get("additionalProperties", False) is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise ValueError(f"{path} 包含未声明属性：{'、'.join(unexpected)}")
        for key, item in value.items():
            if key in properties:
                _validate_value(item, properties[key], path=f"{path}.{key}")
    if isinstance(value, list):
        minimum, maximum = schema.get("minItems"), schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} 至少需要 {minimum} 项")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} 最多允许 {maximum} 项")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                _validate_value(item, schema["items"], path=f"{path}[{index}]")
    if isinstance(value, str):
        minimum, maximum = schema.get("minLength"), schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} 长度不能小于 {minimum}")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} 长度不能大于 {maximum}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path} 不能小于 {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path} 不能大于 {maximum}")
