"""规则库导入作业及其文件产物的级联清理。"""

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...entity.imports import ImportJob
from ...entity.operations import BackgroundOperation
from ...entity.scenarios import ScenarioVersion
from ..system.operation_types import OBJECT_IMPORT_JOB
from .storage import resolve_storage_path

logger = logging.getLogger(__name__)

ACTIVE_OPERATION_STATUSES = ("PENDING", "RUNNING")


def _stage_tree(relative_path: str) -> tuple[Path, Path] | None:
    """把作业目录改名到同根的暂存位置；不存在时返回 None。

    改名是可恢复的删除动作：数据库提交失败时能改回原名，不依赖已删除文件的备份。
    """
    source = resolve_storage_path(relative_path)
    if not source.exists():
        return None
    if not source.is_dir():
        raise BusinessError(
            "STORAGE_PATH_INVALID", "待清理的业务存储路径不是目录",
            status_code=500, details={"path": relative_path},
        )
    staged = source.with_name(f".deleting-{source.name}-{uuid4().hex}")
    source.rename(staged)
    return source, staged


def restore_staged_trees(staged: Sequence[tuple[Path, Path]]) -> None:
    """数据库回滚时把暂存目录改回原名；必须在 rollback 之前调用。"""
    for original, temporary in reversed(staged):
        if temporary.exists():
            temporary.rename(original)


def discard_staged_trees(staged: Sequence[tuple[Path, Path]], *, object_no: str) -> None:
    """提交成功后删除暂存目录；清理失败区分「数据已删除」与「剩余目录待清理」。"""
    remaining = []
    for _original, temporary in staged:
        try:
            shutil.rmtree(temporary)
        except OSError:
            logger.exception("业务数据已删除，但文件清理失败 object_no=%s path=%s", object_no, temporary)
            remaining.append(str(temporary))
    if remaining:
        raise BusinessError(
            "IMPORT_FILE_CLEANUP_FAILED",
            "导入作业数据已删除，但仍有目录未清理，请按剩余路径排查",
            status_code=500,
            details={"object_no": object_no, "deleted": True, "remaining_directories": remaining},
        )


def delete_import_job_entities(db: Session, jobs: Sequence[ImportJob]) -> list[tuple[Path, Path]]:
    """删除导入作业、后台命令及文件产物，但保留业务操作日志。

    调用方必须已经锁定作业行。已发布版本只解除导入来源引用，版本快照继续保留；
    正在执行的转换拒绝删除，避免后台线程在删除后继续写回结果。
    文件目录在数据库提交前只改名暂存，由调用方在提交成功后清理、失败时改回。
    """
    if not jobs:
        return []
    job_nos = [job.job_no for job in jobs]
    job_ids = [job.id for job in jobs]
    operations = db.scalars(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_type == OBJECT_IMPORT_JOB,
            BackgroundOperation.object_no.in_(job_nos),
        )
        .with_for_update()
    ).all()
    active = [operation for operation in operations if operation.status in ACTIVE_OPERATION_STATUSES]
    if active:
        raise BusinessError(
            "IMPORT_JOB_OPERATION_ACTIVE",
            "关联导入作业仍有后台转换正在执行，请先等待完成或中止后再删除场景",
            status_code=409,
            details={
                "job_nos": job_nos,
                "operation_nos": [operation.operation_no for operation in active],
            },
        )

    versions = db.scalars(
        select(ScenarioVersion)
        .where(ScenarioVersion.import_job_id.in_(job_ids))
        .with_for_update()
    ).all()
    for version in versions:
        # 发布版本是独立快照，解除来源引用后继续保留。
        version.import_job_id = None

    # 数据库提交前只把唯一的作业目录改名暂存：提交失败可原样改回，不会出现
    # 「记录回滚了、源文件已永久删除」。路径仍由 resolve_storage_path 做根目录边界校验。
    staged: list[tuple[Path, Path]] = []
    try:
        for job in jobs:
            item = _stage_tree(f"imports/{job.job_no}")
            if item is not None:
                staged.append(item)
        for operation in operations:
            item = _stage_tree(f"llm_responses/{operation.operation_no}")
            if item is not None:
                staged.append(item)

        for operation in operations:
            db.delete(operation)
        for job in jobs:
            db.delete(job)
    except Exception:
        restore_staged_trees(staged)
        raise
    return staged
