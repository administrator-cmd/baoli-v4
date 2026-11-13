"""按业务列保存取值及明细；数据库决定身份、当前版本与状态。

每个任务维护一组固定的四张业务表（格式版本 6）。原文依据直接存主记录的 JSON 列。
写入时按记录身份合并并原子替换，
全部文件落盘后才更新数据库清单。旧格式不做原地转换，读取时明确报错。
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import FACT_KIND_DERIVED, FACT_KIND_EXTRACTED
from app.domain.audit.fact_projection import fact_identity, field_fact_identity
from app.domain.audit.task_value_csv import (
    COUNT_COLUMNS,
    ITEM_CATEGORY,
    ITEM_FILE,
    ITEM_TABLES,
    MAIN_TABLES,
    TABLE_COLUMNS,
    RecordReader,
    RecordWriter,
)
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.model import TaskFact, TaskFieldValue
from app.modules.audit.field.service.value_files import TABLE_CACHE, AuditFieldValueFilesService
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.task.model import AuditTask
from app.services.shared.storage import resolve_storage_path

FORMAT_VERSION = 6


CURRENT_BATCH_ID = "current"


RECORD_TASK_FIELD_VALUE = "TaskFieldValue"


RECORD_TASK_FACT = "TaskFact"


RECORD_RULE_RESULT = "RuleResult"


_ENTITIES = {RECORD_TASK_FIELD_VALUE: TaskFieldValue, RECORD_TASK_FACT: TaskFact, RECORD_RULE_RESULT: RuleResult}


_CACHE_KEY = "task_value_records"


_MAIN_RECORD_TYPES = {
    "values.csv": {RECORD_TASK_FIELD_VALUE, RECORD_TASK_FACT},
    "rule_results.csv": {RECORD_RULE_RESULT},
}


class AuditFieldValueStoreService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _batches(self, task: AuditTask) -> list[dict[str, Any]]:
        metadata = task.value_file_json or {}
        if not metadata:
            return []
        if metadata.get("format_version") != FORMAT_VERSION:
            raise BusinessError(
                "TASK_VALUE_FORMAT_UPGRADE_REQUIRED",
                "任务取值文件格式与当前版本不一致，不能读取或继续执行",
                status_code=500, details={"task_no": task.task_no, "format_version": metadata.get("format_version")},
            )
        batches = metadata.get("batches")
        if not isinstance(batches, list) or len(batches) != 1 or not isinstance(batches[0], dict):
            raise BusinessError(
                "TASK_VALUE_FILE_INVALID",
                "任务取值文件清单必须只登记 current 一组",
                status_code=500, details={"task_no": task.task_no},
            )
        batch = batches[0]
        if batch.get("batch_id") != CURRENT_BATCH_ID:
            raise BusinessError(
                "TASK_VALUE_FILE_INVALID",
                "任务取值文件清单必须只登记 current 一组",
                status_code=500, details={"task_no": task.task_no},
            )
        files = batch.get("files")
        if not isinstance(files, dict) or set(files) != set(TABLE_COLUMNS):
            raise BusinessError(
                "TASK_VALUE_FILE_INVALID",
                "任务取值文件清单与业务表结构不一致",
                status_code=500, details={"task_no": task.task_no},
            )
        return [batch]


    def _read_table(self, path: Path, columns: tuple[str, ...], *, header_only: bool = False) -> list[dict[str, str]]:
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


    def _business_context(self, task: AuditTask, record_type: str, record_id: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        record = AuditFieldCRUD(self.db).get_of(_ENTITIES[record_type], record_id)
        if record is None or record.audit_task_id != task.id:
            raise ValueError(f"取值记录没有对应的任务索引：{record_type}/{record_id}")
        manifest = task.manifest_json
        materials = {item["material_key"]: item["name"] for item in manifest["materials"]}
        fields = manifest["fields"]
        if record_type == RECORD_RULE_RESULT:
            rule = next(item for item in manifest["rules"] if item["rule_key"] == record.rule_key)
            definitions = {
                item["field_key"]: {**item, "material_name": materials[item["material_key"]]}
                for item in rule.get("output_definitions") or []
            }
            if rule.get("result_code"):
                definitions[rule["result_code"]] = {**rule["calculation_definition"], "name": rule["name"]}
            context = {
                "业务编号": record.rule_code, "名称": record.rule_name,
                "材料名称": "、".join(materials[key] for key in rule["materials"]),
                "规则类型": rule.get("kind"), "审核要求": rule.get("requirement"),
                "计算公式": (rule.get("calculation_definition") or {}).get("formula"),
            }
            return context, definitions
        if record_type == RECORD_TASK_FIELD_VALUE:
            field = next(item for item in fields if item["field_key"] == record.field_key)
            context = {"业务类别": "字段候选", "业务编号": field.get("source_id") or field.get("source_ref"),
                       "取值来源": record.origin}
        elif record.fact_kind == FACT_KIND_EXTRACTED:
            matches = [item for item in fields if field_fact_identity(item) == fact_identity(record)]
            if len(matches) != 1:
                raise BusinessError("TASK_FACT_FIELD_IDENTITY_INVALID", "事实索引无法唯一对应完整字段身份", status_code=500,
                                    details={"fact_id": record.id, "matched_count": len(matches)})
            field = matches[0]
            context = {"业务类别": "字段事实", "业务编号": record.fact_code}
        else:
            field = next(item for item in manifest["calculations"] if item["result_code"] == record.fact_code)
            context = {"业务类别": "计算结果", "业务编号": record.fact_code}
        context.update({
            "名称": field.get("name") or field.get("rule_name"),
            "材料名称": materials[record.material_key] if record.material_key else "",
            "期间": field.get("period_scope") or field.get("period_policy"), "报表口径": field.get("statement_scope"),
            "主体范围": field.get("entity_scope"), "声明类型": field.get("value_type") or field.get("result_type"),
            "取值说明": field.get("instruction") or field.get("requirement"),
        })
        if record_type == RECORD_TASK_FACT:
            context.update({"期间": record.period_scope, "报表口径": record.statement_scope,
                            "主体范围": record.entity_scope, "声明类型": record.value_type,
                            "取值来源": "计算产出" if record.fact_kind == FACT_KIND_DERIVED else "字段有效事实"})
        return context, {}


    def append_values(self, task: AuditTask, entries: Iterable[tuple[str, int, dict[str, Any]]]) -> None:
        """合并记录后重写任务唯一的一组四表，再由调用方提交数据库事务。

        写入前在事务内保存旧文件快照；保存点或外层回滚分别恢复对应文件与清单。
        """
        entries = list(entries)
        if not entries:
            return
        AuditFieldValueFilesService(self.db).lock_task_files( task.task_no)
        identities = [(kind, int(record_id)) for kind, record_id, _ in entries]
        if len(identities) != len(set(identities)):
            raise ValueError("同一次取值写入包含重复记录身份")
        batches = self._batches(task)
        cache = self.db.info.setdefault(TABLE_CACHE, {})
        cached = cache.get(task.id)
        if cached is not None and cached["metadata"] is task.value_file_json:
            # 同一事务内刚写过这批文件：内容由本进程写入并已落地，不再整表重读解析。
            existing = cached["tables"]
        else:
            existing = {name: [] for name in TABLE_COLUMNS}
            for batch in batches:
                for name, relative in batch["files"].items():
                    existing[name] = self._read_table(resolve_storage_path(relative), TABLE_COLUMNS[name])
                self._records_from_tables(task.task_no, existing, expected_count=batch["record_count"])

        incoming: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_COLUMNS}
        for record_type, record_id, payload in entries:
            context, definitions = self._business_context( task, record_type, int(record_id))
            RecordWriter({"记录类型": record_type, "记录ID": int(record_id)}, context, incoming).record(payload, definitions)

        # 删除将被更新的旧记录及其明细，再追加新版本。
        incoming_identities = set(identities)
        tables: dict[str, list[dict[str, Any]]] = {}
        changed: list[str] = []
        # 首次写入没有历史清单：必须建齐四张带表头的文件。清单登记四张表，缺任何一张，
        # 下一次严格读取都会报缺文件；已有清单但缺文件属真实损坏，继续由 _read_table 报错。
        initial = not batches
        for name in TABLE_COLUMNS:
            kept = [
                row for row in existing[name]
                if (row["记录类型"], int(row["记录ID"])) not in incoming_identities
            ]
            if initial or incoming[name] or len(kept) != len(existing[name]):
                changed.append(name)
            tables[name] = [*kept, *incoming[name]]

        directory = f"tasks/{task.task_no}/value_batches/{CURRENT_BATCH_ID}"
        files = {name: f"{directory}/{name}" for name in TABLE_COLUMNS}
        targets = {name: resolve_storage_path(relative) for name, relative in files.items()}
        resolve_storage_path(directory).mkdir(parents=True, exist_ok=True)
        # 只重写本次真正变化的表：四张表都很大，逐条提交时全量重写加 fsync 会重复整份文件。
        writing = {name: targets[name] for name in changed}
        AuditFieldValueFilesService(self.db).snapshot_files( writing)
        temporary: dict[str, Path] = {}
        try:
            token = uuid4().hex
            for name, target in writing.items():
                temporary[name] = target.with_name(f".{target.name}.{token}.tmp")
                with temporary[name].open("x", encoding="utf-8-sig", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=TABLE_COLUMNS[name])
                    writer.writeheader()
                    writer.writerows(tables[name])
                    output.flush()
                    os.fsync(output.fileno())
            for name, target in writing.items():
                os.replace(temporary[name], target)
        finally:
            for path in temporary.values():
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        record_count = sum(len(tables[name]) for name in MAIN_TABLES)
        task.value_file_json = {**(task.value_file_json or {}), "format_version": FORMAT_VERSION,
                                "batches": [{"batch_id": CURRENT_BATCH_ID, "files": files,
                                             "record_count": record_count}]}
        cache[task.id] = {"metadata": task.value_file_json, "tables": tables}


    def _identity(self, row: dict[str, str]) -> tuple[str, int]:
        return row["记录类型"], int(row["记录ID"])


    def read_business_files(self, task_no: str, files: dict[str, str], *, expected_count: int | None = None) -> dict[tuple[str, int], dict[str, Any]]:
        """读取一组完整业务表；调用方必须先通过 _batches 校验格式版本。"""
        if set(files) != set(TABLE_COLUMNS):
            raise ValueError("任务取值文件清单与业务表结构不一致")
        tables = {name: self._read_table(resolve_storage_path(relative), TABLE_COLUMNS[name]) for name, relative in files.items()}
        return self._records_from_tables(task_no, tables, expected_count=expected_count)


    def _records_from_tables(self, task_no: str, tables: dict[str, list[dict[str, str]]], *, expected_count: int | None) -> dict[tuple[str, int], dict[str, Any]]:
        latest: dict[tuple[str, int], tuple[dict[str, str], dict[str, str]]] = {}
        try:
            for filename, mapping in MAIN_TABLES.items():
                for row in tables[filename]:
                    record_type = row["记录类型"]
                    if record_type not in _MAIN_RECORD_TYPES[filename]:
                        raise ValueError(f"{filename} 中出现不属于该表的记录类型：{record_type}")
                    latest[(record_type, int(row["记录ID"]))] = (row, mapping)
        except (KeyError, ValueError, TypeError) as exc:
            raise BusinessError("TASK_VALUE_FILE_INVALID", "任务取值文件的记录类型或身份无法读取", status_code=500,
                                details={"task_no": task_no, "reason": str(exc)}) from exc
        if expected_count is not None and (len(latest) != expected_count or sum(len(tables[name]) for name in MAIN_TABLES) != expected_count):
            raise BusinessError("TASK_VALUE_FILE_INVALID", "取值批次的记录缺失、重复或数量与已提交清单不一致", status_code=500,
                                details={"task_no": task_no, "expected_count": expected_count,
                                         "actual_count": sum(len(tables[name]) for name in MAIN_TABLES)})
        details: dict[tuple[str, int], dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
        for row in tables["value_details.csv"]:
            details[self._identity(row)][row["所属项目"]].append(row)
        # 同一明细表按（记录身份, 明细类别）分组；类别来自“明细类别”列。
        items: dict[tuple[str, int], dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
        for row in tables[ITEM_FILE]:
            category = row[ITEM_CATEGORY]
            if category not in ITEM_TABLES:
                raise ValueError(f"未知明细类别：{category}")
            items[self._identity(row)][category].append(row)
        # 明细必须属于一条真实主记录；孤儿明细既不参与读取也不应长期留在文件里。
        orphans = sorted(
            identity for identity in [*details, *items] if identity not in latest
        )
        if orphans:
            raise BusinessError(
                "TASK_VALUE_FILE_INVALID", "任务取值文件存在没有主记录的明细", status_code=500,
                details={"task_no": task_no, "orphan_count": len(orphans),
                         "record_type": orphans[0][0], "record_id": orphans[0][1]},
            )
        records = {}
        for identity, (row, mapping) in latest.items():
            try:
                reader = RecordReader(details[identity])
                payload = reader.attributes(row, mapping, "")
                for key, item_mapping in ITEM_TABLES.items():
                    count = row.get(COUNT_COLUMNS[key])
                    rows = sorted(items[identity][key], key=lambda item: int(item["明细序号"]))
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


    def _load_batch(self, task: AuditTask, batch: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
        return self.read_business_files(task.task_no, batch["files"], expected_count=batch["record_count"])


    def _load(self, task: AuditTask) -> dict[tuple[str, int], dict[str, Any]]:
        # _batches 保证只有一组 current 四表；尚未写入任何取值的任务没有任何批次。
        batches = self._batches(task)
        return self._load_batch(task, batches[0]) if batches else {}


    def values_of(self, task: AuditTask) -> dict[tuple[str, int], dict[str, Any]]:
        """缓存任务取值；固定 current 批次在清单变化后整体重读。"""
        cached = self.db.info.get(_CACHE_KEY, {}).get(task.id)
        if cached is not None and cached["metadata"] is task.value_file_json:
            return cached["records"]
        with AuditFieldValueFilesService(self.db).read_task_files( task.task_no):
            return self._cached_values( task)


    def _cached_values(self, task: AuditTask) -> dict[tuple[str, int], dict[str, Any]]:
        cache = self.db.info.setdefault(_CACHE_KEY, {})
        cached = cache.get(task.id)
        metadata = task.value_file_json
        if cached is not None and cached["metadata"] is metadata:
            return cached["records"]
        records = self._load(task)
        cache[task.id] = {"metadata": metadata, "records": records}
        return records


    def value_of(self, task: AuditTask, record_type: str, record_id: int) -> dict[str, Any]:
        found = self.values_of( task).get((record_type, int(record_id)))
        if found is None:
            raise BusinessError("TASK_VALUE_RECORD_MISSING", "任务取值文件里没有这条记录的取值，本次读取未完成",
                                status_code=500, details={"task_no": task.task_no, "record_type": record_type, "record_id": int(record_id)})
        return found
