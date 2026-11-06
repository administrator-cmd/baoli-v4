"""报告模板契约测试；不连接模型、OCR 服务或数据库。"""

import tempfile
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.common.exceptions import BusinessError
from app.modules.audit.report_template.schema import ReportStyleProfile
from app.modules.audit.report_template.service.analysis import (
    _analysis_pages,
    _ocr_pdf_online,
    analyze_report_style,
    extract_pdf_pages,
    validate_style_profile,
)
from app.modules.audit.report_template.service.lifecycle import ReportTemplateLifecycleService
from app.modules.audit.report_template.service.operations import (
    ReportTemplateAnalysisOperations,
    run_report_template_analysis,
)
from app.modules.audit.rule.service.agent import AuditRuleAgentService
from app.modules.plan.plan.dto import TaskFromPlanRequest
from app.modules.system.operation.service.background import CommandHandle


class ReportTemplateStyleTests(unittest.TestCase):
    def test_style_profile_round_trips_as_versioned_json(self):
        profile = {
            "version": "1.0",
            "document_overview": "经营情况分析报告",
            "structure": {"opening": "先概述，再展开"},
            "sections": [{"name": "经营情况", "purpose": "说明主要经营数据"}],
            "expression": {"tone": "客观、简洁"},
            "formatting_conventions": ["使用分级标题"],
        }
        self.assertEqual(validate_style_profile(profile), profile)

    def test_style_profile_rejects_invalid_section_shape(self):
        with self.assertRaises(ValidationError):
            ReportStyleProfile.model_validate({"sections": [{"purpose": "缺少章节名"}]})

    def test_analysis_samples_opening_middle_and_ending_pages(self):
        pages = [{"page_no": index, "text": f"page-{index}"} for index in range(1, 31)]
        selected = _analysis_pages(pages)
        selected_numbers = [item["page_no"] for item in selected]
        self.assertEqual(len(selected), 16)
        self.assertEqual(selected_numbers[:6], [1, 2, 3, 4, 5, 6])
        self.assertTrue(any(14 <= number <= 17 for number in selected_numbers))
        self.assertEqual(selected_numbers[-6:], [25, 26, 27, 28, 29, 30])

    def test_style_analysis_prompt_includes_exact_json_schema(self):
        with patch(
            "app.modules.audit.report_template.service.analysis.call_json",
            return_value={
                "version": "1.0",
                "document_overview": "合同审核报告",
                "structure": {"opening": "先摘要"},
                "sections": [{"name": "审核结论", "purpose": "概述结论"}],
                "expression": {"tone": "客观"},
                "formatting_conventions": ["分级标题"],
            },
        ) as call:
            analyze_report_style([{"page_no": 1, "text": "样例正文"}], "RPTPL_test")
        prompt = call.call_args.args[1]
        self.assertIn('"properties"', prompt)
        self.assertIn('"formatting_conventions"', prompt)
        self.assertIn('"name"', prompt)

    def test_invalid_style_logs_field_paths_without_model_values(self):
        with patch(
            "app.modules.audit.report_template.service.analysis.call_json",
            return_value={"version": "2.0", "sections": [{"purpose": "sensitive-sample-sentinel"}]},
        ):
            with self.assertLogs("app.modules.audit.report_template.service.analysis", level="WARNING") as captured:
                with self.assertRaises(BusinessError) as raised:
                    analyze_report_style([{"page_no": 1, "text": "样例正文"}], "RPTPL_test")
        self.assertEqual(raised.exception.code, "REPORT_TEMPLATE_PROFILE_INVALID")
        output = "\n".join(captured.output)
        self.assertIn("version", output)
        self.assertIn("sections.0.name", output)
        self.assertNotIn("sensitive-sample-sentinel", output)

    def test_every_pdf_uses_online_ocr_without_local_pdf_parser(self):
        with patch(
            "app.modules.audit.report_template.service.analysis._ocr_pdf_online",
            return_value=[{"page_no": 1, "text": "在线 PaddleOCR 识别正文"}],
        ) as ocr:
            pages = extract_pdf_pages(source_path=Path("source.pdf"), filename="报告.pdf")
        self.assertEqual(pages, [{"page_no": 1, "text": "在线 PaddleOCR 识别正文"}])
        ocr.assert_called_once_with(Path("source.pdf"), "报告.pdf")

    def test_empty_online_ocr_result_is_rejected(self):
        with patch(
            "app.modules.audit.report_template.service.analysis._ocr_pdf_online",
            return_value=[{"page_no": 1, "text": ""}],
        ):
            with self.assertRaises(BusinessError) as raised:
                extract_pdf_pages(source_path=Path("source.pdf"), filename="空白.pdf")
        self.assertEqual(raised.exception.code, "REPORT_TEMPLATE_TEXT_EMPTY")

    def test_invalid_online_ocr_page_shape_is_rejected(self):
        with patch(
            "app.modules.audit.report_template.service.analysis._ocr_pdf_online",
            return_value=[{"page_no": True, "text": "文本"}],
        ):
            with self.assertRaises(BusinessError) as raised:
                extract_pdf_pages(source_path=Path("source.pdf"), filename="报告.pdf")
        self.assertEqual(raised.exception.code, "REPORT_TEMPLATE_OCR_RESULT_INVALID")

    def test_online_ocr_adapter_submits_and_polls_using_shared_client(self):
        deadline = datetime(2026, 11, 5, tzinfo=UTC)
        submission = SimpleNamespace(job_id="job-1", deadline_at=deadline)
        result = SimpleNamespace(artifact=SimpleNamespace(content={"pages": [{"page_no": 1, "text": "识别结果"}]}))
        with (
            patch("app.integrations.documents.ocr.submit_processed_document", return_value=submission) as submit,
            patch("app.integrations.documents.ocr.poll_ocr_job", return_value=result) as poll,
        ):
            pages = _ocr_pdf_online(Path("source.pdf"), "扫描报告.pdf")
        self.assertEqual(pages, [{"page_no": 1, "text": "识别结果"}])
        submit.assert_called_once_with(file_path=Path("source.pdf"), filename="扫描报告.pdf")
        poll.assert_called_once_with(job_id="job-1", deadline_at=deadline)

    def test_report_style_is_added_as_non_factual_guidance(self):
        style = {"version": "1.0", "sections": [{"name": "经营情况", "purpose": "概述"}]}
        messages = AuditRuleAgentService().initial_messages(
            {
                "kind": "REPORT",
                "execution_mode": "REPORT",
                "rule_code": "R-REPORT",
                "name": "审核报告",
                "requirement": "根据当前审核结果形成报告",
                "report_style_profile": style,
                "report_style_template_name": "季度报告",
            },
            [],
            manifest={"fields": []},
            material_names={},
        )
        self.assertIn("不得把风格样本当作事实", messages[0]["content"])
        self.assertIn('"name": "经营情况"', messages[1]["content"])

    def test_task_creation_defaults_to_rules_and_requires_a_template_when_selected(self):
        default = TaskFromPlanRequest(task_name="任务", expected_scope_hash="hash", idempotency_key="key")
        self.assertEqual(default.report_generation_mode, "RULES")
        with self.assertRaises(ValidationError):
            TaskFromPlanRequest(
                task_name="任务", expected_scope_hash="hash", idempotency_key="key",
                report_generation_mode="TEMPLATE",
            )


class ReportTemplateAnalysisOperationTests(unittest.TestCase):
    def command(self):
        return CommandHandle(
            operation_id=1,
            operation_no="OP_test",
            request={"template_no": "RPTPL_test", "source_sha256": "sha256"},
            resume_committed=False,
        )

    def test_failed_reanalysis_preserves_existing_profile_and_restores_ready_state(self):
        existing_profile = {"version": "1.0", "document_overview": "审核报告", "sections": [{"name": "手工档案", "purpose": "保留"}]}
        template = SimpleNamespace(
            template_no="RPTPL_test", source_sha256="sha256", source_path="report_templates/test/source.pdf",
            source_filename="source.pdf", style_profile_json=existing_profile, status="READY", analysis_error=None,
        )
        with (
            patch("app.modules.audit.report_template.service.operations.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.operations.rollback_transaction"),
            patch("app.modules.audit.report_template.service.operations.commit_transaction") as commit,
            patch("app.modules.audit.report_template.service.operations._operation_is_running"),
            patch(
                "app.modules.audit.report_template.service.operations.extract_pdf_pages",
                side_effect=BusinessError("OCR_JOB_FAILED", "OCR 暂不可用", status_code=502),
            ),
        ):
            crud.return_value.lock_by_no.return_value = template
            with self.assertRaises(BusinessError):
                run_report_template_analysis(Mock(), self.command())

        self.assertIs(template.style_profile_json, existing_profile)
        self.assertEqual(template.status, "READY")
        self.assertEqual(template.analysis_error, "OCR 暂不可用")
        commit.assert_called_once()


class ReportTemplateUploadOperationTests(unittest.TestCase):
    def command(self):
        return CommandHandle(
            operation_id=1,
            operation_no="OP_test",
            request={"template_no": "RPTPL_test", "source_sha256": "sha256"},
            resume_committed=False,
        )

    def test_upload_returns_accepted_operation_without_running_ocr_inline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stored = []
            operation = SimpleNamespace(operation_no="OP_upload", status="PENDING")
            db = Mock()
            db.begin_nested.return_value = nullcontext()
            crud = Mock()
            crud.by_upload_idempotency_key.return_value = None
            crud.add.side_effect = lambda template: stored.append(template)
            crud.lock_by_no.side_effect = lambda _: stored[0]

            def save_upload(_upload, *, relative_path, **_kwargs):
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pdf")
                return 3, "sha256"

            with (
                patch("app.modules.audit.report_template.service.lifecycle.save_upload_sync", side_effect=save_upload),
                patch("app.modules.audit.report_template.service.lifecycle.resolve_storage_path", side_effect=lambda value: root / value),
                patch("app.modules.audit.report_template.service.lifecycle.get_settings", return_value=SimpleNamespace(max_document_bytes=100)),
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD", return_value=crud),
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateAnalysisOperations") as analysis_operations,
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateLifecycleService._discard_unreferenced_source"),
                patch("app.modules.audit.report_template.service.analysis.extract_pdf_pages") as ocr,
            ):
                analysis_operations.return_value.submit.return_value = (operation, False)
                template, accepted, reused = ReportTemplateLifecycleService(db).upload(
                    SimpleNamespace(filename="report.pdf"), name="合同审核", idempotency_key="upload-key",
                )

        self.assertEqual(template.status, "PROCESSING")
        self.assertEqual(template.upload_idempotency_key, "upload-key")
        self.assertIs(accepted, operation)
        self.assertFalse(reused)
        analysis_operations.return_value.submit.assert_called_once()
        ocr.assert_not_called()

    def test_repeated_upload_key_reuses_original_template_and_operation(self):
        existing = SimpleNamespace(
            template_no="RPTPL_existing", name="已经改名的合同审核", source_filename="report.pdf",
            source_size=3, source_sha256="sha256",
        )
        operation = SimpleNamespace(operation_no="OP_existing", status="RUNNING")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def save_upload(_upload, *, relative_path, **_kwargs):
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pdf")
                return 3, "sha256"

            crud = Mock()
            crud.by_upload_idempotency_key.return_value = existing
            with (
                patch("app.modules.audit.report_template.service.lifecycle.save_upload_sync", side_effect=save_upload),
                patch("app.modules.audit.report_template.service.lifecycle.resolve_storage_path", side_effect=lambda value: root / value),
                patch("app.modules.audit.report_template.service.lifecycle.get_settings", return_value=SimpleNamespace(max_document_bytes=100)),
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD", return_value=crud),
                patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService") as background,
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateAnalysisOperations") as analysis_operations,
            ):
                background.return_value.existing_operation.return_value = operation
                result = ReportTemplateLifecycleService(Mock()).upload(
                    SimpleNamespace(filename="report.pdf"), name="合同审核", idempotency_key="upload-key",
                )

            remaining_files = [path for path in root.rglob("*") if path.is_file()]

        self.assertEqual(result, (existing, operation, True))
        self.assertEqual(existing.name, "已经改名的合同审核")
        self.assertEqual(remaining_files, [])
        analysis_operations.assert_not_called()
        background.return_value.existing_operation.assert_called_once_with(
            object_type="REPORT_TEMPLATE",
            object_no="RPTPL_existing",
            operation_type="REPORT_TEMPLATE_ANALYZE",
            idempotency_key="upload-analysis:RPTPL_existing",
        )

    def test_failed_first_analysis_marks_failed_without_creating_a_profile(self):
        template = SimpleNamespace(
            template_no="RPTPL_test", source_sha256="sha256", source_path="report_templates/test/source.pdf",
            source_filename="source.pdf", style_profile_json=None, status="PROCESSING", analysis_error=None,
        )
        with (
            patch("app.modules.audit.report_template.service.operations.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.operations.rollback_transaction"),
            patch("app.modules.audit.report_template.service.operations.commit_transaction"),
            patch("app.modules.audit.report_template.service.operations._operation_is_running"),
            patch(
                "app.modules.audit.report_template.service.operations.extract_pdf_pages",
                side_effect=BusinessError("OCR_JOB_FAILED", "OCR 暂不可用", status_code=502),
            ),
        ):
            crud.return_value.lock_by_no.return_value = template
            with self.assertRaises(BusinessError):
                run_report_template_analysis(Mock(), self.command())

        self.assertIsNone(template.style_profile_json)
        self.assertEqual(template.status, "FAILED")

    def test_failed_model_analysis_keeps_manual_profile_and_records_ocr_page_count(self):
        profile = {"version": "1.0", "document_overview": "审核报告", "sections": [{"name": "手工编辑", "purpose": "保留"}]}
        template = SimpleNamespace(
            template_no="RPTPL_test", source_sha256="sha256", source_path="report_templates/test/source.pdf",
            source_filename="source.pdf", style_profile_json=profile, status="READY", analysis_error=None,
            page_count=1,
        )
        with (
            patch("app.modules.audit.report_template.service.operations.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.operations.rollback_transaction"),
            patch("app.modules.audit.report_template.service.operations.commit_transaction"),
            patch("app.modules.audit.report_template.service.operations._operation_is_running"),
            patch(
                "app.modules.audit.report_template.service.operations.extract_pdf_pages",
                return_value=[{"page_no": 1, "text": "页面一"}, {"page_no": 2, "text": "页面二"}],
            ),
            patch(
                "app.modules.audit.report_template.service.operations.analyze_report_style",
                side_effect=BusinessError("REPORT_TEMPLATE_PROFILE_INVALID", "风格结构无效", status_code=502),
            ),
        ):
            crud.return_value.lock_by_no.return_value = template
            with self.assertRaises(BusinessError):
                run_report_template_analysis(Mock(), self.command())

        self.assertIs(template.style_profile_json, profile)
        self.assertEqual(template.page_count, 2)
        self.assertEqual(template.status, "READY")
        self.assertEqual(template.analysis_error, "风格结构无效")

    def test_successful_background_analysis_commits_new_profile_and_page_count(self):
        template = SimpleNamespace(
            template_no="RPTPL_test", source_sha256="sha256", source_path="report_templates/test/source.pdf",
            source_filename="source.pdf", style_profile_json={"version": "1.0"}, status="READY", analysis_error=None,
        )
        profile = {"version": "1.0", "document_overview": "审核报告", "sections": [{"name": "审核结论", "purpose": "结论"}]}
        with (
            patch("app.modules.audit.report_template.service.operations.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.operations.rollback_transaction"),
            patch("app.modules.audit.report_template.service.operations.commit_transaction") as commit,
            patch("app.modules.audit.report_template.service.operations._operation_is_running"),
            patch(
                "app.modules.audit.report_template.service.operations.extract_pdf_pages",
                return_value=[{"page_no": 1, "text": "页面一"}, {"page_no": 2, "text": "页面二"}],
            ),
            patch("app.modules.audit.report_template.service.operations.analyze_report_style", return_value=profile),
        ):
            crud.return_value.lock_by_no.return_value = template
            result = run_report_template_analysis(Mock(), self.command())

        self.assertEqual(result, {"template_no": "RPTPL_test", "page_count": 2})
        self.assertEqual(template.page_count, 2)
        self.assertEqual(template.style_profile_json, profile)
        self.assertEqual(template.status, "READY")
        self.assertIsNone(template.analysis_error)
        commit.assert_called_once()

    def test_reanalysis_submission_keeps_existing_profile_ready_for_use(self):
        profile = {"version": "1.0", "document_overview": "审核报告", "sections": [{"name": "手工编辑", "purpose": "保留"}]}
        template = SimpleNamespace(
            template_no="RPTPL_test", source_sha256="sha256", source_path="source.pdf",
            status="READY", analysis_error="之前的错误", style_profile_json=profile,
        )
        with (
            patch("app.modules.audit.report_template.service.operations.resolve_storage_path") as resolve,
            patch("app.modules.audit.report_template.service.operations.OperationBackgroundService") as background,
        ):
            resolve.return_value.is_file.return_value = True
            background.return_value.submit_operation.return_value = (SimpleNamespace(status="PENDING"), False)
            operation, reused = ReportTemplateAnalysisOperations(Mock()).submit(
                template, idempotency_key="retry-key",
            )
            validate = background.return_value.submit_operation.call_args.kwargs["validate"]
            validate()

        self.assertEqual(operation.status, "PENDING")
        self.assertFalse(reused)
        self.assertEqual(template.status, "READY")
        self.assertIs(template.style_profile_json, profile)
        self.assertIsNone(template.analysis_error)


if __name__ == "__main__":
    unittest.main()
