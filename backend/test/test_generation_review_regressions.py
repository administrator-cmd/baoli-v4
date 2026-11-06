"""审查缺陷回归：模型、事务及数据库入口均模拟，只操作内存和临时 Excel。"""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

from app.common.exceptions import BusinessError
from app.domain.file_quality import check_file_size
from app.domain.generation_assistant import ReviewTextRepair, apply_edits, apply_review_text_repairs, document_changes
from app.domain.material_precheck import PRECHECK_KEY
from app.domain.rulebook_generation import GenerationColumn, GenerationIssue, GenerationSource, mark_user_edits, normalize_business_codes
from app.modules.audit.execution.service.commands import AuditExecutionCommandsService
from app.modules.audit.execution.service.material_precheck import AuditMaterialPrecheckService
from app.modules.imports.generation.assistant_execution import GenerationAssistantExecution
from app.modules.imports.generation.service.llm import GenerationLlmService
from app.modules.imports.generation.service.upload import GenerationUploadService
from app.modules.imports.generation.service.workbook_layout import GenerationWorkbookLayout
from test_generation_assistant import document, edit


class FieldReviewPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.task = SimpleNamespace(id=1, task_no="task", current_node="node", status="WAITING_FIELD_REVIEW", input_revision=4)
        self.state = {"input_revision": 4, "status": "READY", "blocked_materials": {"bad": {"upload_id": 9}}, "outcomes": []}
        self.execution = SimpleNamespace(id=2, audit_task_id=1, node_code="node", status="WAITING_FIELD_REVIEW",
                                         result_json={PRECHECK_KEY: deepcopy(self.state), "attempt_no": 1})

    def test_fact_only_change_preserves_passed_and_blocked_materials(self):
        self.task.input_revision = 5
        service = AuditMaterialPrecheckService(None)
        service.retain_after_field_review(self.task, self.execution, previous_revision=4)
        self.assertEqual(self.execution.result_json[PRECHECK_KEY], {**self.state, "input_revision": 5})
        with patch.object(service, "current_state", return_value=self.execution.result_json[PRECHECK_KEY]):
            service.assert_processing_allowed(self.task, SimpleNamespace(id=8))
            with self.assertRaises(BusinessError) as caught:
                service.assert_processing_allowed(self.task, SimpleNamespace(id=9))
            self.assertEqual(caught.exception.code, "MATERIAL_QUALITY_BLOCKED")

    def test_stale_material_revision_and_unfinished_checks_are_not_rebound(self):
        for revision, status in ((3, "READY"), (4, "RUNNING")):
            with self.subTest(revision=revision, status=status):
                self.task.input_revision = 5
                state = {**self.state, "input_revision": revision, "status": status}
                self.execution.result_json = {PRECHECK_KEY: deepcopy(state)}
                AuditMaterialPrecheckService(None).retain_after_field_review(self.task, self.execution, previous_revision=4)
                self.assertEqual(self.execution.result_json[PRECHECK_KEY], state)

    def test_save_then_confirm_carries_precheck_on_each_fact_revision(self):
        service = AuditExecutionCommandsService(MagicMock())
        def apply(task, corrections, **kwargs):
            task.input_revision += 1
            return [{"field_key": "f1"}]

        with (patch("app.modules.audit.execution.service.commands.AuditTaskLookupService") as lookup,
              patch.object(service, "_assert_field_review_editable", return_value=self.execution),
              patch("app.modules.audit.execution.service.commands.AuditExecutionCRUD") as executions,
              patch("app.modules.audit.execution.service.commands.field_review_service.AuditFieldReviewService") as review,
              patch("app.modules.audit.execution.service.commands.reviewed_extraction_keys"),
              patch("app.modules.audit.execution.service.commands.commit_transaction"),
              patch("app.modules.audit.execution.service.resume.AuditExecutionResumeService") as resume):
            lookup.return_value.task_or_404.return_value = self.task
            executions.return_value.by_id.return_value = self.execution
            review.return_value.apply_corrections.side_effect = apply
            review.return_value.missing_fields.return_value = []
            review.return_value.record_confirmation.return_value = SimpleNamespace(confirmation_no="c1")
            resume.return_value.resume_interrupted_execution.return_value = {"status": "SUCCEEDED"}
            service.save_field_review("task", SimpleNamespace(input_revision=4, corrections=[object()]))
            self.assertEqual(self.execution.result_json[PRECHECK_KEY]["input_revision"], 5)
            result = service.confirm_field_review("task", execution_id=2, corrections=[object()], note=None, operation_no="op")
            self.assertEqual(self.task.input_revision, 6)
            self.assertEqual(self.execution.result_json[PRECHECK_KEY]["input_revision"], 6)
            self.assertEqual(result["status"], "SUCCEEDED")


class AssistantReviewRegressionTests(unittest.TestCase):
    def test_run_corrects_reviewer_without_replaying_editor_deletion(self):
        previous = document()
        service = GenerationAssistantExecution(MagicMock())
        job = SimpleNamespace(id=1, job_no="job", revision=1, business_background="", generation_requirements="")
        command = SimpleNamespace(operation_id=1, operation_no="op", request={"revision": 1, "message": "删除字典"}, phase=MagicMock())
        response = {"kind": "EDIT", "message": "已删除", "edits": [{"op": "remove_row", "row_id": "d1"}]}
        with (patch.object(service, "_current", return_value=(job, SimpleNamespace(phase=None))),
              patch("app.modules.imports.generation.assistant_execution.GenerationMessageCRUD") as messages,
              patch("app.modules.imports.generation.assistant_execution.GenerationHistoryService") as history,
              patch("app.modules.imports.generation.assistant_execution.GenerationEditingService"),
              patch("app.modules.imports.generation.assistant_execution.GenerationLlmService") as source,
              patch("app.modules.imports.generation.assistant_execution.commit_transaction"),
              patch("app.modules.imports.generation.assistant_execution.rollback_transaction"),
              patch("app.modules.imports.generation.assistant_execution.call_json", side_effect=[response, {}, {"problems": []}]) as llm):
            messages.return_value.for_operation.side_effect = [None, SimpleNamespace(id=2)]
            messages.return_value.page.return_value = []
            history.return_value.document_at.return_value = previous
            source.return_value._workbook_payload.return_value = ([], {("原始规则", 2)})
            result = service.run(command)
            self.assertTrue(result["changed"])
            self.assertEqual(job.revision, 2)
            self.assertNotIn("d1", [row["row_id"] for row in job.document_json["sheets"][0]["rows"]])
            self.assertEqual(sum(":edit:" in call.kwargs["idempotency_key"] for call in llm.call_args_list), 1)

    def test_review_correction_keeps_already_applied_text_repair(self):
        previous = document()
        candidate = apply_edits(previous, edit({"op": "set_document", "title": "新名称"}))
        responses = [{"problems": ["说明未同步"], "text_repairs": [{"target": "document_summary", "target_id": "document", "text": "新说明"}]},
                     {}, {"problems": []}]
        with patch("app.modules.imports.generation.assistant_execution.call_json", side_effect=responses):
            saved, _ = GenerationAssistantExecution(None)._review(
                SimpleNamespace(operation_no="op", request={"message": "改名称和说明"}), previous, candidate,
                workbook=[], source_rows={("原始规则", 2)}, issue_answer=None, conversation=[], attempt=0,
            )
            self.assertEqual(saved.title, "新名称")
            self.assertEqual(saved.summary, "新说明")

    def test_invalid_reviewer_retries_review_of_same_deleted_candidate(self):
        previous = document()
        candidate = apply_edits(previous, edit({"op": "remove_row", "row_id": "d1"}))
        invalid = {"problems": [], "text_repairs": [{"target": "sheet_description", "target_id": "s1", "text": "说明"}]}
        with patch("app.modules.imports.generation.assistant_execution.call_json", side_effect=[invalid, {"problems": []}]) as llm:
            saved, review = GenerationAssistantExecution(None)._review(
                SimpleNamespace(operation_no="op", request={"message": "删除字典"}), previous, candidate,
                workbook=[], source_rows={("原始规则", 2)}, issue_answer=None, conversation=[], attempt=0,
            )
            self.assertEqual(saved, candidate)
            self.assertFalse(review.problems)
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(llm.call_args_list[0].args[0]["changes"], llm.call_args_list[1].args[0]["changes"])
            self.assertIn("correction", llm.call_args_list[1].args[0])

    def test_repeated_bad_review_has_review_error_instead_of_edit_error(self):
        with patch("app.modules.imports.generation.assistant_execution.call_json", return_value={"wrong": True}):
            with self.assertRaises(BusinessError) as caught:
                GenerationAssistantExecution(None)._review(
                    SimpleNamespace(operation_no="op", request={"message": "修改"}), document(), document(),
                    workbook=[], source_rows=set(), issue_answer=None, conversation=[], attempt=0,
                )
            self.assertEqual(caught.exception.code, "ASSISTANT_REVIEW_INVALID")

    def test_initial_review_format_error_does_not_regenerate_document(self):
        with patch("app.modules.imports.generation.service.llm.call_json", side_effect=[{}, {"problems": []}]) as llm:
            result = GenerationLlmService()._review_document({}, document(), operation_no="op", attempt=0, reasoning_effort="low")
            self.assertEqual(result, [])
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(llm.call_args_list[0].args[0]["document"], llm.call_args_list[1].args[0]["document"])
        with patch("app.modules.imports.generation.service.llm.call_json", return_value={}):
            with self.assertRaises(BusinessError) as caught:
                GenerationLlmService()._review_document({}, document(), operation_no="op", attempt=0, reasoning_effort="low")
            self.assertEqual(caught.exception.code, "LLM_REVIEW_INVALID")

    def test_reordered_rows_allow_related_issue_text_repair(self):
        previous = document()
        previous.issues = [GenerationIssue(issue_id="q1", title="位置说明", detail="第二行", affected_row_ids=["r1"],
                                           source=GenerationSource(kind="USER_INPUT"))]
        candidate = apply_edits(previous, edit({"op": "reorder", "sheet_id": "s1", "ids": ["r1", "m1", "d1"]}))
        repaired = apply_review_text_repairs(previous, candidate, [ReviewTextRepair(target="issue_detail", target_id="q1", text="第一行")])
        self.assertEqual(repaired.issues[0].detail, "第一行")
        self.assertEqual([row.row_id for row in repaired.sheets[0].rows], ["r1", "m1", "d1"])

    def test_new_row_later_metadata_note_is_retained(self):
        candidate = apply_edits(document(), edit(
            {"op": "upsert_row", "sheet_id": "s1", "row": {"row_id": "new", "business_code": "N-1",
             "cells": {"name": "新增", "body": "正文"}, "source": {"kind": "USER_INPUT", "note": "原说明"}}},
            {"op": "update_row_metadata", "row_id": "new", "source_note": "修订后的说明"},
        ))
        self.assertEqual(candidate.sheets[0].rows[-1].source.note, "修订后的说明")

    def test_legacy_number_alignment_does_not_mark_untouched_rows(self):
        previous = document()
        previous.sheets[0].columns.append(GenerationColumn(column_id="code", name="原编号"))
        for index, row in enumerate(previous.sheets[0].rows):
            row.cells["code"] = f"OLD-{index}"
            row.business_code = ""
        edited = previous.model_copy(deep=True)
        edited.sheets[0].rows[1].cells["body"] = "实际修改"
        manual = mark_user_edits(previous, normalize_business_codes(edited, previous))
        assistant = apply_edits(previous, edit({"op": "update_cells", "row_id": "r1", "cells": {"body": "实际修改"}}))
        for result in (manual, assistant):
            self.assertEqual(result.sheets[0].rows[0].source, previous.sheets[0].rows[0].source)
            self.assertEqual(result.sheets[0].rows[2].source, previous.sheets[0].rows[2].source)
            self.assertTrue(result.sheets[0].rows[1].source.note)
        self.assertNotIn("整理", GenerationAssistantExecution(None)._summary(previous, assistant))
        self.assertFalse(any("business_code" in change["path"] for change in document_changes(previous, assistant)))


class CoverageAndWorkbookRegressionTests(unittest.TestCase):
    def test_data_named_id_cannot_rebind_number_column(self):
        ir = SimpleNamespace(sheets=[SimpleNamespace(name="字段", rows=[
            SimpleNamespace(row_no=1, cells={"A1": "编号", "B1": "名称"}),
            SimpleNamespace(row_no=2, cells={"A2": "F-1", "B2": "编号"}),
            SimpleNamespace(row_no=3, cells={"A3": "F-2", "B3": "ID"}),
            SimpleNamespace(row_no=4, cells={"A4": "编号", "B4": "名称"}),
            SimpleNamespace(row_no=5, cells={"A5": "F-3", "B5": "正文"}),
            SimpleNamespace(row_no=7, cells={"A7": "F-4", "B7": "编号"}),
            SimpleNamespace(row_no=9, cells={"A9": "名称", "B9": "字典ID"}),
            SimpleNamespace(row_no=10, cells={"A10": "名称字段", "B10": "D-1"}),
        ])])
        self.assertEqual(GenerationLlmService()._source_records(ir), [
            {"sheet_name": "字段", "row_no": row, "codes": [code]}
            for row, code in ((2, "F-1"), (3, "F-2"), (5, "F-3"), (7, "F-4"), (10, "D-1"))
        ])

    def test_partial_body_merge_does_not_hide_group_heading(self):
        source = Workbook()
        self.addCleanup(source.close)
        ws = source.active
        ws.append(["名称", "说明", "备注"])
        ws.append(["材料分组"])
        ws.merge_cells("A2:C2")
        ws.append(["条目", "说明"])
        ws.merge_cells("B3:C3")
        sources = [GenerationSource(kind="ORIGINAL_EXCEL", origin={"sheet_name": ws.title, "row_no": 3})]
        groups = GenerationWorkbookLayout()._groups(ws, 1, sources)
        self.assertEqual(str(groups[0][0]), "A2:C2")

    def test_auxiliary_same_name_uses_styles_without_original_preamble(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.xlsx"
            source = Workbook()
            ws = source.active
            ws.title = "待明确事项"
            ws.append(["不应复制的前言"])
            ws.merge_cells("A1:B1")
            ws.append(["事项", "问题与影响"])
            ws.append(["原事项", "短说明"])
            ws.row_dimensions[3].height = 15
            source.save(path)
            source.close()
            layout = GenerationWorkbookLayout(path)
            output = Workbook()
            try:
                result = layout.add_sheet(output, "待明确事项", ["事项", "问题与影响"], [["新事项", "长说明" * 200]],
                                          write_value=lambda cell, value: setattr(cell, "value", value), style_only=True)
                self.assertEqual(result["A1"].value, "事项")
                self.assertFalse(result.merged_cells.ranges)
                self.assertTrue(result["B2"].alignment.wrap_text)
                self.assertGreater(result.row_dimensions[2].height, 15)
            finally:
                output.close()
                layout.close()


class InputAndQualityRegressionTests(unittest.TestCase):
    def test_size_label_uses_unit_only_once_on_pass_and_fail(self):
        for standard, unit in (("50MB", None), ("50MB", "MB"), ("50", "MB")):
            for size, status in ((1, "PASSED"), (50_000_001, "ABNORMAL")):
                with self.subTest(standard=standard, unit=unit, size=size):
                    result = check_file_size(SimpleNamespace(size_bytes=size), standard_value=standard, unit=unit)
                    self.assertEqual(result["status"], status)
                    self.assertTrue(result["message"].endswith("（50MB）"))
                    self.assertEqual(result["limit_bytes"], "50000000")

    def test_long_background_rejected_before_saving_upload(self):
        with patch("app.modules.imports.generation.service.upload.save_upload_sync") as upload:
            with self.assertRaises(BusinessError) as caught:
                GenerationUploadService(None).create_job(None, business_background="字" * 16001,
                                                        generation_requirements="", idempotency_key="k")
            self.assertEqual(caught.exception.code, "GENERATION_INPUT_TOO_LONG")
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
