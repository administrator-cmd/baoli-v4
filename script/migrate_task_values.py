"""显式转换旧三列、v2 业务表或压缩旧 v3 批次；默认只读预览，--apply 才切换引用。

旧格式原文件保留且继续登记为引用文件；旧 v3 批次压缩到固定六表，删除随事务回滚。
不调用模型，不重跑审核，不删除数据库数据。
请停止该任务的执行后运行。示例：python script/migrate_task_values.py TASK_xxx --apply
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "backend"))

from sqlalchemy import select

from app.database import SessionLocal
from app.domain.fact_projection import decode_value
from app.domain.task_value_csv import TABLE_COLUMNS
from app.entity import AuditTask, RuleResult, TaskFact, TaskFieldValue
from app.services.system.background import assert_no_active_operation
from app.services.system.operation_types import OBJECT_AUDIT_TASK
from app.services.shared.storage import resolve_storage_path
from app.services.audit.value_store import FORMAT_VERSION, _load, _read_table, append_values, read_business_files
from app.services.audit.value_files import lock_task_files


def _business_payload(record_type, record, payload):
    """去除重复的模型结论封套，旧的非重复业务属性逐项保留。"""
    payload = dict(payload)
    if record_type == "RuleResult":
        verdict = payload.pop("verdict", None)
        if verdict is not None:
            remaining = {}
            for key, value in verdict.items():
                if key in {"rule_key", "rule_name"}:
                    continue  # 身份及名称由冻结配置、数据库索引提供。
                if key not in payload:
                    payload[key] = value
                elif payload[key] != value:
                    remaining[key] = value
            if remaining:
                payload["历史结论差异"] = remaining
        # 当前索引状态与旧模型结论不同的情况不可被隐藏。
        for key in ("status", "message"):
            indexed = getattr(record, key)
            if key in payload and payload[key] != indexed:
                payload.setdefault("历史结论差异", {})[key] = payload[key]
            payload[key] = indexed
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_no")
    parser.add_argument("--apply", action="store_true", help="写固定六表并切换引用；旧 v3 批次清理，旧格式原文件保留")
    args = parser.parse_args()
    with SessionLocal() as db:
        if not args.apply:
            lock_task_files(db, args.task_no)
        statement = select(AuditTask).where(AuditTask.task_no == args.task_no)
        task = db.scalar(statement.with_for_update() if args.apply else statement)
        if task is None:
            raise ValueError(f"任务不存在：{args.task_no}")
        if args.apply:
            # 写入统一先锁数据库任务行，再锁文件；不能与后台写入反向持锁。
            assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
            lock_task_files(db, args.task_no)
        metadata = task.value_file_json
        if not metadata:
            raise ValueError("任务没有登记取值文件")
        if metadata.get("format_version") == FORMAT_VERSION:
            source = "现有 v3 批次清单"
            latest = _load(task)
            originals = []
        elif metadata.get("format_version") == 2:
            source = metadata["relative_path"]
            latest = read_business_files(task.task_no, metadata["files"])
            originals = list(metadata["files"].values())
        elif metadata.get("format_version") in (None, 1):
            source = metadata["relative_path"]
            legacy_rows = _read_table(resolve_storage_path(source), ("record_type", "record_id", "payload_json"))
            latest = {(row["record_type"], int(row["record_id"])): decode_value(json.loads(row["payload_json"])) for row in legacy_rows}
            originals = [source]
        else:
            raise ValueError(f"不支持转换该文件版本：{metadata.get('format_version')}")
        entries = []
        for model in (TaskFieldValue, TaskFact, RuleResult):
            rows = db.scalars(select(model).where(model.audit_task_id == task.id).order_by(model.id))
            for record in rows:
                identity = (model.__name__, record.id)
                payload = latest.pop(identity)  # 索引缺值明确失败，不补空值。
                entries.append((*identity, _business_payload(model.__name__, record, payload)))
        orphan_note = "一并保留到 current 六表" if metadata.get("format_version") == FORMAT_VERSION else "随旧来源文件留存并登记为 legacy_files"
        print(f"任务：{task.task_no}；来源：{source}")
        print(f"索引对应取值：{len(entries)} 条；无索引孤立取值：{len(latest)} 条（{orphan_note}）")
        print("新表：" + "、".join(TABLE_COLUMNS))
        if not args.apply:
            print("只读预览结束，未改写业务 CSV、未修改数据库。")
            return
        if metadata.get("format_version") != FORMAT_VERSION:
            task.value_file_json = {"format_version": FORMAT_VERSION, "batches": [], "record_count": 0,
                                    "legacy_files": list(dict.fromkeys([*originals, *metadata.get("legacy_files", [])]))}
        # v3 保留原清单，让 append_values 同时合并孤立记录并登记旧文件的回滚快照。
        append_values(db, task, entries, compact=True)
        restored = _load(task)
        for record_type, record_id, expected in entries:
            actual = restored[(record_type, record_id)]
            if any(key not in actual or actual[key] != value for key, value in expected.items()):
                raise ValueError(f"转换后的业务值与原值不一致：{record_type}/{record_id}，未切换引用")
        if metadata.get("format_version") == FORMAT_VERSION:
            for identity, expected in latest.items():
                if restored[identity] != expected:
                    raise ValueError(f"压缩后的孤立取值与原值不一致：{identity}，未切换引用")
        db.commit()
        print("引用已切换到固定六表；旧 v3 批次已清理，旧格式原文件仍登记为 legacy_files。")


if __name__ == "__main__":
    main()
