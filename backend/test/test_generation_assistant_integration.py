"""显式启用的本地 MySQL 回归；仅创建/清理本测试作业，模型始终由 fixture 替代。

BAOLI_TEST_GENERATION_DB=1 python -m unittest discover -s test -p test_generation_assistant*.py -v
"""

import hashlib
import io
import os
import unittest
import uuid
from unittest.mock import patch

from fastapi import UploadFile
from openpyxl import Workbook, load_workbook
from sqlalchemy import delete, select

from app.common.exceptions import BusinessError
from app.core.database import SessionLocal
from app.core.transactions import commit_transaction, rollback_transaction
from app.domain.generation.document import GenerationDocument
from app.modules.imports.generation.crud import GenerationMessageCRUD, GenerationRevisionCRUD
from app.modules.imports.generation.dto import AssistantIssueAnswer
from app.modules.imports.generation.model import (
    RulebookGenerationExport, RulebookGenerationJob, RulebookGenerationMessage, RulebookGenerationRevision,
)
from app.modules.imports.generation.service.assistant import GenerationAssistantService
from app.modules.imports.generation.service.editing import GenerationEditingService
from app.modules.imports.generation.service.export import GenerationExportService
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.generation.service.lifecycle import GenerationLifecycleService
from app.modules.imports.generation.service.operations import GenerationOperationsService
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.imports.generation.service.upload import GenerationUploadService
from app.modules.system.operation.execution import OperationExecution
from app.modules.system.operation.model import BackgroundOperation, OperationLog
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION
from app.runtime.schema_check import verify_schema
from app.services.shared.storage import resolve_storage_path
from test_generation_assistant import document


@unittest.skipUnless(os.environ.get("BAOLI_TEST_GENERATION_DB") == "1", "需显式启用本地数据库回归")
class AssistantPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        verify_schema()

    def setUp(self):
        self.job_no = None
        self.job_id = None
        self.paths = set()
        # 测试即使误触额外模型调用也必须失败，不能越过测试边界。
        self.llm = patch(
            "app.modules.imports.generation.assistant_execution.call_json",
            side_effect=AssertionError("测试禁止真实模型调用"),
        ).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.cleanup_fixture)
        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active.title = "原始规则"
        workbook.active.append(["名称", "业务内容"])
        workbook.active.append(["违约金", "应计算违约金，原文未给算法"])
        workbook.save(stream)
        workbook.close()
        stream.seek(0)
        with SessionLocal() as db:
            job, _ = GenerationUploadService(db).create_job(
                UploadFile(file=stream, filename="助手回归专用.xlsx"), business_background="仅用于本地回归",
                generation_requirements="", idempotency_key="assistant-test:" + uuid.uuid4().hex,
            )
            self.job_no, self.job_id = job.job_no, job.id
            self.paths.add(resolve_storage_path(job.source_path))
            self.job_directory = resolve_storage_path(job.source_path).parent
            job.revision = 1
            job.document_json = document().model_dump(mode="json")
            GenerationHistoryService(db).snapshot(job, origin="INITIAL", summary="回归测试原版")
            commit_transaction(db)

    def cleanup_fixture(self):
        if not self.job_id:
            return
        with SessionLocal() as db:
            exports = db.scalars(select(RulebookGenerationExport).where(
                RulebookGenerationExport.generation_job_id == self.job_id,
            )).all()
            self.paths.update(resolve_storage_path(item.relative_path) for item in exports)
            for model in (RulebookGenerationMessage, RulebookGenerationExport, RulebookGenerationRevision):
                db.execute(delete(model).where(model.generation_job_id == self.job_id))
            db.execute(delete(BackgroundOperation).where(BackgroundOperation.object_no == self.job_no))
            db.execute(delete(OperationLog).where(
                OperationLog.object_type == OBJECT_RULEBOOK_GENERATION, OperationLog.object_no == self.job_no,
            ))
            db.execute(delete(RulebookGenerationJob).where(RulebookGenerationJob.id == self.job_id))
            commit_transaction(db)
        root = self.job_directory
        for path in self.paths:
            self.assertTrue(path.resolve().is_relative_to(root))
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent.is_relative_to(root) and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    def submit(self, *, message="整理标题", revision=1, key=None, issue_id=None, issue_answers=None):
        with patch.object(OperationExecution, "dispatch") as dispatch, SessionLocal() as db:
            operation, reused = GenerationAssistantService(db).submit(
                self.job_no, message=message, expected_revision=revision, idempotency_key=key or uuid.uuid4().hex,
                issue_id=issue_id, issue_answers=issue_answers,
            )
            operation_id = operation.id
            self.assertFalse(dispatch.called, "提交事务前不能调度模型")
            commit_transaction(db)
            return operation_id, reused, dispatch.call_args.args[2] if dispatch.called else None

    def run_operation(self, operation_id, runner, replies):
        self.llm.reset_mock()
        self.llm.side_effect = replies
        OperationExecution()._run_in_background(operation_id, runner)
        with SessionLocal() as db:
            return db.get(BackgroundOperation, operation_id)

    def current(self):
        with SessionLocal() as db:
            job = GenerationQueryService(db).get_job(self.job_no)
            return job.revision, job.document_json

    def test_actual_edit_atomic_history_messages_and_idempotent_replay(self):
        key = uuid.uuid4().hex
        operation_id, reused, runner = self.submit(key=key)
        self.assertFalse(reused)
        replay_id, reused, replay_runner = self.submit(key=key)
        self.assertEqual(replay_id, operation_id)
        self.assertTrue(reused)
        self.assertIsNone(replay_runner)
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已整理标题", "edits": [{"op": "set_document", "title": "合同规则库（整理版）"}]},
            {"problems": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        self.assertEqual(operation.phase, "RESULT_READY")
        self.assertEqual(self.llm.call_count, 2)
        review_input = self.llm.call_args_list[1].args[0]
        self.assertEqual(review_input["changes"], [{"path": ["title"], "before": "合同业务规则库", "after": "合同规则库（整理版）"}])
        self.assertNotIn("after", review_input, "复核不应重复发送整本候选文档")
        self.assertEqual(self.current()[0], 2)
        with SessionLocal() as db:
            messages = GenerationMessageCRUD(db).page(self.job_id, limit=10)
            self.assertEqual([(msg.role, msg.revision) for msg, _ in messages], [("ASSISTANT", 2), ("USER", 1)])
            history = GenerationRevisionCRUD(db)
            self.assertEqual(history.by_revision(self.job_id, 1).document_json["title"], "合同业务规则库")
            self.assertEqual(history.by_revision(self.job_id, 2).origin, "AI")
        # 结果已落库但客户端响应丢失时，旧版本的同键重发仍应返回原命令。
        self.assertTrue(self.submit(key=key)[1])
        self.assertEqual(self.llm.call_count, 2)

    def add_issue(self, *additional):
        with SessionLocal() as db:
            job = GenerationQueryService(db).get_job(self.job_no)
            payload = dict(job.document_json)
            payload["issues"] = [{
                "issue_id": "UNC-001", "title": "违约金算法", "detail": "原文没有算法",
                "affected_row_ids": ["r1"], "status": "OPEN", "resolution": "",
                "source": {"kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 2}},
            }, *additional]
            job.document_json = payload
            GenerationRevisionCRUD(db).by_revision(self.job_id, 1).document_json = payload
            commit_transaction(db)
            return payload["issues"][0]

    def test_batch_applies_two_answers_together_and_preserves_unanswered_issue(self):
        source = {"kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 2}}
        second = {"issue_id": "UNC-002", "title": "主合同附件", "affected_row_ids": ["m1"], "source": source}
        unanswered = {"issue_id": "UNC-003", "title": "违约金上限", "affected_row_ids": ["r1"], "source": source}
        first = self.add_issue(second, unanswered)
        key = uuid.uuid4().hex
        answers = [AssistantIssueAnswer(issue_id="UNC-001", answer="按合同金额的1%计算"),
                   AssistantIssueAnswer(issue_id="UNC-002", answer="需要附签字页")]
        operation_id, _, runner = self.submit(message=None, key=key, issue_answers=answers)
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已补充违约金算法和主合同附件要求", "edits": [
                {"op": "update_cells", "row_id": "r1", "cells": {"body": "违约金按合同金额的1%计算"}},
                {"op": "update_cells", "row_id": "m1", "cells": {"body": "主合同需附签字页"}},
                {"op": "upsert_issue", "issue": {**first, "status": "RESOLVED", "resolution": answers[0].answer}},
                {"op": "upsert_issue", "issue": {**second, "status": "RESOLVED", "resolution": answers[1].answer}},
            ]},
            {"problems": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        self.assertEqual(self.llm.call_count, 2)
        for call in self.llm.call_args_list:
            self.assertEqual({item["issue_id"]: item["answer"] for item in call.args[0]["issue_answers"]},
                             {item.issue_id: item.answer for item in answers})
        revision, payload = self.current()
        self.assertEqual(revision, 2)
        self.assertEqual({item["issue_id"]: item["status"] for item in payload["issues"]},
                         {"UNC-001": "RESOLVED", "UNC-002": "RESOLVED", "UNC-003": "OPEN"})
        self.assertEqual(self.submit(message=None, key=key, issue_answers=answers[::-1]), (operation_id, True, None))

    def test_batch_rejects_model_closing_unanswered_issue_without_saving(self):
        first = self.add_issue({"issue_id": "UNC-002", "title": "违约金上限", "affected_row_ids": ["r1"],
                                "source": {"kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 2}}})
        original = self.current()
        answers = [AssistantIssueAnswer(issue_id=first["issue_id"], answer="按合同金额的1%计算")]
        operation_id, _, runner = self.submit(message=None, issue_answers=answers)
        wrong = {"kind": "EDIT", "message": "已解决", "edits": [{"op": "upsert_issue", "issue": {
            **original[1]["issues"][1], "status": "RESOLVED", "resolution": "假定没有上限",
        }}]}
        operation = self.run_operation(operation_id, runner, [wrong, wrong])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(self.current(), original)
        self.assertEqual(self.llm.call_args.args[0]["correction"]["problems"][0]["code"], "ASSISTANT_INVALID_EDIT")

    def test_missing_issue_is_rejected_before_accepting_an_operation(self):
        with self.assertRaises(BusinessError) as failure:
            self.submit(message="按合同约定", issue_id="missing")
        self.assertEqual(failure.exception.code, "GENERATION_ISSUE_NOT_FOUND")
        self.assertEqual(self.current()[0], 1)
        self.assertFalse(self.llm.called)

    def test_insufficient_issue_answer_keeps_document_and_asks_one_question(self):
        self.add_issue()
        original = self.current()
        operation_id, _, runner = self.submit(message="正常计算就行", issue_id="UNC-001")
        operation = self.run_operation(operation_id, runner, [
            {"kind": "CLARIFY", "message": "请补充违约金的计算公式或合同约定依据。", "edits": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        self.assertEqual(operation.result_json["reply_kind"], "CLARIFY")
        self.assertEqual(self.current(), original)
        self.assertEqual(self.llm.call_count, 1)

    def test_issue_answer_cannot_succeed_with_explanation_or_noop(self):
        self.add_issue()
        original = self.current()
        operation_id, _, runner = self.submit(message="按合同金额的1%计算", issue_id="UNC-001")
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EXPLAIN", "message": "已记录你的补充", "edits": []},
            {"kind": "EDIT", "message": "已处理", "edits": []},
        ])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(self.current(), original)

    def test_resolving_only_issue_is_repaired_before_body_is_saved(self):
        issue = self.add_issue()
        operation_id, _, runner = self.submit(message="违约金按合同金额的1%计算", issue_id=issue["issue_id"])
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已处理", "edits": [{"op": "upsert_issue", "issue": {
                **issue, "status": "RESOLVED", "resolution": "用户补充按合同金额的1%计算",
            }}]},
            {"problems": ["仅记录了答案，相关正文仍未补充算法"]},
            {"kind": "EDIT", "message": "已将1%算法写入正文", "edits": [
                {"op": "update_cells", "row_id": "r1", "cells": {"body": "违约金按合同金额的1%计算"}},
            ]},
            {"problems": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        revision, payload = self.current()
        self.assertEqual(revision, 2)
        self.assertEqual(payload["issues"][0]["status"], "RESOLVED")
        self.assertEqual(payload["sheets"][0]["rows"][1]["cells"]["body"], "违约金按合同金额的1%计算")

    def test_explanation_and_noop_do_not_create_revisions(self):
        for response in (
            {"kind": "EXPLAIN", "message": "原文要求计算违约金，但没有提供算法", "edits": []},
            {"kind": "EDIT", "message": "无需修改", "edits": [{"op": "set_document", "title": "合同业务规则库"}]},
        ):
            operation_id, _, runner = self.submit(message="说明或保留当前标题")
            operation = self.run_operation(operation_id, runner, [response])
            self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
            self.assertEqual(self.llm.call_count, 1)
            self.assertEqual(self.current()[0], 1)
        with SessionLocal() as db:
            self.assertEqual(len(GenerationRevisionCRUD(db).summaries(self.job_id)), 1)

    def test_invalid_edits_fail_without_writing_partial_document(self):
        original = self.current()
        operation_id, _, runner = self.submit()
        response = {"kind": "EDIT", "message": "已修改", "edits": [{"op": "remove_sheet", "sheet_id": "s1"}]}
        operation = self.run_operation(operation_id, runner, [response, response])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(operation.failure_code, "ASSISTANT_INVALID_EDIT")
        self.assertEqual(self.current(), original)
        with SessionLocal() as db:
            self.assertIsNone(GenerationMessageCRUD(db).for_operation(operation_id, "ASSISTANT"))

    def test_gateway_failure_is_not_retried_as_an_edit_error(self):
        operation_id, _, runner = self.submit()
        operation = self.run_operation(operation_id, runner, [BusinessError("LLM_TIMEOUT", "模型超时", status_code=502)])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(operation.failure_code, "LLM_TIMEOUT")
        self.assertEqual(self.llm.call_count, 1)
        self.assertEqual(self.current()[0], 1)
        # 用户显式重试接受新的操作，上一条失败事实仍保留。
        retry_id, _, retry_runner = self.submit()
        self.assertNotEqual(operation_id, retry_id)
        retry = self.run_operation(retry_id, retry_runner, [{"kind": "EXPLAIN", "message": "已读取", "edits": []}])
        self.assertEqual(retry.status, "SUCCEEDED")

    def test_active_operation_and_stale_revision_cannot_overwrite(self):
        operation_id, _, runner = self.submit()
        with self.assertRaises(BusinessError) as conflict:
            self.submit(message="另一条要求")
        self.assertEqual(conflict.exception.code, "OPERATION_ALREADY_ACTIVE")
        with SessionLocal() as db:
            with self.assertRaises(BusinessError) as conflict:
                GenerationHistoryService(db).restore(self.job_no, expected_revision=1, target_revision=1)
            self.assertEqual(conflict.exception.code, "OPERATION_ALREADY_ACTIVE")
            rollback_transaction(db)
        self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已整理", "edits": [{"op": "set_document", "title": "新标题"}]},
            {"problems": []},
        ])
        with self.assertRaises(BusinessError) as conflict:
            self.submit(revision=1)
        self.assertEqual(conflict.exception.code, "GENERATION_REVISION_CONFLICT")
        self.assertEqual(self.current()[1]["title"], "新标题")

    def test_delete_rejects_active_operation_without_removing_files(self):
        self.submit()
        with SessionLocal() as db:
            with self.assertRaises(BusinessError) as conflict:
                GenerationLifecycleService(db).delete_job(self.job_no)
            self.assertEqual(conflict.exception.code, "GENERATION_OPERATION_ACTIVE")
            rollback_transaction(db)
        self.assertTrue(all(path.is_file() for path in self.paths))
        self.assertEqual(self.current()[0], 1)

    def test_delete_rolls_back_then_removes_files_and_records_on_commit(self):
        operation_id, _, runner = self.submit()
        self.run_operation(operation_id, runner, [{"kind": "EXPLAIN", "message": "已读取", "edits": []}])
        with SessionLocal() as db:
            export = GenerationExportService(db).create_export(self.job_no, expected_revision=1)
            self.paths.add(resolve_storage_path(export.relative_path))
            commit_transaction(db)

        with SessionLocal() as db:
            try:
                GenerationLifecycleService(db).delete_job(self.job_no)
                self.assertFalse(self.job_directory.exists())
            finally:
                rollback_transaction(db)
        self.assertTrue(all(path.is_file() for path in self.paths))
        self.assertEqual(self.current()[0], 1)

        with SessionLocal() as db:
            try:
                GenerationLifecycleService(db).delete_job(self.job_no)
                commit_transaction(db)
            except Exception:
                rollback_transaction(db)
                raise
            with self.assertRaises(BusinessError) as missing:
                GenerationQueryService(db).get_job(self.job_no)
            self.assertEqual(missing.exception.code, "GENERATION_JOB_NOT_FOUND")
            self.assertIsNone(db.get(BackgroundOperation, operation_id))
        self.assertFalse(self.job_directory.exists())

    def test_manual_save_historical_export_and_restore_create_new_version(self):
        old_revision, payload = self.current()
        payload["sheets"][0]["rows"][1]["cells"]["body"] = "应计算违约金，算法待业务明确；保留原要求"
        with SessionLocal() as db:
            job = GenerationEditingService(db).save_document(self.job_no, expected_revision=old_revision, document_payload=payload)
            self.assertEqual(job.revision, 2)
            commit_transaction(db)
        for revision in (1, 2):
            with SessionLocal() as db:
                service = GenerationExportService(db)
                export = service.create_export(self.job_no, expected_revision=revision)
                self.assertEqual(export.revision, revision)
                path = resolve_storage_path(export.relative_path)
                self.paths.add(path)
                workbook = load_workbook(path)
                try:
                    values = [cell.value for row in workbook["合同业务"] for cell in row]
                finally:
                    workbook.close()
                expected = "应计算违约金，原文未给算法" if revision == 1 else payload["sheets"][0]["rows"][1]["cells"]["body"]
                self.assertIn(expected, values)
                export_id = export.id
                commit_transaction(db)
                self.assertEqual(service.create_export(self.job_no, expected_revision=revision).id, export_id)
                commit_transaction(db)
        with SessionLocal() as db:
            restored = GenerationHistoryService(db).restore(self.job_no, expected_revision=2, target_revision=1)
            self.assertEqual(restored.revision, 3)
            self.assertEqual(restored.document_json["sheets"][0]["rows"][1]["cells"]["body"], "应计算违约金，原文未给算法")
            commit_transaction(db)
            self.assertEqual(len(GenerationRevisionCRUD(db).summaries(self.job_id)), 3)
        self.llm.assert_not_called()

    def test_legacy_download_uses_frozen_snapshot_and_preserves_original_file(self):
        with SessionLocal() as db:
            service = GenerationExportService(db)
            export = service.create_export(self.job_no, expected_revision=1)
            path = resolve_storage_path(export.relative_path)
            self.paths.add(path)
            # 模拟升级前已经存在的冻结文件；当前页面需能直接下载新排版。
            old = Workbook()
            old.active["A1"] = "旧版装饰性标题"
            old.save(path)
            old.close()
            export.export_format_version = "rulebook_generation_xlsx_v5"
            export.size = path.stat().st_size
            export.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            export_no, frozen_hash = export.export_no, export.sha256
            commit_transaction(db)
        revision, payload = self.current()
        payload["sheets"][0]["rows"][1]["cells"]["body"] = "后续修订的正文不能混入历史下载"
        with SessionLocal() as db:
            GenerationEditingService(db).save_document(self.job_no, expected_revision=revision, document_payload=payload)
            commit_transaction(db)
        with SessionLocal() as db:
            service = GenerationExportService(db)
            stream, export = service.source_layout_file(self.job_no, export_no)
            self.assertIsInstance(stream, io.BytesIO)
            workbook = load_workbook(stream)
            try:
                values = [cell.value for row in workbook["合同业务"] for cell in row]
                self.assertIn("应计算违约金，原文未给算法", values)
                self.assertNotIn("后续修订的正文不能混入历史下载", values)
                self.assertEqual(workbook["合同业务"]["A1"].value, "编制编号")
            finally:
                workbook.close()
            self.assertEqual(export.export_format_version, "rulebook_generation_xlsx_v5")
            frozen_path, _ = service.export_file(self.job_no, export_no)
            self.assertEqual(hashlib.sha256(frozen_path.read_bytes()).hexdigest(), frozen_hash)
            self.assertEqual(len(GenerationRevisionCRUD(db).summaries(self.job_id)), 2)
            # 原件损坏时不能静默换一套样式，冻结历史仍可下载。
            source = service._layout_source_path(GenerationQueryService(db).get_job(self.job_no))
            source.write_bytes(b"changed")
            with self.assertRaises(BusinessError) as failure:
                service.source_layout_file(self.job_no, export_no)
            self.assertEqual(failure.exception.code, "GENERATION_SOURCE_CHANGED")
            self.assertEqual(service.export_file(self.job_no, export_no)[0], frozen_path)
        self.llm.assert_not_called()

    def test_review_failure_does_not_commit_candidate(self):
        original = self.current()
        operation_id, _, runner = self.submit()
        response = {"kind": "EDIT", "message": "已修改", "edits": [{"op": "set_document", "title": "候选标题"}]}
        operation = self.run_operation(operation_id, runner, [
            response, {"problems": ["本地测试复核拒绝候选内容"]},
            response, {"problems": ["仍不符合本次要求"]},
        ])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(operation.failure_code, "ASSISTANT_REVIEW_FAILED")
        self.assertIn("一致性复核", operation.failure_message)
        self.assertEqual(self.llm.call_count, 4)
        self.assertEqual(self.current(), original)

    def test_deletion_repairs_stale_descriptions_then_reviews_before_save(self):
        issue = self.add_issue()
        original = self.current()[1]
        operation_id, _, runner = self.submit(message="这个规则去除掉，这是错误规则", issue_id=issue["issue_id"])
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已删除错误规则及其关联", "edits": [
                {"op": "remove_row", "row_id": "r1"},
                {"op": "upsert_issue", "issue": {**issue, "affected_row_ids": [], "status": "RESOLVED",
                    "detail": "规则、关联和业务备注已清空", "resolution": "按用户要求删除错误规则"}},
            ]},
            {"problems": ["实际仅删除规则和关联，材料业务备注仍存在；表说明需与当前内容一致"], "text_repairs": [
                {"target": "issue_detail", "target_id": "UNC-001", "text": "错误规则及其关联已移除，其余业务内容保留"},
                {"target": "issue_resolution", "target_id": "UNC-001", "text": "依据本次用户明确删除要求处理"},
                {"target": "sheet_description", "target_id": "s1", "text": "保留材料适用说明和主体字典"},
                {"target": "document_summary", "target_id": "document", "text": "按用户要求移除错误的违约金规则，保留材料和字典"},
            ]},
            {"problems": [], "text_repairs": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        self.assertEqual(self.llm.call_count, 3, "说明矛盾直接补修，再独立复核，无须重新调用全文编辑器")
        revision, saved = self.current()
        self.assertEqual(revision, 2)
        self.assertEqual([row["row_id"] for row in saved["sheets"][0]["rows"]], ["m1", "d1"])
        self.assertEqual(saved["sheets"][0]["rows"][0]["cells"], original["sheets"][0]["rows"][0]["cells"])
        self.assertEqual(saved["issues"][0]["detail"], "错误规则及其关联已移除，其余业务内容保留")
        self.assertEqual(saved["issues"][0]["resolution"], "依据本次用户明确删除要求处理")
        self.assertEqual(saved["issues"][0]["status"], "RESOLVED")
        self.assertEqual(saved["summary"], "按用户要求移除错误的违约金规则，保留材料和字典")
        final_review = self.llm.call_args_list[2].args[0]
        self.assertEqual(final_review["before"], GenerationDocument.model_validate(original).model_dump(mode="json"))
        self.assertTrue(any(change["after"] == "保留材料适用说明和主体字典" for change in final_review["changes"]))
        with SessionLocal() as db:
            self.assertEqual(len(GenerationRevisionCRUD(db).summaries(self.job_id)), 2)

    def test_failed_review_after_text_repair_keeps_original_version(self):
        issue = self.add_issue()
        original = self.current()
        operation_id, _, runner = self.submit(message="明确违约金算法", issue_id=issue["issue_id"])
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已处理", "edits": [{"op": "upsert_issue", "issue": {
                **issue, "detail": "算法已经补齐", "status": "RESOLVED", "resolution": "声称已处理",
            }}]},
            {"problems": ["算法实际未补齐，说明必须如实记录"], "text_repairs": [
                {"target": "issue_detail", "target_id": "UNC-001", "text": "算法仍未明确"},
            ]},
            {"problems": ["仍无算法，不能将事项标为已明确"]},
            {"kind": "EDIT", "message": "已处理", "edits": []},
            {"problems": ["仍无算法，不能将事项标为已明确"]},
        ])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(operation.failure_code, "ASSISTANT_REVIEW_FAILED")
        self.assertEqual(self.current(), original)
        repair_input = self.llm.call_args_list[3].args[0]
        self.assertEqual(repair_input["document"]["issues"][0]["detail"], "算法仍未明确")
        self.assertNotIn("previous_response", repair_input)
        self.assertNotIn("仍无算法", operation.failure_message, "技术诊断保留在日志，不铺到用户提示")

    def test_review_cannot_claim_pass_while_requesting_unapplied_repairs(self):
        original = self.current()
        operation_id, _, runner = self.submit()
        response = {"kind": "EDIT", "message": "已修改", "edits": [{"op": "set_document", "title": "候选标题"}]}
        invalid_review = {"problems": [], "text_repairs": [{
            "target": "sheet_description", "target_id": "s1", "text": "尚未落实的修补",
        }]}
        operation = self.run_operation(operation_id, runner, [response, invalid_review, response, invalid_review])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(self.current(), original)

    def test_repair_keeps_first_round_body_and_updates_issue_atomically(self):
        with SessionLocal() as db:
            job = GenerationQueryService(db).get_job(self.job_no)
            payload = job.document_json.copy()
            payload["issues"] = [{
                "issue_id": "UNC-002", "title": "违约金算法未明确", "detail": "未提供算法",
                "affected_row_ids": ["r1"], "status": "OPEN", "resolution": "",
                "source": {"kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 2}},
            }]
            job.document_json = payload
            GenerationRevisionCRUD(db).by_revision(self.job_id, 1).document_json = payload
            commit_transaction(db)
        operation_id, _, runner = self.submit(message="违约金按合同约定计算", issue_id="UNC-002")
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "已补充计算说明", "edits": [
                {"op": "update_cells", "row_id": "r1", "cells": {"body": "违约金按合同约定计算"}},
                {"op": "remove_row", "row_id": "d1"},
            ]},
            {"problems": ["正文已经补充，但 UNC-002 仍称未提供算法，请同步解决该事项"]},
            {"kind": "EDIT", "message": "已同步正文和事项", "edits": [{"op": "upsert_issue", "issue": {
                **payload["issues"][0], "status": "RESOLVED", "detail": "用户已明确依据合同约定",
                "resolution": "按本次用户答复补充正文", "source": {"kind": "USER_INPUT", "note": "本次用户明确答复"},
            }}]},
            {"problems": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        revision, saved = self.current()
        self.assertEqual(revision, 2)
        self.assertEqual(saved["sheets"][0]["rows"][1]["cells"]["body"], "违约金按合同约定计算")
        self.assertNotIn("d1", [row["row_id"] for row in saved["sheets"][0]["rows"]])
        self.assertEqual(saved["issues"][0]["status"], "RESOLVED")
        self.assertEqual(self.llm.call_args_list[0].args[0]["issue_answer"]["issue_id"], "UNC-002")
        self.assertEqual(self.llm.call_args_list[0].args[0]["issue_answer"]["answer"], "违约金按合同约定计算")
        repair = self.llm.call_args_list[2].args[0]
        self.assertEqual(repair["correction"]["mode"], "REPAIR_CANDIDATE")
        self.assertEqual(repair["document"]["sheets"][0]["rows"][1]["cells"]["body"], "违约金按合同约定计算")
        final_review = self.llm.call_args_list[3].args[0]
        self.assertEqual(final_review["issue_answer"]["issue_id"], "UNC-002")
        self.assertEqual(final_review["before"]["sheets"][0]["rows"][1]["cells"]["body"], "应计算违约金，原文未给算法")
        with SessionLocal() as db:
            self.assertEqual(len(GenerationRevisionCRUD(db).summaries(self.job_id)), 2)
            self.assertEqual(GenerationRevisionCRUD(db).by_revision(self.job_id, 2).document_json, saved)

    def test_structurally_invalid_edits_retry_from_unchanged_base(self):
        operation_id, _, runner = self.submit()
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "修改", "edits": [
                {"op": "set_document", "title": "不能留下的半成品"},
                {"op": "update_cells", "row_id": "missing", "cells": {"name": "错误"}},
            ]},
            {"kind": "EDIT", "message": "已修正", "edits": [{"op": "set_document", "title": "完整有效标题"}]},
            {"problems": []},
        ])
        self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
        retry = self.llm.call_args_list[1].args[0]
        self.assertEqual(retry["correction"]["mode"], "REPLACE_INVALID_EDITS")
        self.assertEqual(retry["document"]["title"], "合同业务规则库")
        self.assertEqual(self.current()[1]["title"], "完整有效标题")

    def test_explanation_cannot_save_rejected_candidate(self):
        original = self.current()
        operation_id, _, runner = self.submit()
        operation = self.run_operation(operation_id, runner, [
            {"kind": "EDIT", "message": "修改", "edits": [{"op": "set_document", "title": "有问题的候选"}]},
            {"problems": ["需纠正候选"]},
            {"kind": "EXPLAIN", "message": "已经修好", "edits": []},
        ])
        self.assertEqual(operation.status, "FAILED")
        self.assertEqual(self.current(), original)

    def test_interruption_during_model_call_rejects_late_result(self):
        original = self.current()
        operation_id, _, runner = self.submit()

        def interrupted(*args, **kwargs):
            with SessionLocal() as db:
                # 模型调用时另一事务能够取得对象锁；随后模拟操作被中止。
                GenerationQueryService(db).get_job(self.job_no, lock=True)
                db.get(BackgroundOperation, operation_id).status = "INTERRUPTED"
                commit_transaction(db)
            return {"kind": "EXPLAIN", "message": "迟到结果", "edits": []}

        self.llm.side_effect = interrupted
        OperationExecution()._run_in_background(operation_id, runner)
        self.assertEqual(self.current(), original)
        with SessionLocal() as db:
            self.assertEqual(db.get(BackgroundOperation, operation_id).status, "INTERRUPTED")
            self.assertIsNone(GenerationMessageCRUD(db).for_operation(operation_id, "ASSISTANT"))

    def test_initial_workflow_retries_failure_then_saves_numbered_snapshot(self):
        with SessionLocal() as db:
            db.execute(delete(RulebookGenerationRevision).where(RulebookGenerationRevision.generation_job_id == self.job_id))
            job = GenerationQueryService(db).get_job(self.job_no, lock=True)
            job.document_json, job.revision = None, 0
            commit_transaction(db)

        def accept():
            with patch.object(OperationExecution, "dispatch") as dispatch, SessionLocal() as db:
                job = GenerationQueryService(db).get_job(self.job_no, lock=True)
                target, operation, _ = GenerationOperationsService(db).retry_generation(job, idempotency_key=uuid.uuid4().hex)
                self.assertEqual(target.job_no, self.job_no)
                commit_transaction(db)
                return operation.id, dispatch.call_args.args[2]

        with patch("app.modules.imports.generation.service.llm.call_json", side_effect=BusinessError("LLM_TIMEOUT", "首轮超时", status_code=502)):
            operation_id, runner = accept()
            OperationExecution()._run_in_background(operation_id, runner)
        self.assertEqual(self.current(), (0, None))
        initial = document().model_dump(mode="json")
        requirement = {"requirement_id": "req1", "text": "应计算违约金，原文未给算法", "category": "合同要求", "source": initial["sheets"][0]["rows"][1]["source"]}
        initial["requirements"] = [requirement]
        initial["sheets"][0]["rows"][1]["source"]["requirement_ids"] = ["req1"]
        inventory = {"schema_version": initial["schema_version"], "requirements": [requirement], "unclear_items": []}
        with patch("app.modules.imports.generation.service.llm.call_json", side_effect=[inventory, initial, BusinessError("LLM_TIMEOUT", "复核超时")]) as model:
            operation_id, runner = accept()
            OperationExecution()._run_in_background(operation_id, runner)
            self.assertEqual(model.call_count, 3)
        self.assertEqual(self.current(), (0, None))
        with SessionLocal() as db:
            service = GenerationQueryService(db)
            view = service.job_view(service.get_job(self.job_no))
            self.assertIsNone(view["document"])
            self.assertFalse(view["can_edit"])
            with self.assertRaises(BusinessError) as denied:
                GenerationExportService(db).create_export(self.job_no, expected_revision=0)
            self.assertEqual(denied.exception.code, "GENERATION_DOCUMENT_MISSING")
        with patch("app.modules.imports.generation.service.llm.call_json", side_effect=[inventory, initial, {"problems": []}]) as model:
            operation_id, runner = accept()
            OperationExecution()._run_in_background(operation_id, runner)
            self.assertEqual(model.call_count, 3)
            self.assertIn(":requirements:", model.call_args_list[0].kwargs["idempotency_key"])
        with SessionLocal() as db:
            operation = db.get(BackgroundOperation, operation_id)
            self.assertEqual(operation.status, "SUCCEEDED", operation.failure_message)
            self.assertEqual(operation.phase, "RESULT_READY")
            snapshot = GenerationRevisionCRUD(db).by_revision(self.job_id, 1)
            self.assertEqual(snapshot.origin, "INITIAL")
            self.assertEqual(snapshot.document_json["sheets"][0]["rows"][1]["business_code"], "RUL-001")
        self.assertEqual(self.current()[0], 1)


if __name__ == "__main__":
    unittest.main()
