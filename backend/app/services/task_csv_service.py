"""审核任务 CSV 中间值：写入随数据库事务切换文件指针，执行读取 CSV。

数据库保留事实/结果索引与投影用于证据关联；字段原文、规范值、派生值、规则结论从 CSV
恢复给执行节点。文件采用独立版本，事务回滚不会覆盖已提交文件，也不会读到未提交指针。
无 CSV 文件、损坏或身份错配均显式失败，不从数据库数值降级读取。
"""

import csv
import hashlib
import io
import json
import os
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entity import AuditTask, TaskFieldValue, TaskFact, RuleResult
from .storage_service import resolve_storage_path

MODELS = {model.__name__: model for model in (TaskFieldValue, TaskFact, RuleResult)}
VALUE_COLUMNS = ("raw_value", "normalized_value_json", "decimal_value", "text_value", "date_value", "boolean_value")
COLUMNS = ("record_type", "record_id", "field_key", "fact_code", "fact_kind", "rule_key", "rule_name",
           "material_key", "value_type", "status", "revision_no", "is_current", "unit", "currency",
           "period_scope", "statement_scope", *VALUE_COLUMNS, "message", "result_json", "payload_json")


def _json_default(value: Any) -> str:
    if isinstance(value, (Decimal, date, datetime)):
        return str(value)
    raise TypeError(f"CSV 无法编码 {type(value).__name__}")


def _records(task: AuditTask) -> dict[tuple[str, int], dict[str, Any]]:
    if task.intermediate_csv_json is None:
        return {}
    metadata = task.intermediate_csv_json
    path = resolve_storage_path(metadata["relative_path"])
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata["sha256"]:
        raise BusinessError("TASK_CSV_CONTENT_CHANGED", "任务 CSV 中间文件内容与已提交版本不一致", status_code=500)
    records = {}
    # CSV 标准库默认单格限制不能截断业务 JSON；按真实文件大小解析。
    csv.field_size_limit(max(csv.field_size_limit(), len(content)))
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != list(COLUMNS):
        raise BusinessError("TASK_CSV_HEADER_INVALID", "任务 CSV 列结构与执行协议不一致", status_code=500)
    for row in reader:
        payload = json.loads(row["payload_json"])
        if payload["audit_task_id"] != task.id:
            raise BusinessError("TASK_CSV_SCOPE_INVALID", "CSV 记录不属于当前审核任务", status_code=500)
        key = (row["record_type"], int(row["record_id"]))
        if key in records:
            raise BusinessError("TASK_CSV_DUPLICATE", "CSV 中间记录身份重复", status_code=500)
        records[key] = payload
    return records


def _persist(db: Session, changes: list[Any], value_changes: dict[int, set[str]]) -> None:
    by_task: dict[int, list[Any]] = {}
    for row in changes:
        by_task.setdefault(row.audit_task_id, []).append(row)
    for task_id, task_changes in by_task.items():
        task = db.get(AuditTask, task_id)
        if task.intermediate_csv_json is None:
            for model in MODELS.values():
                changed_ids = [row.id for row in task_changes if type(row) is model]
                existing = db.scalar(select(model.id).where(
                    model.audit_task_id == task.id, model.id.not_in(changed_ids)
                ).limit(1))
                if existing is not None:
                    raise BusinessError("TASK_CSV_MISSING", "任务已有结果索引但缺少 CSV 中间文件，不能重建部分结果冒充完整数据", status_code=500)
        records = _records(task)
        for row in task_changes:
            # 当前写命令的真实结果进入 CSV；下次读取不再取数据库业务值。
            payload = {column.key: getattr(row, column.key) for column in row.__table__.columns
                       if column.computed is None and column.key not in {"created_at", "updated_at"}}
            key = (type(row).__name__, row.id)
            if key in records and type(row) in {TaskFact, TaskFieldValue}:
                # 失效、证据关联等索引变化不能用数据库投影覆盖 CSV 的原始精确值。
                for column in VALUE_COLUMNS:
                    if column in records[key] and column not in value_changes.get(id(row), set()):
                        payload[column] = records[key][column]
            records[key] = json.loads(json.dumps(payload, ensure_ascii=False, default=_json_default))
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
        writer.writeheader()
        for (kind, row_id), payload in sorted(records.items()):
            record = {column: payload.get(column) for column in COLUMNS if column not in {"record_type", "record_id", "payload_json"}}
            for column, value in record.items():
                if isinstance(value, (dict, list)):
                    record[column] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            record.update(record_type=kind, record_id=row_id,
                          payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            writer.writerow(record)
        content = buffer.getvalue().encode("utf-8-sig")
        digest = hashlib.sha256(content).hexdigest()
        if task.intermediate_csv_json is not None and task.intermediate_csv_json["sha256"] == digest:
            continue
        transaction = db.get_nested_transaction() or db.get_transaction()
        paths = db.info.setdefault("task_csv_paths", {})
        relative_path = paths.setdefault((task.id, transaction), f"tasks/{task.task_no}/intermediate/{uuid4().hex}.csv")
        path = resolve_storage_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 唯一文件只在写完后关联；失败传播，数据库事务保持失败状态。
        temporary = path.with_name(uuid4().hex + ".tmp")
        with temporary.open("xb") as output:
            output.write(content)
        os.replace(temporary, path)
        task.intermediate_csv_json = {"relative_path": relative_path, "sha256": digest, "record_count": len(records)}


@event.listens_for(Session, "before_flush")
def _collect_csv_changes(db: Session, flush_context: Any, instances: Any) -> None:
    changes = [row for row in (*db.new, *db.dirty) if type(row).__name__ in MODELS and (row in db.new or db.is_modified(row))]
    db.info["task_csv_changes"] = changes
    db.info["task_csv_value_changes"] = {
        id(row): {column for column in VALUE_COLUMNS
                  if column in inspect(row).attrs and inspect(row).attrs[column].history.has_changes()}
        for row in changes
    }


@event.listens_for(Session, "after_flush_postexec")
def _write_csv_changes(db: Session, flush_context: Any) -> None:
    changes = db.info.pop("task_csv_changes", [])
    value_changes = db.info.pop("task_csv_value_changes", {})
    if changes:
        _persist(db, changes, value_changes)


@event.listens_for(Session, "do_orm_execute", retval=True)
def _write_bulk_csv_changes(state: Any) -> Any:
    """材料替换与下游失效使用批量 UPDATE，同步对应记录的当前状态。"""
    if not state.is_update:
        return state.invoke_statement()
    mapper = state.bind_mapper
    if mapper is None or mapper.class_.__name__ not in MODELS:
        return state.invoke_statement()
    model = mapper.class_
    db = state.session
    ids = list(db.scalars(select(model.id).where(*state.statement._where_criteria)))
    result = state.invoke_statement()
    with db.no_autoflush:
        rows = list(db.scalars(select(model).where(model.id.in_(ids)).execution_options(populate_existing=True)))
        changed_columns = {getattr(column, "key", column) for column in (state.statement._values or {})} & set(VALUE_COLUMNS)
        _persist(db, rows, {id(row): changed_columns for row in rows})
    return result


def records(db: Session, task: AuditTask, model: type) -> list[SimpleNamespace]:
    db.flush()
    if task.intermediate_csv_json is None and db.scalar(select(model.id).where(model.audit_task_id == task.id).limit(1)) is not None:
        raise BusinessError("TASK_CSV_MISSING", "任务已有结果索引但缺少 CSV 中间文件，不能继续执行", status_code=500)
    payloads = _records(task)
    result = []
    for (kind, _), payload in payloads.items():
        if kind != model.__name__:
            continue
        attributes = dict(payload)
        for column in model.__table__.columns:
            value = attributes.get(column.key)
            if value is None or column.computed is not None:
                continue
            python_type = column.type.python_type
            if python_type is Decimal:
                attributes[column.key] = Decimal(value)
            elif python_type is date:
                attributes[column.key] = date.fromisoformat(value)
        result.append(SimpleNamespace(**attributes))
    return result


def from_index(db: Session, task: AuditTask, model: type, rows: list[Any]) -> list[SimpleNamespace]:
    """数据库仅筛选身份/上传版本/执行批次；业务值按精确记录 ID 从 CSV 取。"""
    by_id = {row.id: row for row in records(db, task, model)}
    result = []
    for row in rows:
        if row.id not in by_id:
            raise BusinessError("TASK_CSV_RECORD_MISSING", "任务中间 CSV 缺少当前索引记录", status_code=500,
                                details={"record_type": model.__name__, "record_id": row.id})
        result.append(by_id[row.id])
    return result


@event.listens_for(Session, "after_transaction_end")
def _clear_csv_transaction(db: Session, transaction: Any) -> None:
    # 历史及失败事务文件按执行契约保留。只在根事务结束后清会话中的路径映射；
    # SAVEPOINT 提交或回滚都不能清掉外层仍会复用的未提交路径。
    if transaction.parent is None:
        db.info.pop("task_csv_paths", None)
        db.info.pop("task_csv_changes", None)
        db.info.pop("task_csv_value_changes", None)


def download_path(task: AuditTask) -> Any:
    if task.intermediate_csv_json is None:
        raise BusinessError("TASK_CSV_NOT_CREATED", "本任务尚未产生抽取或计算结果", status_code=409)
    _records(task)  # 文件校验失败直接暴露，不返回其他数据冒充。
    return resolve_storage_path(task.intermediate_csv_json["relative_path"])
