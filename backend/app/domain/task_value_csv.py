"""任务取值的业务表结构与无损单元格编码。

业务证据是少量有明确结构的引用对象，保存在主记录的 JSON 单元格中；
其它结构化业务值仍按既有类型规则处理，不能借用证据列逃避校验。
"""

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

IDENTITY_COLUMNS = ("记录类型", "记录ID", "写入批次")
CONTEXT_COLUMNS = (
    "业务类别", "业务编号", "名称", "材料名称", "期间", "报表口径", "主体范围",
    "声明类型", "取值说明", "修订号", "写入时状态", "取值来源", "执行批次ID", "字段键", "材料键", "任务编号",
)

# payload 属性 → 人工可直接维护的业务列；每个值都有独立的存储类型列。
VALUE_COLUMNS = {
    "raw_value": "原文", "normalized_value_json": "候选值", "value": "有效值",
    "unit": "单位", "currency": "币种", "status": "结论状态", "message": "结论原因",
    "risk_level": "风险等级", "action": "风险处置", "next_action": "处理建议",
    "evidence": "原文依据",
    "presentation_version": "展示协议版本",
}
MAIN_TABLES = {
    "values.csv": {key: VALUE_COLUMNS[key] for key in ("raw_value", "normalized_value_json", "value", "unit", "currency", "evidence")},
    "rule_results.csv": {key: VALUE_COLUMNS[key] for key in ("status", "message", "risk_level", "action", "next_action", "evidence", "presentation_version")},
}

# 证据引用有固定的业务形状，作为一个 JSON 单元格保存，避免为每个引用、属性和
# 数组节点生成明细行。其它 object/array 仍使用 value_details.csv 的严格树编码。
JSON_COLUMNS = frozenset({"evidence"})
# 一条规则的多值业务项写在同一张明细表，用“明细类别”区分。
ITEM_FILE = "rule_items.csv"
ITEM_CATEGORY = "明细类别"
ITEM_TABLES = {
    "basis": {"label": "核查项", "value": "实际取值", "explanation": "依据说明"},
    "outputs": {"code": "产出编号", "raw_value": "原文", "value": "产出值", "field_status": "字段状态"},
    "missing_items": {"item": "缺失事项"},
}
ITEM_CONTEXT_COLUMNS = ("业务编号", "名称", "材料名称")
DETAIL_COLUMNS = (*ITEM_CONTEXT_COLUMNS, "业务项目", "属性名", "数组序号", "值类型", "值", "所属项目", "节点编号", "上级节点", *IDENTITY_COLUMNS)
EXTRA_LABELS = {
    "tool_findings": "工具核查明细", "extracted_values": "抽取字段明细",
    "calculation_definition": "计算定义", "calculation_steps": "计算过程",
    "source_rules": "风险来源规则", "risk_condition": "风险触发条件", "report": "报告正文",
}


def typed_columns(mapping: dict[str, str]) -> tuple[str, ...]:
    return tuple(column for label in mapping.values() for column in (label, f"{label}类型"))


def _union_item_columns() -> tuple[str, ...]:
    """明细类别共用的类型列并集；同名不同义的列按各自标签分别保留。"""
    columns: list[str] = []
    for mapping in ITEM_TABLES.values():
        for column in typed_columns(mapping):
            if column not in columns:
                columns.append(column)
    return tuple(columns)


_TYPED_ITEM_COLUMNS = _union_item_columns()


TABLE_COLUMNS = {
    "values.csv": (
        *CONTEXT_COLUMNS[:-4], *typed_columns(MAIN_TABLES["values.csv"]),
        "补充属性数", *CONTEXT_COLUMNS[-4:], *IDENTITY_COLUMNS,
    ),
    "rule_results.csv": (
        *ITEM_CONTEXT_COLUMNS, "规则类型", "审核要求", "计算公式", *typed_columns(MAIN_TABLES["rule_results.csv"]),
        "执行批次ID", "任务编号",
        "判断依据数", "实际产出数", "缺失事项数", "补充属性数", *IDENTITY_COLUMNS,
    ),
    "rule_items.csv": (
        *ITEM_CONTEXT_COLUMNS, "明细序号", ITEM_CATEGORY,
        "字段名称", "产出材料名称", "产出单位", "产出币种",
        *_TYPED_ITEM_COLUMNS, "补充属性数", "字段键", "产出材料键", *IDENTITY_COLUMNS,
    ),
    "value_details.csv": DETAIL_COLUMNS,
}
COUNT_COLUMNS = {"basis": "判断依据数", "outputs": "实际产出数", "missing_items": "缺失事项数"}


def _scalar(value: Any) -> tuple[str, str]:
    if value is None:
        return "null", ""
    if isinstance(value, str):
        return "text", value
    if isinstance(value, bool):
        return "boolean", "true" if value else "false"
    if isinstance(value, int):
        return "integer", str(value)
    if isinstance(value, (float, Decimal)):
        if not (value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)):
            raise ValueError("任务取值不能包含非有限数")
        return ("decimal", format(value, "f")) if isinstance(value, Decimal) else ("float", repr(value))
    if isinstance(value, datetime):
        return "datetime", value.isoformat()
    if isinstance(value, date):
        return "date", value.isoformat()
    if isinstance(value, dict):
        return "object", ""
    if isinstance(value, (list, tuple)):
        return "array", ""
    raise TypeError(f"任务取值不支持类型：{type(value).__name__}")


def _parse_scalar(kind: str, text: str) -> Any:
    if kind == "text":
        return text
    if kind == "null" and not text:
        return None
    if kind == "boolean" and text in {"true", "false"}:
        return text == "true"
    parsers = {"integer": int, "float": float, "decimal": Decimal, "date": date.fromisoformat, "datetime": datetime.fromisoformat}
    if kind in parsers:
        value = parsers[kind](text)
        _scalar(value)  # 非有限数不能通过手工编辑混入业务值。
        return value
    raise ValueError(f"无法解析任务取值单元格：类型 {kind!r}，值 {text!r}")


class RecordWriter:
    def __init__(self, identity: dict[str, Any], context: dict[str, Any], tables: dict[str, list[dict[str, Any]]]):
        self.identity = identity
        self.context = context
        self.tables = tables

    def cell(self, row: dict[str, Any], label: str, value: Any, slot: str) -> None:
        if label == VALUE_COLUMNS["evidence"]:
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise TypeError("原文依据必须是对象列表")
            try:
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("原文依据不是可保存的 JSON 结构") from exc
            row[label], row[f"{label}类型"] = encoded, "json"
            return
        kind, text = _scalar(value)
        row[label], row[f"{label}类型"] = text, kind
        if kind in {"object", "array"}:
            self.tree(slot, value)

    def tree(self, slot: str, value: Any) -> None:
        rows = self.tables["value_details.csv"]
        next_id = 0

        def visit(item: Any, parent: int | str, key: str = "", index: int | str = "") -> None:
            nonlocal next_id
            node_id = next_id
            next_id += 1
            kind, text = _scalar(item)
            rows.append({
                **self.identity, **{name: self.context.get(name) for name in ITEM_CONTEXT_COLUMNS},
                "业务项目": EXTRA_LABELS.get(slot.removeprefix("补充/"), slot),
                "所属项目": slot, "节点编号": node_id, "上级节点": parent,
                "属性名": key, "数组序号": index, "值类型": kind, "值": text,
            })
            if kind == "object":
                for child_key, child in item.items():
                    visit(child, node_id, str(child_key))
            elif kind == "array":
                for child_index, child in enumerate(item):
                    visit(child, node_id, index=child_index)

        visit(value, "")

    def attributes(self, row: dict[str, Any], payload: dict[str, Any], mapping: dict[str, str], prefix: str) -> None:
        for key, label in mapping.items():
            if key in payload:
                self.cell(row, label, payload[key], f"{prefix}{label}")
        extras = {key: value for key, value in payload.items() if key not in mapping}
        row["补充属性数"] = len(extras)
        for key, value in extras.items():
            # 每个补充业务属性独立展开，未知属性也不会被丢弃。
            self.tree(f"{prefix}补充/{key}", value)

    def record(self, payload: dict[str, Any], output_definitions: dict[str, dict[str, Any]]) -> None:
        main_filename = "rule_results.csv" if self.identity["记录类型"] == "RuleResult" else "values.csv"
        row = {key: value for key, value in self.context.items() if key in TABLE_COLUMNS[main_filename]}
        row.update(self.identity)
        scalars = {key: value for key, value in payload.items() if key not in ITEM_TABLES}
        self.attributes(row, scalars, MAIN_TABLES[main_filename], "")
        for key, mapping in ITEM_TABLES.items():
            if key not in payload:
                continue
            items = payload[key]
            # 空白表示此记录没有该属性；0 表示明确的空列表。
            if not isinstance(items, list):
                raise TypeError(f"{key} 必须是列表")
            row[COUNT_COLUMNS[key]] = len(items)
            for index, item in enumerate(items):
                detail = {**self.identity, **{name: self.context.get(name) for name in ITEM_CONTEXT_COLUMNS},
                          "明细序号": index, ITEM_CATEGORY: key}
                values = {"item": item} if key == "missing_items" else item
                self.attributes(detail, values, mapping, f"{key}/{index}/")
                if key == "outputs":
                    definition = output_definitions.get(item["code"], {})
                    detail.update({"字段名称": definition.get("name"), "产出材料名称": definition.get("material_name"),
                                   "产出单位": definition.get("unit"), "产出币种": definition.get("currency"),
                                   "字段键": definition.get("field_key"), "产出材料键": definition.get("material_key")})
                self.tables[ITEM_FILE].append(detail)
        # 主记录最后追加，只有写完所有明细的批次才会被读取。
        self.tables[main_filename].append(row)


class RecordReader:
    def __init__(self, details: dict[str, list[dict[str, str]]]):
        self.details = details

    def tree(self, slot: str) -> Any:
        rows = self.details[slot]
        nodes = {int(row["节点编号"]): row for row in rows}
        if len(nodes) != len(rows):
            raise ValueError(f"结构化明细节点重复：{slot}")
        children: dict[int, list[int]] = {}
        for node_id, row in nodes.items():
            if row["上级节点"]:
                children.setdefault(int(row["上级节点"]), []).append(node_id)
        visited: set[int] = set()

        def build(node_id: int) -> Any:
            if node_id in visited:
                raise ValueError(f"结构化明细节点循环：{slot}")
            visited.add(node_id)
            row = nodes[node_id]
            kind = row["值类型"]
            child_ids = children.get(node_id, [])
            if kind in {"object", "array"} and row["值"]:
                raise ValueError(f"结构化节点的值须在子节点维护：{slot}")
            if kind == "object":
                keys = [nodes[child]["属性名"] for child in child_ids]
                if len(set(keys)) != len(keys):
                    raise ValueError(f"结构化明细属性重复：{slot}")
                return {nodes[child]["属性名"]: build(child) for child in child_ids}
            if kind == "array":
                ordered = sorted(child_ids, key=lambda child: int(nodes[child]["数组序号"]))
                if [int(nodes[child]["数组序号"]) for child in ordered] != list(range(len(ordered))):
                    raise ValueError(f"结构化数组序号不连续：{slot}")
                return [build(child) for child in ordered]
            if child_ids:
                raise ValueError(f"标量值不能挂接子节点：{slot}")
            return _parse_scalar(kind, row["值"])

        if nodes[0]["上级节点"]:
            raise ValueError(f"结构化明细根节点不能有上级：{slot}")
        result = build(0)
        if visited != nodes.keys():
            raise ValueError(f"结构化明细存在未关联节点：{slot}")
        return result

    def attributes(self, row: dict[str, str], mapping: dict[str, str], prefix: str) -> dict[str, Any]:
        output = {}
        for key, label in mapping.items():
            kind = row[f"{label}类型"]
            if not kind and row[label]:
                raise ValueError(f"单元格有值但缺少类型：{prefix}{label}")
            if kind:
                if key in JSON_COLUMNS:
                    if kind != "json":
                        raise ValueError(f"{prefix}{label} 必须使用 json 类型")
                    try:
                        value = json.loads(row[label])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{prefix}{label} 不是有效 JSON") from exc
                    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                        raise ValueError(f"{prefix}{label} 的结构必须是对象列表")
                    output[key] = value
                elif kind in {"object", "array"}:
                    if row[label]:
                        raise ValueError(f"结构化单元格的值须在明细表维护：{prefix}{label}")
                    value = self.tree(f"{prefix}{label}")
                    if _scalar(value)[0] != kind:
                        raise ValueError(f"结构化明细与单元格类型不符：{prefix}{label}")
                    output[key] = value
                else:
                    output[key] = _parse_scalar(kind, row[label])
        extra_prefix = f"{prefix}补充/"
        extras = [slot for slot in self.details if slot.startswith(extra_prefix)]
        if len(extras) != int(row["补充属性数"]):
            raise ValueError(f"补充业务属性缺失或多出：{prefix}")
        for slot in extras:
            key = slot.removeprefix(extra_prefix)
            if key in mapping:
                raise ValueError(f"补充业务属性与业务列重复：{key}")
            output[key] = self.tree(slot)
        return output
