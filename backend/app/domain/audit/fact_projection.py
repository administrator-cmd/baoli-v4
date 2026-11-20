"""统一事实的 JSON 编码、字段取值投影和规则输入解析。"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.common.exceptions import BusinessError
from app.constants.execution import FACT_KIND_DERIVED, FACT_KIND_EXTRACTED
from app.constants.status import EXECUTABLE_FIELD_STATUSES
from app.domain.configuration import field_strategy

# 被显式编码的标量类型。`kind` 取值即这几个，解码时据此还原。
ENCODED_KINDS = ("decimal", "date", "datetime")
_ENVELOPE_KEYS = frozenset({"kind", "value"})
_ESCAPED_MAPPING_KIND = "mapping"

def recognized_scopes(output: dict[str, Any]) -> dict[str, str]:
    """字段事实和规则详情均使用本次产出的识别口径，不用配置值补缺。"""
    mapping = {
        "actual_period_scope": "recognized_period",
        "actual_statement_scope": "recognized_statement_scope",
        "actual_entity_scope": "recognized_entity_scope",
        "actual_unit": "recognized_unit",
    }
    return {target: value.strip() for target, source in mapping.items()
            if isinstance(value := output.get(source), str) and value.strip()}


def encode_value(value: Any) -> Any:
    """把一个取值投影成 JSON 可表达、且可无损还原的形式。

    模型上下文与 JSON 响应使用此投影，解码由 `decode_value` 负责。
    业务列 CSV 使用 task_value_csv 的独立类型列，不保存这里的 JSON 封套。
    无法投影的类型显式报错——静默丢弃会让「这个维度被丢掉了」与
    「这个维度取不到值」变得无法区分。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BusinessError(
                "FACT_VALUE_UNENCODABLE",
                "事实取值包含非有限数，无法冻结为有效 JSON",
                status_code=500,
                details={"value_type": type(value).__name__},
            )
        # STRUCTURED 的 JSON number 保持数值类型；金额仍走下面的 Decimal 编码。
        return value
    if isinstance(value, Decimal):
        # 十进制字符串：保留精度与原值，绝不经过 float。
        return {"kind": "decimal", "value": format(value, "f")}
    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise BusinessError("FACT_VALUE_UNENCODABLE", "结构化事实的对象键必须为字符串", status_code=500)
        encoded = {key: encode_value(item) for key, item in value.items()}
        if set(value) == _ENVELOPE_KEYS and isinstance(value.get("kind"), str) and value["kind"] in (*ENCODED_KINDS, _ESCAPED_MAPPING_KIND):
            # 转义负载使用键值对列表，与业务对象和标量封套明确区分。
            return {"kind": _ESCAPED_MAPPING_KIND, "value": [[key, item] for key, item in encoded.items()]}
        return encoded
    raise BusinessError(
        "FACT_VALUE_UNENCODABLE",
        "事实取值无法投影成 JSON 安全形式，规则上下文未写入",
        status_code=500,
        details={"value_type": type(value).__name__},
    )


def decode_value(value: Any) -> Any:
    """`encode_value` 的逆运算：还原成 Python 取值。

    标量封套按声明类型还原；与保留封套同形的业务字典通过 mapping 封套转义。
    转义层只解码成员，不把原始业务字典再次当作标量封套。
    """
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if isinstance(value, dict):
        if set(value) == _ENVELOPE_KEYS and value.get("kind") == _ESCAPED_MAPPING_KIND:
            raw = value["value"]
            if not isinstance(raw, list):
                raise BusinessError("FACT_VALUE_INVALID", "结构化事实的转义对象无效", status_code=500)
            decoded: dict[str, Any] = {}
            for pair in raw:
                if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                    raise BusinessError("FACT_VALUE_INVALID", "结构化事实的转义键值对无效", status_code=500)
                if pair[0] in decoded:
                    raise BusinessError("FACT_VALUE_INVALID", "结构化事实的转义对象包含重复键", status_code=500)
                decoded[pair[0]] = decode_value(pair[1])
            return decoded
        if set(value) == _ENVELOPE_KEYS and isinstance(value.get("kind"), str) and value["kind"] in ENCODED_KINDS:
            return _decode_scalar(str(value["kind"]), value.get("value"))
        return {key: decode_value(item) for key, item in value.items()}
    return value


def _decode_scalar(kind: str, raw: Any) -> Any:
    """解码一个被显式编码的标量。

    编码内容损坏（不是字符串、或不是合法数值/日期）时显式报错：
    用 `None` 顶替会让「解析失败」看起来像「这次没有取值」。
    """
    if not isinstance(raw, str):
        raise BusinessError(
        "FACT_VALUE_INVALID",
            "规则上下文里的取值不是可解析的文本，无法还原",
            status_code=500,
            details={"kind": kind},
        )
    if kind == "decimal":
        try:
            return Decimal(raw)
        except ArithmeticError as exc:
            raise BusinessError(
                "FACT_VALUE_INVALID",
                "规则上下文里的十进制取值无法还原",
                status_code=500,
                details={"kind": kind},
            ) from exc
    if kind == "date":
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise BusinessError(
                "FACT_VALUE_INVALID",
                "规则上下文里的日期取值无法还原",
                status_code=500,
                details={"kind": kind},
            ) from exc
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BusinessError(
            "FACT_VALUE_INVALID",
            "规则上下文里的时间取值无法还原",
            status_code=500,
            details={"kind": kind},
        ) from exc














def fact_identity(item: Any) -> tuple[Any, ...]:
    """业务编号必须连同材料、期间、报表与主体口径定位，不能单独充当字段键。"""
    get = item.get if isinstance(item, dict) else lambda key: getattr(item, key, None)
    return tuple(get(key) for key in ("fact_code", "material_key", "period_scope", "statement_scope", "entity_scope"))


def field_fact_identity(field: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(field.get("source_ref") or field["field_key"]), field["material_key"],
        field_strategy.normalize_field_scope(field, "period_scope", status_code=500),
        field_strategy.normalize_field_scope(field, "statement_scope", status_code=500),
        str(field.get("entity_scope") or "SELF"),
    )


def field_identity_index(definitions: dict[str, dict[str, Any]]) -> dict[tuple[Any, ...], str]:
    index = {}
    for key, field in definitions.items():
        identity = field_fact_identity(field)
        if identity in index:
            raise BusinessError("TASK_FIELD_IDENTITY_AMBIGUOUS", "多个字段声明使用相同的完整事实身份", status_code=500,
                                details={"field_keys": [index[identity], key], "identity": identity})
        index[identity] = key
    return index


def resolve_rule_inputs(
    facts: list[dict[str, Any]],
    rule_inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从输入事实里按身份引用解析一条规则的输入。

    输出形状与 `services/audit/facts._field_inputs` 相同（这是两者能互换的前提），区别只有
    取数来源：这里只读规则上下文，不查当前数据。规则上下文里没有的事实如实返回 `NOT_EXECUTED`
    与空取值——缺输入是事实，不由这里补默认值。

    字段事实已按完整口径解析为内部 field_key；计算事实使用 result_code。

    `rule_inputs` 是规则声明的 (field_key, material_key) 引用清单，顺序即调用顺序。
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in facts:
        key = _identity_of_fact(item)
        if key[0]:
            if key in index:
                raise BusinessError("RULE_INPUT_IDENTITY_AMBIGUOUS", "规则输入对应多个事实，无法确定实际取值", status_code=500,
                                    details={"field_key": key[0], "material_key": key[1]})
            index[key] = item
    resolved: list[dict[str, Any]] = []
    for item in rule_inputs:
        field_key = str(item.get("field_key") or "")
        material_key = item.get("material_key")
        fact = index.get((field_key, material_key))
        common = {
            "field_key": field_key,
            "material_key": material_key,
            # 展示用的中文名随输入一起传：比较面板据此写明左右两侧分别是谁。
            "field_name": (fact or {}).get("field_name"),
            "material_name": (fact or {}).get("material_name"),
            "source_files": list((fact or {}).get("source_files") or []),
            # 事实记录 id 供诊断按历史版本回读取值，不在结果里再复制一份业务值。
            "fact_id": (fact or {}).get("fact_id"),
            "value_type": (fact or {}).get("value_type"),
            "unit": (fact or {}).get("unit"),
            "currency": (fact or {}).get("currency"),
            "actual_period_scope": (fact or {}).get("actual_period_scope"),
            "actual_statement_scope": (fact or {}).get("actual_statement_scope"),
            "actual_entity_scope": (fact or {}).get("actual_entity_scope"),
            "actual_unit": (fact or {}).get("actual_unit"),
            "material_state": (fact or {}).get("material_state"),
            **{key: (fact or {}).get(key) for key in ("period_scope", "statement_scope", "entity_scope", "origin")},
        }
        if fact is None:
            resolved.append(
                {
                    **common,
                    "status": "NOT_EXECUTED",
                    "raw_value": None,
                    "normalized_value": None,
                    "evidence": [],
                }
            )
            continue
        resolved.append(
            {
                **common,
                "status": fact.get("status"),
                # 取值按编码还原成 Python 值：金额是 Decimal（绝不经过 float），
                # 日期是 date。工具与算子读到的是同一形状，不因事实来自哪张表而不同。
                "raw_value": decode_value(fact.get("raw_value")),
                "normalized_value": decode_value(fact.get("normalized_value")),
                "evidence": list(fact.get("evidence") or []),
                **{key: fact[key] for key in ("source_field_value_id", "source_rule_result_id") if key in fact},
            }
        )
    return resolved


def _identity_of_fact(item: dict[str, Any]) -> tuple[str, Any]:
    """字段使用已解析的内部身份；计算结果使用其 result_code。"""
    return (
        str(item.get("field_key") or (item.get("fact_code") if item.get("fact_kind") == FACT_KIND_DERIVED else "") or ""),
        item.get("material_key"),
    )


def declared_inputs_of(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """规则声明的输入引用清单。只保留身份键，不把规则里的其它配置带出去。"""
    return [
        {"field_key": item.get("field_key"), "material_key": item.get("material_key")}
        for item in (rule.get("inputs") or [])
        if isinstance(item, dict) and item.get("field_key")
    ]


def facts_of_task_fact_rows(
    rows: list[Any], *, values: dict[int, dict[str, Any]],
    field_definitions: dict[str, dict[str, Any]] | None = None,
    material_states: dict[str, Any] | None = None,
    raw_values: dict[int, Any] | None = None,
    material_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """把事实索引行与它们的取值投影为模型和工具共用的输入。

    `values` 是 `(记录 id → 取值)`：表里只有身份、口径与当前位，取值来自任务 CSV。
    取值已被解码回 Python 类型（金额是 Decimal、日期是 date），这里不重复转换。
    字段与材料的中文名只用于展示（比较双方的来源说明），不参与任何业务判断。
    """
    index = field_identity_index(field_definitions or {})
    states = material_states or {}
    facts = []
    for row in rows:
        if row.id not in values:
            raise BusinessError("TASK_VALUE_RECORD_MISSING", "事实索引存在，但任务 CSV 缺少对应取值", status_code=500,
                                details={"record_type": "TaskFact", "record_id": row.id})
        payload = values[row.id]
        normalized_value = payload.get("value")
        executable = (row.status in EXECUTABLE_FIELD_STATUSES if row.fact_kind == FACT_KIND_EXTRACTED
                      else row.fact_kind == FACT_KIND_DERIVED and row.status == "COMPUTED")
        field_key = str(row.fact_code) if row.fact_kind == FACT_KIND_DERIVED else index.get(fact_identity(row))
        if field_key is None:
            raise BusinessError("TASK_FACT_FIELD_UNDECLARED", "字段事实的完整口径不属于本次字段声明", status_code=500,
                                details={"fact_id": row.id, "identity": fact_identity(row)})
        facts.append({
            "fact_id": row.id,
            "fact_code": row.fact_code, "field_key": field_key, "fact_kind": row.fact_kind,
            "material_key": row.material_key, "period_scope": row.period_scope,
            "statement_scope": row.statement_scope, "entity_scope": row.entity_scope,
            "revision_no": row.revision_no, "value_type": row.value_type,
            "field_name": ((field_definitions or {}).get(field_key) or {}).get("name"),
            "material_name": (material_names or {}).get(row.material_key),
            "unit": payload.get("unit"), "currency": payload.get("currency"), "status": row.status,
            "actual_period_scope": payload.get("actual_period_scope"),
            "actual_statement_scope": payload.get("actual_statement_scope"),
            "actual_entity_scope": payload.get("actual_entity_scope"),
            "actual_unit": payload.get("actual_unit"),
            "material_state": states.get(row.material_key) if row.material_key else None,
            "raw_value": encode_value((raw_values or {}).get(row.id) if executable else None),
            "normalized_value": encode_value(normalized_value if executable else None),
            "evidence": [],
        })
    return facts


def canonical_fact_value(value: Any, definition: dict[str, Any]) -> Any:
    """仅转换接口数据类型；业务计算、单位和口径由模型负责。"""
    if value is None:
        return None
    kind = definition.get("value_type") or definition.get("result_type") or "STRING"
    try:
        if definition.get("extraction_strategy") == field_strategy.STRUCTURED:
            return field_strategy.normalize_structured_value(value, definition["structure_schema"])
        if kind in {"DECIMAL", "AMOUNT", "PERCENT", "INTEGER"}:
            if isinstance(value, bool):
                raise ValueError("数值不能是布尔值")
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError("数值必须有限")
            if kind == "INTEGER" and number != number.to_integral_value():
                raise ValueError("INTEGER 产出必须为整数")
            return format(number, "f")
        if kind == "DATE":
            return date.fromisoformat(str(value)).isoformat()
        if kind == "BOOLEAN":
            if not isinstance(value, bool):
                raise ValueError("BOOLEAN 产出必须为 JSON 布尔值")
            return value
        return value
    except (InvalidOperation, ValueError) as exc:
        raise BusinessError("FACT_VALUE_INVALID", f"事实取值不符合声明的数据类型：{exc}") from exc




def canonical_field_value(value: Any, field_definition: dict[str, Any]) -> Any:
    """把候选层与统一事实层的同一字段值归一成可直接比较的业务值。"""
    if value is None:
        return None
    if field_strategy.strategy_of(field_definition) == field_strategy.STRUCTURED:
        schema = field_definition.get("structure_schema")
        if not isinstance(schema, dict):
            raise BusinessError(
                "TASK_FACT_STRUCTURE_SCHEMA_MISSING",
                "结构化字段缺少字段 Schema，无法比较候选值与统一事实",
                status_code=500,
                details={"field_key": field_definition.get("field_key")},
            )
        try:
            return field_strategy.normalize_structured_value(value, schema)
        except ValueError as exc:
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "字段候选值不符合冻结的结构 Schema",
                status_code=500,
                details={"field_key": field_definition.get("field_key")},
            ) from exc
    value_type = str(field_definition.get("value_type") or "")
    if value_type in {"DECIMAL", "AMOUNT"}:
        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "数值字段候选值不是有效十进制数",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            ) from exc
        if not decimal_value.is_finite():
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "数值字段候选值必须是有限十进制数",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            )
        decimal_text = format(decimal_value, "f")
        if "." in decimal_text:
            decimal_text = decimal_text.rstrip("0").rstrip(".")
        return "0" if decimal_text == "-0" else decimal_text
    if value_type == "DATE":
        if type(value) is date:
            return value.isoformat()
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError as exc:
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "日期字段候选值不是有效 ISO 日期",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            ) from exc
    if value_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "布尔字段候选值不是布尔值",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            )
        return value
    return value.strip() if isinstance(value, str) else value


