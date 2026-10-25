"""规则库导入作业及其文件产物的级联清理。"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...entity.imports import ImportJob
from ...entity.operations import BackgroundOperation
from ...entity.scenarios import ScenarioVersion
from ..system.operation_types import OBJECT_IMPORT_JOB
from .storage import remove_storage_tree

ACTIVE_OPERATION_STATUSES = ("PENDING", "RUNNING")


def delete_import_job_entities(db: Session, jobs: Sequence[ImportJob]) -> None:
    """删除导入作业、后台命令及文件产物，但保留业务操作日志。

    调用方必须已经锁定作业行。已发布版本只解除导入来源引用，版本快照继续保留；
    正在执行的转换拒绝删除，避免后台线程在删除后继续写回结果。
    """
    if not jobs:
        return
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

    # 数据库提交前清理唯一的作业目录。缺失目录视为已经清理；路径仍由
    # resolve_storage_path 做根目录边界校验，避免数据库内容构造越界路径。
    for job in jobs:
        remove_storage_tree(f"imports/{job.job_no}")
    for operation in operations:
        remove_storage_tree(f"llm_responses/{operation.operation_no}")

    for operation in operations:
        db.delete(operation)
    for job in jobs:
        db.delete(job)
