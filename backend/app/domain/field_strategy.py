"""字段抽取策略及结构化值契约。

Excel 只声明业务字段类型。普通字段默认由系统选择原文或确定性策略；只有需要
多属性结果的字段才显式声明 STRUCTURED，并提供 JSON Schema。
普通字段由执行端按 value_type 处理原文，RAW_TEXT 与 DETERMINISTIC 不限制类型配对；
STRUCTURED 使用声明的 Schema 处理 JSON 对象，与普通字段分开。
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from ..common.exceptions import BusinessError


RAW_TEXT = "RAW_TEXT"
DETERMINISTIC = "DETERMINISTIC"
STRUCTURED = "STRUCTURED"
EXTRACTION_STRATEGIES = frozenset({RAW_TEXT, DETERMINISTIC, STRUCTURED})

_TEXT_VALUE_TYPES = frozenset({"STRING", "CLAUSE", "DECIMAL_CN"})


def default_extraction_strategy(value_type: str) -> str:
    return RAW_TEXT if value_type in _TEXT_VALUE_TYPES else DETERMINISTIC


def contract_payload() -> dict[str, Any]:
    """结构化字段使用标准 JSON Schema，实际值保持 JSON 对象。"""
    return {
        "strategies": {
            RAW_TEXT: "LLM 抽取原文，后端按字段 value_type 形成规范值；不限制类型配对",
            DETERMINISTIC: "LLM 抽取原文，后端按字段 value_type 形成规范值；不限制类型配对",
            STRUCTURED: "LLM 返回符合 structure_schema 的 JSON 对象及属性证据；不限制类型配对",
        },
        "structured_root_type": "object",
        "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
    }


def strategy_of(field: dict[str, Any]) -> str:
    value = field.get("extraction_strategy")
    return str(value) if isinstance(value, str) and value else default_extraction_strategy(str(field.get("value_type") or ""))


def validate_field_strategy(
    *,
    value_type: str,
    extraction_strategy: str | None,
    structure_schema: Any,
    field_name: str,
    enabled: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    strategy = extraction_strategy or default_extraction_strategy(value_type)
    if not enabled:
        return strategy, structure_schema
    if strategy not in EXTRACTION_STRATEGIES:
        raise BusinessError(
            "FIELD_EXTRACTION_STRATEGY_INVALID",
            f"字段“{field_name}”的抽取策略不受支持",
            details={"strategy": strategy, "allowed": sorted(EXTRACTION_STRATEGIES)},
        )
    if strategy != STRUCTURED:
        return strategy, structure_schema
    if not isinstance(structure_schema, dict):
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_REQUIRED",
            f"字段“{field_name}”使用结构化抽取时必须配置结构 Schema",
        )
    try:
        json.dumps(structure_schema, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_INVALID",
            f"字段“{field_name}”的结构 Schema 不是有效 JSON：{exc}",
        ) from exc
    try:
        Draft202012Validator.check_schema(structure_schema)
    except SchemaError as exc:
        raise BusinessError(
            "FIELD_STRUCTURE_SCHEMA_INVALID",
            f"字段“{field_name}”的 JSON Schema 无效：{exc.message}",
        ) from exc
    return strategy, structure_schema


def decode_structured_value(value: Any) -> dict[str, Any]:
    """还原已存 JSON 对象，不重复审查字段业务结构。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"结构化字段不是有效 JSON：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("结构化字段的值必须是 JSON 对象")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"结构化字段不是有效 JSON：{exc}") from exc
    return value


def normalize_structured_value(value: Any, schema: dict[str, Any]) -> dict[str, Any]:
    value = decode_structured_value(value)
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(f"{path}：{exc.message}") from exc
    return value
