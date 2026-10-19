"""固定六表的事务锁与保存点快照；不判断规则或吞掉读写失败。"""

from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import shutil
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from ..shared.file_lock import FileLock
from ..shared.storage import resolve_storage_path


logger = logging.getLogger(__name__)
_LOCKS = "task_value_file_locks"
_SNAPSHOTS = "task_value_file_snapshots"
_CACHE = "task_value_records"


def _file_lock(task_no: str) -> FileLock:
    key = hashlib.sha256(task_no.encode("utf-8")).hexdigest()
    return FileLock(resolve_storage_path(f".locks/task-values/{key}.lock"))


def lock_task_files(db: Session, task_no: str) -> None:
    """在读取任务清单之前持锁，直到最外层事务结束（包括回滚恢复）。"""
    if not db.in_transaction():
        db.begin()
    locks = db.info.setdefault(_LOCKS, {})
    if task_no not in locks:
        lock = _file_lock(task_no)
        lock.acquire()
        locks[task_no] = lock


@contextmanager
def read_task_files(db: Session, task_no: str):
    """只读调用只在加载六表时持锁；已有事务锁时复用，不提前释放。"""
    if task_no in db.info.get(_LOCKS, {}):
        yield
        return
    lock = _file_lock(task_no)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def snapshot_files(db: Session, targets: dict[str, Path]) -> None:
    """每层事务首次改写时各留一份；保存点成功不能删除外层快照。"""
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:
        raise RuntimeError("取值文件写入必须属于数据库事务")
    snapshots = db.info.setdefault(_SNAPSHOTS, {})
    while transaction is not None:
        if transaction.parent is None or transaction.nested:
            files = snapshots.setdefault(transaction, {})
            for target in targets.values():
                if target in files:
                    continue
                if target.exists():
                    # 放在目标旁边，确保跨盘数据目录也能原子恢复；不同任务不共用备份名。
                    backup = target.with_name(f".{target.name}.{uuid4().hex}.rollback")
                    shutil.copyfile(target, backup)
                    files[target] = backup
                else:
                    files[target] = None
        transaction = transaction.parent


def remove_obsolete_files(db: Session, batches, current_files, retained_files) -> None:
    current = set(current_files.values())
    targets = {relative: resolve_storage_path(relative)
               for batch in batches for relative in batch["files"].values()
               if relative not in current and relative not in retained_files}
    # 删除在业务提交之前完成；失败会明确上抛，回滚同时恢复旧文件。
    snapshot_files(db, targets)
    for target in targets.values():
        target.unlink(missing_ok=True)


def _discard_backups(files) -> None:
    for backup in files.values():
        if backup is None:
            continue
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            # 业务数据已提交或已恢复；临时快照清理单独报告，不能伪报业务提交失败。
            logger.exception("取值文件事务已结束，但临时快照未清理 remaining_path=%s", backup)


def _finish_snapshot(db: Session, transaction, *, restore: bool) -> None:
    snapshots = db.info.get(_SNAPSHOTS, {})
    files = snapshots.get(transaction)
    if files is None:
        return
    if restore:
        for target, backup in files.items():
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                # 保留源备份直到全部恢复成功，避免部分恢复后再次恢复时找不到备份。
                restoring = target.with_name(f".{target.name}.{uuid4().hex}.restoring")
                shutil.copyfile(backup, restoring)
                os.replace(restoring, target)
        db.info.pop(_CACHE, None)
    del snapshots[transaction]
    _discard_backups(files)


@event.listens_for(Session, "after_commit")
def _commit_files(db: Session) -> None:
    transaction = db.get_nested_transaction() or db.get_transaction()
    _finish_snapshot(db, transaction, restore=False)


@event.listens_for(Session, "after_rollback")
def _rollback_files(db: Session) -> None:
    # 此时保存点仍可定位；先恢复文件，再允许外层分支保存 BLOCKED 结果。
    transaction = db.get_nested_transaction() or db.get_transaction()
    _finish_snapshot(db, transaction, restore=True)


@event.listens_for(Session, "after_transaction_end")
def _end_files(db: Session, transaction) -> None:
    # Session.close() 也可能结束未提交事务，不一定触发 after_rollback。
    _finish_snapshot(db, transaction, restore=True)
    if transaction.parent is None:
        for pending in list(db.info.get(_SNAPSHOTS, {})):
            _finish_snapshot(db, pending, restore=False)
        db.info.pop(_SNAPSHOTS, None)
        db.info.pop(_CACHE, None)
        locks = db.info.pop(_LOCKS, {})
        for lock in locks.values():
            lock.release()
