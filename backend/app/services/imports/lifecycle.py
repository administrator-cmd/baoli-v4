"""规则库导入作业删除与后台转换中止。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...common.exceptions import BusinessError
from ...entity import ImportJob
from ..system.operation_log import record_operation
from datetime import UTC, datetime
from ...entity import BackgroundOperation
from ..system.background import ACTIVE_STATUSES
from ..system.operation_types import OBJECT_IMPORT_JOB
from ..shared.import_cleanup import delete_import_job_entities
from .lookup import _job_or_404


def delete_import_job(db: Session, job_no: str) -> None:
    """直接删除验证项目中的导入作业。

    验证项目不保留导入来源追溯；已发布版本解除对作业的来源引用后继续保留版本本身。
    作业的上传文件、后台命令和 LLM 原始响应目录同步清理；正在执行的后台转换必须先中止。
    """
    job = db.scalar(select(ImportJob).where(ImportJob.job_no == job_no).with_for_update())
    if not job:
        raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": job_no})
    record_operation(
        db,
        object_type="IMPORT_JOB",
        object_no=job_no,
        object_name=job.source_filename,
        operation_type="DELETE",
        detail={"job_no": job_no},
    )
    delete_import_job_entities(db, [job])
    db.commit()


def abort_import_job(db: Session, job_no: str) -> BackgroundOperation:
    """中止该导入作业尚未结束的后台操作。

    只把命令本身标成终态 INTERRUPTED 并释放活跃占用，让作业回到可重试 / 可删除的状态。这是
    「释放占用」而不是「回滚」：作业状态与草稿数据一概不改，重试时由 retry_conversion 按自己
    的草稿修订整体重建。没有活跃操作时明确报错，不静默成功。

    不强杀正在等待模型的线程；模型返回后写入前必须重新检查命令状态，中止结果不能覆盖草稿。
    模型调用期间不持有数据库事务。中止与转换落库都按「作业、命令」顺序加锁，
    保证中止提交以后，旧转换不能再写入候选或覆盖中止状态。
    """
    job = _job_or_404(db, job_no, lock=True)
    operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_type == OBJECT_IMPORT_JOB,
            BackgroundOperation.object_no == job_no,
            BackgroundOperation.status.in_(ACTIVE_STATUSES),
        )
        .order_by(BackgroundOperation.id.desc())
        .with_for_update()
    )
    if operation is None:
        raise BusinessError(
            "OPERATION_NOT_ACTIVE",
            "该导入作业没有正在进行的操作，无需中止",
            status_code=409,
            details={"job_no": job_no},
        )
    previous_status = operation.status
    operation.status = "INTERRUPTED"
    operation.failure_code = "OPERATION_ABORTED"
    operation.failure_message = "该操作已被人工中止，可重新发起转换"
    operation.finished_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_IMPORT_JOB,
        object_no=job_no,
        object_name=job.source_filename,
        operation_type="ABORT",
        detail={"job_no": job_no, "operation_no": operation.operation_no, "previous_status": previous_status},
    )
    db.commit()
    db.refresh(operation)
    return operation
