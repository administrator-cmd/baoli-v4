"""导入作业列表与中止的可执行验证脚本。

本仓库没有测试框架（requirements.txt 无 pytest），所以这里写成纯标准库脚本：
    python test_import_job_list.py
每个用例直接连真实 MySQL（连接串与生产一致），前置数据自带、跑完自清理，
不留残余行。断言失败即非 0 退出。
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import delete, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import BackgroundOperation, FieldCandidate, ImportIssue, ImportJob, ImportSheet, OperationLog  # noqa: E402


# 与真实导入作业同量级的大 JSON 列：>256KB 的 workbook_ir_json 是这个 bug 的触发条件。
BIG_JSON = {"sheets": [{"rows": [{"cell": "x" * 200} for _ in range(2000)]}]}


def _make_job(db, *, suffix: str) -> ImportJob:
    job = ImportJob(
        job_no=f"TEST_LIST_{suffix}",
        source_filename="列表验证.xlsx",
        source_path=f"/tmp/{suffix}.xlsx",
        source_sha256="0" * 64,
        source_size=1024,
        profile_version="contract_review_v3",
        status="PENDING_DECISION",
        workbook_ir_json=BIG_JSON,
        proposals_json={"sheets": []},
        draft_revision=1,
    )
    db.add(job)
    return job


def _cleanup(db, job_no: str) -> None:
    """清掉本次用例自建的行。

    子表按 job_no 直接删，不依赖作业行还在：中止用例会先把作业删掉，
    那时再靠 job.id 找子表就漏了，会留下悬挂的 background_operation 与操作日志。
    """
    db.rollback()
    job = db.scalar(select(ImportJob).where(ImportJob.job_no == job_no))
    if job is not None:
        for model in (FieldCandidate, ImportIssue, ImportSheet):
            db.execute(delete(model).where(model.import_job_id == job.id))
        db.delete(job)
    for model in (BackgroundOperation, OperationLog):
        db.execute(delete(model).where(model.object_no == job_no))
    db.commit()


def test_abort_does_not_wait_for_the_holder_of_the_job_row_lock() -> None:
    """转换线程持有作业行锁时，中止必须立刻返回，不能等到转换自己跑完。

    回归：早期实现先取作业行锁再改命令，而 retry_conversion 会在整个转换期间
    （含 LLM 调用）持有该锁，于是中止请求会一直阻塞——那正是用户点「中止」要避免的。
    """
    import threading

    from app.services import import_service

    suffix = uuid.uuid4().hex[:12]
    job_no = f"TEST_LIST_{suffix}"
    holder = SessionLocal()
    db = SessionLocal()
    try:
        _make_job(holder, suffix=suffix)
        holder.flush()
        holder.add(
            BackgroundOperation(
                operation_no=f"OP_TEST_{suffix}",
                object_type="IMPORT_JOB",
                object_no=job_no,
                operation_type="IMPORT_CONVERT",
                idempotency_key=f"IDEM_TEST_{suffix}",
                status="RUNNING",
                request_hash="0" * 64,
                request_json={},
            )
        )
        holder.commit()
        # 模拟转换线程：retry_conversion 全程持有作业行锁。
        holder.execute(select(ImportJob).where(ImportJob.job_no == job_no).with_for_update()).one()

        outcome: list[object] = []

        def run() -> None:
            try:
                outcome.append(import_service.abort_import_job(db, job_no))
            except Exception as exc:  # noqa: BLE001 - 线程里必须把异常带回主线程
                outcome.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=5)

        assert not worker.is_alive(), "中止被作业行锁挡住了：它会一直等到转换结束才返回"
        assert outcome, "中止线程没有产生任何结果"
        assert isinstance(outcome[0], BackgroundOperation), f"中止失败：{outcome[0]!r}"
        assert outcome[0].status == "INTERRUPTED"
    finally:
        holder.rollback()
        holder.close()
        _cleanup(db, job_no)
        db.close()


def test_list_reports_active_operation_count_per_job() -> None:
    """列表必须回「有没有未结束的后台操作」——中止按钮能否点只由它决定。

    回归：转换期间作业状态不变（retry_conversion 全程持有作业行锁，结束时才提交），
    所以按作业状态判断「是否执行中」永远为假，中止按钮会一直不可点。
    """
    from app.services import admin_query_service

    suffix = uuid.uuid4().hex[:12]
    plain_suffix = uuid.uuid4().hex[:12]
    job_no = f"TEST_LIST_{suffix}"
    db = SessionLocal()
    try:
        _make_job(db, suffix=suffix)
        _make_job(db, suffix=plain_suffix)
        db.flush()
        db.add(
            BackgroundOperation(
                operation_no=f"OP_TEST_{suffix}",
                object_type="IMPORT_JOB",
                object_no=job_no,
                operation_type="IMPORT_CONVERT",
                idempotency_key=f"IDEM_TEST_{suffix}",
                status="RUNNING",
                request_hash="0" * 64,
                request_json={},
            )
        )
        db.commit()

        result = admin_query_service.list_import_jobs_page(
            db, filename="列表验证", status=None, created_from=None, created_to=None, page=1, page_size=200
        )
        counts = {item["job_no"]: item["active_operation_count"] for item in result["items"]}

        assert counts[f"TEST_LIST_{suffix}"] == 1, "有 RUNNING 操作的作业应报告 1 个活跃操作"
        assert counts[f"TEST_LIST_{plain_suffix}"] == 0, "没有后台操作的作业应报告 0"
    finally:
        _cleanup(db, job_no)
        _cleanup(db, f"TEST_LIST_{plain_suffix}")
        db.close()


def test_list_import_jobs_page_survives_wide_json_rows() -> None:
    """存在带大 JSON 列的作业时，管理分页列表必须能正常返回。

    回归：整行 SELECT * + MySQL 默认 256KB 排序缓冲会让 ORDER BY 直接报
    1038 Out of sort memory，列表接口 500。
    """
    from app.services import admin_query_service

    suffix = uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        _make_job(db, suffix=suffix)
        db.commit()

        result = admin_query_service.list_import_jobs_page(
            db,
            filename=None,
            status=None,
            created_from=None,
            created_to=None,
            page=1,
            page_size=20,
        )

        assert result["total"] >= 1, f"作业已入库，total 至少为 1，实际 {result['total']}"
        assert any(item["job_no"] == f"TEST_LIST_{suffix}" for item in result["items"]), (
            "新建的作业必须出现在第一页"
        )
    finally:
        _cleanup(db, f"TEST_LIST_{suffix}")
        db.close()


def test_import_job_summaries_survives_wide_json_rows() -> None:
    """兼容用的摘要接口同样不能因为大 JSON 列而 500。"""
    from app.services import import_service

    suffix = uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        _make_job(db, suffix=suffix)
        db.commit()

        summaries = import_service.import_job_summaries(db)

        assert any(item["job_no"] == f"TEST_LIST_{suffix}" for item in summaries)
    finally:
        _cleanup(db, f"TEST_LIST_{suffix}")
        db.close()


def test_abort_marks_pending_operation_interrupted_and_releases_the_lock() -> None:
    """中止未执行的后台操作：状态转 INTERRUPTED，活跃占用释放。"""
    from app.services import import_service

    suffix = uuid.uuid4().hex[:12]
    job_no = f"TEST_LIST_{suffix}"
    db = SessionLocal()
    try:
        _make_job(db, suffix=suffix)
        db.flush()
        db.add(
            BackgroundOperation(
                operation_no=f"OP_TEST_{suffix}",
                object_type="IMPORT_JOB",
                object_no=job_no,
                operation_type="IMPORT_CONVERT",
                idempotency_key=f"IDEM_TEST_{suffix}",
                status="PENDING",
                request_hash="0" * 64,
                request_json={},
            )
        )
        db.commit()

        operation = import_service.abort_import_job(db, job_no)

        assert operation.status == "INTERRUPTED", f"中止后应为 INTERRUPTED，实际 {operation.status}"
        assert operation.failure_code == "OPERATION_ABORTED"
        assert operation.finished_at is not None, "中止是终态，必须写入结束时间"

        # 占用已释放：此时删除作业不应再被 OPERATION_ALREADY_ACTIVE 挡住。
        from app.services import admin_query_service

        admin_query_service.delete_import_job(db, job_no)
        assert db.scalar(select(ImportJob).where(ImportJob.job_no == job_no)) is None
    finally:
        _cleanup(db, job_no)
        db.close()


def test_abort_rejects_when_no_operation_is_active() -> None:
    """没有活跃操作时中止必须明确报错，不能静默成功。"""
    from app.errors import BusinessError
    from app.services import import_service

    suffix = uuid.uuid4().hex[:12]
    job_no = f"TEST_LIST_{suffix}"
    db = SessionLocal()
    try:
        _make_job(db, suffix=suffix)
        db.commit()

        try:
            import_service.abort_import_job(db, job_no)
        except BusinessError as exc:
            assert exc.code == "OPERATION_NOT_ACTIVE", f"错误码应为 OPERATION_NOT_ACTIVE，实际 {exc.code}"
        else:
            raise AssertionError("没有活跃操作时中止必须报错")
    finally:
        _cleanup(db, job_no)
        db.close()


def test_background_runner_skips_an_operation_aborted_before_it_claims() -> None:
    """执行线程启动前已被中止的操作，线程不得再执行 runner。"""
    from app.services import background_service, import_service

    suffix = uuid.uuid4().hex[:12]
    job_no = f"TEST_LIST_{suffix}"
    db = SessionLocal()
    try:
        _make_job(db, suffix=suffix)
        db.flush()
        operation = BackgroundOperation(
            operation_no=f"OP_TEST_{suffix}",
            object_type="IMPORT_JOB",
            object_no=job_no,
            operation_type="IMPORT_CONVERT",
            idempotency_key=f"IDEM_TEST_{suffix}",
            status="PENDING",
            request_hash="0" * 64,
            request_json={},
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id

        import_service.abort_import_job(db, job_no)

        executed: list[int] = []

        def runner(session, command):  # noqa: ANN001, ANN202 - 测试替身
            executed.append(command.operation_id)
            return {}

        background_service._run_in_background(operation_id, runner)

        assert executed == [], "已被中止的操作不能再被认领执行"
        session = SessionLocal()
        try:
            current = session.get(BackgroundOperation, operation_id)
            assert current.status == "INTERRUPTED", f"状态不得被线程改写，实际 {current.status}"
        finally:
            session.close()
    finally:
        _cleanup(db, job_no)
        db.close()


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 验证脚本必须打印真实失败原因
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed at {datetime.now(UTC).isoformat()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
