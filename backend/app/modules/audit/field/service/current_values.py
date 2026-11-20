"""Current reusable facts and archived field values for format 7."""

import csv
import json
import os
from uuid import uuid4

from app.common.exceptions import BusinessError
from app.constants.status import EXECUTABLE_FIELD_STATUSES
from app.domain.audit.fact_projection import decode_value, encode_value
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.model import TaskFact
from app.modules.audit.field.service.value_files import TABLE_CACHE, AuditFieldValueFilesService
from app.services.shared.storage import audit_directory, resolve_storage_path

CURRENT_CSV = "values.csv"
HISTORY_JSON = "history.json"
CURRENT_FILES = {CURRENT_CSV, HISTORY_JSON}
FACT_ATTRIBUTES = {
    "unit": "单位", "currency": "币种", "actual_period_scope": "识别期间",
    "actual_statement_scope": "识别报表口径", "actual_entity_scope": "识别主体",
    "actual_unit": "识别单位",
}
CURRENT_COLUMNS = (
    "事实ID", "类别", "业务编号", "名称", "材料键", "材料名称", "期间", "报表口径", "主体范围",
    "声明类型", "状态", "版本", "值类型", "当前值", *FACT_ATTRIBUTES.values(),
)


def _value_cells(value):
    if isinstance(value, str):
        return "text", value
    encoded = encode_value(value)
    if isinstance(encoded, dict) and set(encoded) == {"kind", "value"} and encoded["kind"] in ("decimal", "date", "datetime"):
        return encoded["kind"], encoded["value"]
    return "json", json.dumps(encoded, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _fact_payload(row):
    kind, raw = row["值类型"], row["当前值"]
    if kind == "text":
        value = raw
    elif kind == "json":
        value = decode_value(json.loads(raw))
    elif kind in ("decimal", "date", "datetime"):
        value = decode_value({"kind": kind, "value": raw})
    else:
        raise ValueError(f"未知取值类型：{kind}")
    if value is None:
        raise ValueError("当前取值不能为 null")
    return {"value": value, **{key: row[column] or None for key, column in FACT_ATTRIBUTES.items()}}


def _record_key(record_type, record_id):
    return f"{record_type}/{record_id}"


class CurrentTaskValues:
    def __init__(self, db):
        self.db = db

    def _cache(self, task):
        tables = self.db.info.setdefault(TABLE_CACHE, {})
        cached = tables.get(task.id)
        if cached is None or cached["metadata"] is not task.value_file_json:
            cached = {"metadata": task.value_file_json}
            tables[task.id] = cached
        return cached

    def _rows(self, task, batch, read_table):
        cached = self._cache(task)
        if "current_rows" not in cached:
            rows = {}
            if batch:
                try:
                    for row in read_table(resolve_storage_path(batch["files"][CURRENT_CSV]), CURRENT_COLUMNS):
                        fact_id = int(row["事实ID"])
                        if fact_id in rows:
                            raise ValueError(f"重复事实ID：{fact_id}")
                        rows[fact_id] = row
                    if len(rows) != batch["record_count"]:
                        raise ValueError("当前取值行数与清单不一致")
                except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
                    raise self._invalid(task, CURRENT_CSV) from exc
            cached["current_rows"] = rows
        return cached["current_rows"]

    def _history(self, task, batch):
        cached = self._cache(task)
        if "history" not in cached:
            history = {}
            if batch:
                try:
                    with resolve_storage_path(batch["files"][HISTORY_JSON]).open(encoding="utf-8") as source:
                        history = json.load(source)
                    if not isinstance(history, dict):
                        raise ValueError("历史取值应为对象")
                except (FileNotFoundError, ValueError) as exc:
                    raise self._invalid(task, HISTORY_JSON) from exc
            cached["history"] = history
        return cached["history"]

    @staticmethod
    def _invalid(task, filename):
        return BusinessError("TASK_VALUE_FILE_INVALID", "任务取值文件缺失或内容不正确", status_code=500,
                             details={"task_no": task.task_no, "filename": filename})

    def find(self, task, batch, record_type, record_id, read_table):
        if record_type == "TaskFact":
            row = self._rows(task, batch, read_table).get(record_id)
            if row is not None:
                try:
                    return _fact_payload(row)
                except (ValueError, TypeError, ArithmeticError) as exc:
                    raise self._invalid(task, CURRENT_CSV) from exc
        value = self._history(task, batch).get(_record_key(record_type, record_id))
        if value is None:
            return None
        payload = decode_value(value)
        if not isinstance(payload, dict):
            raise self._invalid(task, HISTORY_JSON)
        return payload

    def write(self, task, batch, entries, *, read_table, business_context, format_version):
        old_rows = self._rows(task, batch, read_table)
        rows = dict(old_rows)
        archived = {}
        crud = AuditFieldCRUD(self.db)
        crud.flush()
        if rows:
            current_ids = crud.current_fact_ids(task.id)
            for fact_id in rows.keys() - current_ids:
                archived[_record_key("TaskFact", fact_id)] = encode_value(_fact_payload(rows.pop(fact_id)))
        for record_type, record_id, payload in entries:
            key = _record_key(record_type, record_id)
            if record_type == "TaskFieldValue":
                archived[key] = encode_value(payload)
                continue
            if record_type != "TaskFact":
                raise ValueError(f"未知取值记录类型：{record_type}")
            fact = crud.get_of(TaskFact, record_id)
            if fact is None or fact.audit_task_id != task.id:
                raise ValueError(f"取值记录没有对应的任务索引：{key}")
            value = payload.get("value")
            if not fact.is_current or value is None or fact.status not in EXECUTABLE_FIELD_STATUSES | {"COMPUTED"}:
                rows.pop(record_id, None)
                archived[key] = encode_value(payload)
                continue
            context, _ = business_context(task, record_type, record_id)
            kind, raw = _value_cells(value)
            rows[record_id] = {
                "事实ID": str(record_id), "类别": context["业务类别"], "业务编号": fact.fact_code,
                "名称": context["名称"] or "", "材料键": fact.material_key or "", "材料名称": context["材料名称"],
                "期间": fact.period_scope or "", "报表口径": fact.statement_scope or "", "主体范围": fact.entity_scope or "",
                "声明类型": fact.value_type, "状态": fact.status, "版本": str(fact.revision_no),
                "值类型": kind, "当前值": raw,
                **{column: payload.get(key) or "" for key, column in FACT_ATTRIBUTES.items()},
            }

        history = {**self._history(task, batch), **archived} if archived or not batch else None
        changed = {CURRENT_CSV} if not batch or rows != old_rows else set()
        if history is not None:
            changed.add(HISTORY_JSON)
        if not changed:
            return
        directory = f"{audit_directory(task.task_no, task.created_at)}/values"
        files = batch["files"] if batch else {
            CURRENT_CSV: f"{directory}/current/{CURRENT_CSV}",
            HISTORY_JSON: f"{directory}/{HISTORY_JSON}",
        }
        targets = {name: resolve_storage_path(files[name]) for name in changed}
        AuditFieldValueFilesService(self.db).snapshot_files(targets)
        temporary = {}
        try:
            for name, target in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary[name] = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                with temporary[name].open("x", encoding="utf-8-sig" if name == CURRENT_CSV else "utf-8", newline="") as output:
                    if name == CURRENT_CSV:
                        writer = csv.DictWriter(output, fieldnames=CURRENT_COLUMNS)
                        writer.writeheader()
                        writer.writerows(rows.values())
                    else:
                        json.dump(history, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    output.flush()
                    os.fsync(output.fileno())
            for name, target in targets.items():
                os.replace(temporary[name], target)
        finally:
            for path in temporary.values():
                path.unlink(missing_ok=True)
        task.value_file_json = {**(task.value_file_json or {}), "format_version": format_version,
                                "batches": [{"batch_id": "current", "files": files, "record_count": len(rows)}]}
        cached = self._cache(task)
        cached["current_rows"] = rows
        if history is not None:
            cached["history"] = history
