"""按业务列保存取值及明细；数据库决定身份、当前版本与状态。

每次写入独立、不可变的六表批次；全部文件落盘后才更新数据库批次清单。
事务回滚只留下未引用文件，不会覆盖仍生效的业务值。
"""
from __future__ import annotations

from collections import defaultdict
import csv
import os
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..domain.fact_projection import fact_identity, field_fact_identity
from ..domain.task_value_csv import COUNT_COLUMNS, ITEM_TABLES, MAIN_TABLES, TABLE_COLUMNS, RecordReader, RecordWriter
from ..entity import AuditTask, RuleResult, TaskFact, TaskFieldValue
from .storage_service import resolve_storage_path

FORMAT_VERSION = 3
RECORD_TASK_FIELD_VALUE = "TaskFieldValue"
RECORD_TASK_FACT = "TaskFact"
RECORD_RULE_RESULT = "RuleResult"
_ENTITIES = {RECORD_TASK_FIELD_VALUE: TaskFieldValue, RECORD_TASK_FACT: TaskFact, RECORD_RULE_RESULT: RuleResult}
_CACHE_KEY = "task_value_records"


def _batches(task: AuditTask) -> list[dict[str, Any]]:
    metadata = task.value_file_json or {}
    if not metadata:
        return []
    if metadata.get("format_version") != FORMAT_VERSION:
        raise BusinessError(
            "TASK_VALUE_FORMAT_UPGRADE_REQUIRED",
            "任务取值文件需显式转换为不可变批次格式后读取或继续执行",
            status_code=500, details={"task_no": task.task_no, "format_version": metadata.get("format_version")},
        )
    batches = metadata["batches"]
    identifiers = [batch["batch_id"] for batch in batches]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("任务取值批次清单存在重复批次")
    for batch in batches:
        if not batch["batch_id"] or set(batch["files"]) != set(TABLE_COLUMNS):
            raise ValueError("任务取值批次文件清单与业务表结构不一致")
    return batches


def _read_table(path: Path, columns: tuple[str, ...], *, header_only: bool = False) -> list[dict[str, str]]:
    if not path.exists():
        raise BusinessError("TASK_VALUE_FILE_MISSING", "任务的业务取值文件不存在", status_code=500,
                            details={"relative_path": str(path)})
    csv.field_size_limit(max(csv.field_size_limit(), path.stat().st_size))
    with path.open(encoding="utf-8-sig", newline="") as content:
        reader = csv.DictReader(content)
        if reader.fieldnames != list(columns):
            raise BusinessError("TASK_VALUE_FILE_HEADER_INVALID", "任务取值文件的列结构与当前协议不一致",
                                status_code=500, details={"relative_path": str(path), "columns": reader.fieldnames})
        if header_only:
            return []
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"取值文件存在列数不匹配的行：{path}")
    return rows


def _business_context(db: Session, task: AuditTask, record_type: str, record_id: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    record = db.get(_ENTITIES[record_type], record_id)
    if record is None or record.audit_task_id != task.id:
        raise ValueError(f"取值记录没有对应的任务索引：{record_type}/{record_id}")
    manifest = task.manifest_json
    materials = {item["material_key"]: item["name"] for item in manifest["materials"]}
    fields = manifest["fields"]
    context = {"任务编号": task.task_no, "写入时状态": record.status}
    if record_type == RECORD_RULE_RESULT:
        rule = next(item for item in manifest["rules"] if item["rule_key"] == record.rule_key)
        definitions = {
            item["field_key"]: {**item, "material_name": materials[item["material_key"]]}
            for item in rule.get("output_definitions") or []
        }
        if rule.get("result_code"):
            definitions[rule["result_code"]] = {**rule["calculation_definition"], "name": rule["name"]}
        context.update({"业务类别": "审核结论", "业务编号": record.rule_code, "名称": record.rule_name,
                        "材料名称": "、".join(materials[key] for key in rule["materials"]),
                        "规则类型": rule.get("kind"), "审核要求": rule.get("requirement"),
                        "计算公式": (rule.get("calculation_definition") or {}).get("formula"),
                        "执行批次ID": record.node_execution_id, "取值来源": "规则执行"})
        return context, definitions
    if record_type == RECORD_TASK_FIELD_VALUE:
        field = next(item for item in fields if item["field_key"] == record.field_key)
        context.update({"业务类别": "字段候选", "业务编号": field.get("source_id") or field.get("source_ref"),
                        "字段键": record.field_key, "取值来源": record.origin,
                        "执行批次ID": record.extraction_execution_id})
    elif record.fact_kind == "EXTRACTED":
        matches = [item for item in fields if field_fact_identity(item) == fact_identity(record)]
        if len(matches) != 1:
            raise BusinessError("TASK_FACT_FIELD_IDENTITY_INVALID", "事实索引无法唯一对应完整字段身份", status_code=500,
                                details={"fact_id": record.id, "matched_count": len(matches)})
        field = matches[0]
        context.update({"业务类别": "字段事实", "业务编号": record.fact_code, "字段键": field["field_key"]})
    else:
        field = next(item for item in manifest["calculations"] if item["result_code"] == record.fact_code)
        context.update({"业务类别": "计算结果", "业务编号": record.fact_code})
    context.update({
        "名称": field.get("name") or field.get("rule_name"), "材料键": record.material_key,
        "材料名称": materials[record.material_key] if record.material_key else "",
        "期间": field.get("period_scope") or field.get("period_policy"), "报表口径": field.get("statement_scope"),
        "主体范围": field.get("entity_scope"), "声明类型": field.get("value_type") or field.get("result_type"),
        "取值说明": field.get("instruction") or field.get("requirement"),
        "修订号": record.revision_no,
    })
    if record_type == RECORD_TASK_FACT:
        context.update({"期间": record.period_scope, "报表口径": record.statement_scope,
                        "主体范围": record.entity_scope, "声明类型": record.value_type,
                        "执行批次ID": record.source_execution_id,
                        "取值来源": "计算产出" if record.fact_kind == "DERIVED" else "字段有效事实"})
    return context, {}


def append_values(db: Session, task: AuditTask, entries: Iterable[tuple[str, int, dict[str, Any]]]) -> None:
    """先完整写入新批次，再由调用方事务提交批次引用与业务索引。"""
    entries = list(entries)
    if not entries:
        return
    batches = _batches(task)
    identities = [(kind, int(record_id)) for kind, record_id, _ in entries]
    if len(identities) != len(set(identities)):
        raise ValueError("同一次取值写入包含重复记录身份")
    batch_id = uuid4().hex
    directory = f"tasks/{task.task_no}/value_batches/{batch_id}"
    files = {name: f"{directory}/{name}" for name in TABLE_COLUMNS}
    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_COLUMNS}
    for record_type, record_id, payload in entries:
        context, definitions = _business_context(db, task, record_type, int(record_id))
        RecordWriter({"记录类型": record_type, "记录ID": int(record_id), "写入批次": uuid4().hex},
                     context, tables).record(payload, definitions)
    targets = {name: resolve_storage_path(relative) for name, relative in files.items()}
    resolve_storage_path(directory).mkdir(parents=True, exist_ok=False)
    for name, target in targets.items():
        with target.open("x", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=TABLE_COLUMNS[name])
            writer.writeheader()
            writer.writerows(tables[name])
            output.flush()
            os.fsync(output.fileno())
    previous = int((task.value_file_json or {}).get("record_count") or 0)
    task.value_file_json = {**(task.value_file_json or {}), "format_version": FORMAT_VERSION,
                            "batches": [*batches, {"batch_id": batch_id, "files": files, "record_count": len(entries)}],
                            "record_count": previous + len(entries)}


def _identity(row: dict[str, str]) -> tuple[str, int, str]:
    return row["记录类型"], int(row["记录ID"]), row["写入批次"]


def read_business_files(task_no: str, files: dict[str, str], *, expected_count: int | None = None) -> dict[tuple[str, int], dict[str, Any]]:
    """读取一组完整业务表；也用于显式转换 v2 文件，执行路径不兼容旧格式。"""
    if set(files) != set(TABLE_COLUMNS):
        raise ValueError("任务取值文件清单与业务表结构不一致")
    tables = {name: _read_table(resolve_storage_path(relative), TABLE_COLUMNS[name]) for name, relative in files.items()}
    latest = {
        (row["记录类型"], int(row["记录ID"])): (row, mapping)
        for filename, mapping in MAIN_TABLES.items() for row in tables[filename]
    }
    if expected_count is not None and (len(latest) != expected_count or sum(len(tables[name]) for name in MAIN_TABLES) != expected_count):
        raise BusinessError("TASK_VALUE_FILE_INVALID", "取值批次的记录缺失、重复或数量与已提交清单不一致", status_code=500,
                            details={"task_no": task_no, "files": files})
    details: dict[tuple[str, int, str], dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for row in tables["value_details.csv"]:
        details[_identity(row)][row["所属项目"]].append(row)
    items: dict[str, dict[tuple[str, int, str], list[dict[str, str]]]] = {}
    for key, (filename, _) in ITEM_TABLES.items():
        items[key] = defaultdict(list)
        for row in tables[filename]:
            items[key][_identity(row)].append(row)
    records = {}
    for identity, (row, mapping) in latest.items():
        try:
            batch = _identity(row)
            reader = RecordReader(details[batch])
            payload = reader.attributes(row, mapping, "")
            for key, (_, item_mapping) in ITEM_TABLES.items():
                count = row.get(COUNT_COLUMNS[key])
                rows = sorted(items[key][batch], key=lambda item: int(item["明细序号"]))
                if not count:
                    if rows:
                        raise ValueError(f"{key} 有明细但没有主记录声明")
                    continue
                if [int(item["明细序号"]) for item in rows] != list(range(int(count))):
                    raise ValueError(f"{key} 的明细行缺失、重复或序号不连续")
                values = [reader.attributes(item, item_mapping, f"{key}/{index}/") for index, item in enumerate(rows)]
                payload[key] = [item["item"] for item in values] if key == "missing_items" else values
                if key == "outputs" and identity[0] == RECORD_RULE_RESULT and "extracted_values" not in payload:
                    payload["extracted_values"] = [
                        {"field_key": item["字段键"], "field_name": item["字段名称"],
                         "material_key": item["产出材料键"], "material_name": item["产出材料名称"],
                         "raw_value": value.get("raw_value"), "normalized_value": value["value"],
                         "status": value.get("field_status")}
                        for item, value in zip(rows, values) if item["字段键"]
                    ]
            records[identity] = payload
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            raise BusinessError("TASK_VALUE_FILE_INVALID", "任务取值文件内容不完整或单元格类型不正确",
                                status_code=500, details={"task_no": task_no, "record_type": identity[0],
                                                         "record_id": identity[1], "reason": str(exc)}) from exc
    return records


def _load_batch(task: AuditTask, batch: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return read_business_files(task.task_no, batch["files"], expected_count=batch["record_count"])


def _load(task: AuditTask) -> dict[tuple[str, int], dict[str, Any]]:
    records = {}
    for batch in _batches(task):
        records.update(_load_batch(task, batch))
    return records


def values_of(db: Session, task: AuditTask) -> dict[tuple[str, int], dict[str, Any]]:
    """按生效批次增量缓存；回滚后的清单不含已撤销批次，必须重新构建。"""
    cache = db.info.setdefault(_CACHE_KEY, {})
    cached = cache.get(task.id)
    metadata = task.value_file_json
    if cached is not None and cached["metadata"] is metadata:
        return cached["records"]
    batches = _batches(task)
    signature = tuple((batch["batch_id"], tuple(sorted(batch["files"].items())), batch["record_count"]) for batch in batches)
    previous = cached["signature"] if cached is not None else ()
    extends = cached is not None and signature[:len(previous)] == previous
    records = cached["records"] if extends else {}
    updates = {}
    for batch in batches[len(previous) if extends else 0:]:
        updates.update(_load_batch(task, batch))
    records.update(updates)
    cache[task.id] = {"metadata": metadata, "signature": signature, "records": records}
    return records


def value_of(db: Session, task: AuditTask, record_type: str, record_id: int) -> dict[str, Any]:
    found = values_of(db, task).get((record_type, int(record_id)))
    if found is None:
        raise BusinessError("TASK_VALUE_RECORD_MISSING", "任务取值文件里没有这条记录的取值，本次读取未完成",
                            status_code=500, details={"task_no": task.task_no, "record_type": record_type, "record_id": int(record_id)})
    return found


def values_for(db: Session, task: AuditTask, record_type: str, rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [value_of(db, task, record_type, row.id) for row in rows]
