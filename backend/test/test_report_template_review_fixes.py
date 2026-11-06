"""报告模板审查回归：实际契约/路由/服务逻辑，数据库、OCR 和模型全部使用替身。"""

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.common.exceptions import BusinessError, business_error_handler
from app.core.database import get_db
from app.modules.audit.report_template.controller import router
from app.modules.audit.report_template.schema import MAX_STYLE_PROFILE_BYTES, ReportStyleProfile
from app.modules.audit.report_template.service.analysis import validate_style_profile
from app.modules.audit.report_template.service.lifecycle import ReportTemplateLifecycleService
from app.modules.audit.report_template.service.operations import (
    ReportTemplateAnalysisOperations,
    run_report_template_analysis,
)
from app.modules.audit.rule.service.rule_execution import AuditRuleExecutionService
from app.modules.audit.task.service.creation import AuditTaskCreationService
from app.modules.system.operation.service.background import CommandHandle


def profile(**changes):
    return {
        "version": "1.0", "document_overview": "合同审核报告", "sections": [{"name": "审核结论", "purpose": "概述结论"}],
        "structure": {}, "expression": {}, "formatting_conventions": [], **changes,
    }


def template(value, **changes):
    return SimpleNamespace(
        **{
            "template_no": "RPTPL_test", "name": "原名称", "source_filename": "报告.pdf", "source_size": 3,
            "source_sha256": "sha256", "source_path": "source.pdf", "page_count": 1,
            "style_profile_json": value, "status": "READY", "analysis_error": None,
            "created_at": datetime(2026, 11, 6, tzinfo=UTC), "updated_at": datetime(2026, 11, 6, tzinfo=UTC),
            **changes,
        },
    )


class ReportProfileValidationTests(unittest.TestCase):
    def test_empty_missing_and_whitespace_content_is_rejected(self):
        for value in [
            {}, profile(document_overview=""), profile(document_overview=" \n\t"), profile(sections=[]),
            profile(sections=[{"name": " \t"}]), profile(sections=[{"purpose": "没有名称"}]),
        ]:
            with self.subTest(value=value), self.assertRaises(BusinessError) as raised:
                validate_style_profile(value)
            self.assertEqual(raised.exception.code, "REPORT_TEMPLATE_PROFILE_INVALID")

    def test_total_json_budget_counts_nested_data_and_utf8_at_exact_boundary(self):
        value = profile(structure={"nested": {"text": ""}})
        empty_size = len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        value["structure"]["nested"]["text"] = "x" * (MAX_STYLE_PROFILE_BYTES - empty_size)
        self.assertEqual(validate_style_profile(value), value)
        value["structure"]["nested"]["text"] += "x"
        with self.assertRaises(BusinessError):
            validate_style_profile(value)
        value = profile(expression={"nested": ["审" * 3000]})
        self.assertLess(len(json.dumps(value, ensure_ascii=False)), MAX_STYLE_PROFILE_BYTES)
        with self.assertRaises(BusinessError):
            validate_style_profile(value)

    def test_formatting_entry_and_combined_size_are_limited(self):
        for entries in [["x" * 501], ["x" * 500] * 20]:
            with self.subTest(count=len(entries)), self.assertRaises(BusinessError):
                validate_style_profile(profile(formatting_conventions=entries))

    def test_non_json_and_nonfinite_dictionary_values_are_rejected(self):
        for value in [float("nan"), float("inf"), object()]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ReportStyleProfile.model_validate(profile(structure={"value": value}))

    def test_empty_or_oversized_model_response_fails_initial_analysis(self):
        for value in [{}, profile(sections=[]), profile(expression={"text": "x" * 9000})]:
            with (
                self.subTest(value_type=type(value)),
                patch("app.modules.audit.report_template.service.analysis.call_json", return_value=value),
                patch("app.modules.audit.report_template.service.operations.ReportTemplateCRUD") as crud,
                patch("app.modules.audit.report_template.service.operations.rollback_transaction"),
                patch("app.modules.audit.report_template.service.operations.commit_transaction"),
                patch("app.modules.audit.report_template.service.operations._operation_is_running"),
                patch("app.modules.audit.report_template.service.operations.extract_pdf_pages", return_value=[{"page_no": 1, "text": "正文"}]),
            ):
                item = template(None, status="PROCESSING")
                crud.return_value.lock_by_no.return_value = item
                command = CommandHandle(operation_id=1, operation_no="OP_test", resume_committed=False,
                                        request={"template_no": item.template_no, "source_sha256": "sha256"})
                with self.assertRaises(BusinessError) as raised:
                    run_report_template_analysis(Mock(), command)
                self.assertEqual(raised.exception.status_code, 502)
                self.assertEqual(item.status, "FAILED")
                self.assertIsNone(item.style_profile_json)

    def test_failed_analysis_does_not_promote_invalid_stored_profile_to_ready(self):
        item = template({})
        with (
            patch("app.modules.audit.report_template.service.operations.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.operations.rollback_transaction"),
            patch("app.modules.audit.report_template.service.operations.commit_transaction"),
            patch("app.modules.audit.report_template.service.operations._operation_is_running"),
            patch("app.modules.audit.report_template.service.operations.extract_pdf_pages", side_effect=BusinessError("OCR_FAILED", "识别失败")),
        ):
            crud.return_value.lock_by_no.return_value = item
            command = CommandHandle(operation_id=1, operation_no="OP_test", resume_committed=False,
                                    request={"template_no": item.template_no, "source_sha256": "sha256"})
            with self.assertRaises(BusinessError):
                run_report_template_analysis(Mock(), command)
        self.assertEqual(item.status, "FAILED")
        self.assertEqual(item.style_profile_json, {})

    def test_reanalysis_does_not_offer_an_invalid_old_profile(self):
        item = template({})
        with (
            patch("app.modules.audit.report_template.service.operations.resolve_storage_path") as path,
            patch("app.modules.audit.report_template.service.operations.OperationBackgroundService") as background,
        ):
            path.return_value.is_file.return_value = True
            background.return_value.submit_operation.return_value = (Mock(), False)
            ReportTemplateAnalysisOperations(Mock()).submit(item, idempotency_key="key")
            background.return_value.submit_operation.call_args.kwargs["validate"]()
        self.assertEqual(item.status, "PROCESSING")
        self.assertEqual(item.style_profile_json, {})


class ReportTemplateApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: Mock()
        app.add_exception_handler(BusinessError, business_error_handler)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_one_invalid_row_cannot_break_list_or_detail_or_be_selected(self):
        invalid_values = [{}, {"version": "2.0"}, [], None, profile(expression={"text": "x" * 9000})]
        for invalid in invalid_values:
            with (
                self.subTest(invalid_type=type(invalid)),
                patch("app.modules.audit.report_template.controller.ReportTemplateCRUD") as crud,
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as lifecycle_crud,
                patch("app.modules.audit.report_template.controller.OperationBackgroundService") as background,
            ):
                bad = template(invalid, template_no="bad", analysis_error="之前分析失败")
                good = template(profile(), template_no="good")
                snapshot = deepcopy(vars(bad))
                crud.return_value.list_all.return_value = [bad, good]
                crud.return_value.latest_operations.return_value = []
                lifecycle_crud.return_value.by_no.return_value = bad
                background.return_value.latest_operation.return_value = None
                response = self.client.get("/report-templates")
                self.assertEqual(response.status_code, 200)
                items = response.json()["data"]["items"]
                self.assertEqual(len(items), 2)
                self.assertIsNone(items[0]["style_profile"])
                self.assertEqual(items[0]["status"], "FAILED")
                self.assertIn("之前分析失败", items[0]["analysis_error"])
                self.assertIn("风格档案", items[0]["analysis_error"])
                self.assertEqual(items[1]["status"], "READY")
                response = self.client.get("/report-templates/bad")
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["data"]["style_profile"])
                options = self.client.get("/report-templates/options")
                self.assertEqual(options.status_code, 200)
                self.assertEqual(options.json()["data"]["items"], [{"template_no": "good", "name": "原名称"}])
                self.assertEqual(vars(bad), snapshot)

    def test_ready_profile_remains_selectable_during_reanalysis(self):
        with patch("app.modules.audit.report_template.controller.ReportTemplateCRUD") as crud:
            crud.return_value.list_all.return_value = [template(profile(), analysis_error="上次更新失败")]
            self.assertEqual(len(self.client.get("/report-templates/options").json()["data"]["items"]), 1)

    def test_pdf_preview_serves_original_as_inline_pdf(self):
        item = template(profile())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"%PDF-1.4 sample")
            with (
                patch(
                    "app.modules.audit.report_template.controller.ReportTemplateLifecycleService.template_or_404",
                    return_value=item,
                ),
                patch("app.modules.audit.report_template.controller.resolve_storage_path", return_value=source),
            ):
                response = self.client.get(f"/report-templates/{item.template_no}/pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(response.content, b"%PDF-1.4 sample")

    def test_pdf_preview_reports_missing_original_as_not_found(self):
        item = template(profile())
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.pdf"
            with (
                patch(
                    "app.modules.audit.report_template.controller.ReportTemplateLifecycleService.template_or_404",
                    return_value=item,
                ),
                patch("app.modules.audit.report_template.controller.resolve_storage_path", return_value=missing),
            ):
                response = self.client.get(f"/report-templates/{item.template_no}/pdf")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "REPORT_TEMPLATE_SOURCE_MISSING")

    def test_name_only_put_accepts_omitted_or_null_profile_and_preserves_failure(self):
        for payload in [{"name": "新名称"}, {"name": "新名称", "style_profile": None}]:
            with (
                self.subTest(payload=payload),
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as crud,
                patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService", autospec=True),
                patch("app.modules.audit.report_template.controller.OperationBackgroundService") as background,
            ):
                item = template(None, status="FAILED", analysis_error="OCR 失败")
                crud.return_value.lock_by_no.return_value = item
                background.return_value.latest_operation.return_value = None
                response = self.client.put("/report-templates/RPTPL_test", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(item.name, "新名称")
                self.assertEqual(item.status, "FAILED")
                self.assertIsNone(item.style_profile_json)
                self.assertEqual(item.analysis_error, "OCR 失败")

    def test_invalid_profile_put_is_rejected_without_mutating_name_or_profile(self):
        for invalid in [{}, profile(expression={"text": "x" * 9000})]:
            with (
                self.subTest(invalid=bool(invalid)),
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as crud,
                patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService", autospec=True),
            ):
                item = template(profile())
                snapshot = deepcopy(vars(item))
                crud.return_value.lock_by_no.return_value = item
                response = self.client.put("/report-templates/RPTPL_test", json={"name": "新名称", "style_profile": invalid})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(vars(item), snapshot)
                crud.return_value.flush.assert_not_called()


class ReportTemplateLifecycleReviewTests(unittest.TestCase):
    def test_upload_replay_after_rename_reuses_original_without_resetting_name(self):
        item = template(profile(), name="已经改过的名称")
        operation = Mock()
        with (
            patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService") as background,
        ):
            crud.return_value.by_upload_idempotency_key.return_value = item
            background.return_value.existing_operation.return_value = operation
            result = ReportTemplateLifecycleService(Mock())._existing_upload(
                key="original-key", filename="报告.pdf", size=3, sha256="sha256",
            )
        self.assertEqual(result, (item, operation, True))
        self.assertEqual(item.name, "已经改过的名称")

    def test_upload_replay_still_rejects_changed_file_identity(self):
        item = template(profile())
        for changes in [{"filename": "另一份.pdf"}, {"size": 4}, {"sha256": "different"}]:
            with (
                self.subTest(changes=changes),
                patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as crud,
                patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService") as background,
            ):
                crud.return_value.by_upload_idempotency_key.return_value = item
                with self.assertRaises(BusinessError) as raised:
                    ReportTemplateLifecycleService(Mock())._existing_upload(
                        **{"key": "key", "filename": "报告.pdf", "size": 3, "sha256": "sha256", **changes},
                    )
                self.assertEqual(raised.exception.status_code, 409)
                background.assert_not_called()

    def test_name_only_save_preserves_existing_profile_and_update_error(self):
        original = profile()
        item = template(original, analysis_error="重新分析失败")
        with (
            patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService", autospec=True),
        ):
            crud.return_value.lock_by_no.return_value = item
            ReportTemplateLifecycleService(Mock()).update(item.template_no, name="新名称")
        self.assertIs(item.style_profile_json, original)
        self.assertEqual(item.analysis_error, "重新分析失败")
        self.assertEqual(item.status, "READY")

    def test_name_only_save_cannot_bypass_active_operation_lock(self):
        item = template(None, status="PROCESSING")
        with (
            patch("app.modules.audit.report_template.service.lifecycle.ReportTemplateCRUD") as crud,
            patch("app.modules.audit.report_template.service.lifecycle.OperationBackgroundService", autospec=True) as background,
        ):
            crud.return_value.lock_by_no.return_value = item
            background.return_value.assert_no_active_operation.side_effect = BusinessError("OPERATION_ACTIVE", "处理中", status_code=409)
            with self.assertRaises(BusinessError):
                ReportTemplateLifecycleService(Mock()).update(item.template_no, name="新名称")
        self.assertEqual(item.name, "原名称")


class ReportTemplateTaskBoundaryTests(unittest.TestCase):
    def create_task(self, value):
        plan = SimpleNamespace(
            id=1, deleted_at=None, enabled=True, scenario_code="S", scenario_version_id=1,
            scope_hash="hash", sync_error=None, plan_code="P", plan_name="方案",
            manifest_json={"rules": [], "materials": []},
        )
        with (
            patch("app.modules.audit.task.service.creation.crud.AuditTaskCRUD") as crud,
            patch("app.modules.audit.task.service.creation.build_task_schema", return_value={}),
            patch("app.modules.audit.task.service.creation.execution_container_of", return_value={"node_code": "RULE_EXECUTION"}),
            patch("app.modules.audit.report_template.crud.ReportTemplateCRUD") as template_crud,
        ):
            crud.return_value.lock_plan.return_value = plan
            crud.return_value.scenario_template.return_value = SimpleNamespace(enabled=True)
            crud.return_value.scenario_version.return_value = SimpleNamespace(id=1)
            template_crud.return_value.by_no.return_value = template(value)
            return AuditTaskCreationService(Mock()).create_task_from_plan(
                plan, task_name="任务", remark=None, report_generation_mode="TEMPLATE", report_template_no="RPTPL_test",
            )

    def test_task_cannot_freeze_empty_or_oversized_stored_profile(self):
        for invalid in [{}, None, profile(structure={"text": "x" * 9000})]:
            with self.subTest(invalid_type=type(invalid)), self.assertRaises(BusinessError) as raised:
                self.create_task(invalid)
            self.assertEqual(raised.exception.code, "REPORT_TEMPLATE_NOT_READY")

    def test_valid_task_freezes_independent_normalized_profile(self):
        original = profile()
        task = self.create_task(original)
        frozen = task.manifest_json["report_generation"]["style_profile"]
        self.assertEqual(frozen, original)
        original["sections"][0]["name"] = "后来修改"
        self.assertEqual(frozen["sections"][0]["name"], "审核结论")

    def test_report_execution_rechecks_frozen_profile_before_prompt_injection(self):
        for value in [None, {}, profile(version="2.0"), profile(expression={"text": "x" * 9000}), profile()]:
            with (
                self.subTest(valid=bool(value) and len(str(value)) < 8192),
                patch("app.modules.audit.rule.service.rule_execution.AuditRuleCRUD") as crud,
                patch("app.modules.audit.rule.service.rule_execution.latest_execution", return_value=None),
                patch("app.modules.audit.field.service.fact_context.AuditFieldFactContextService") as facts,
            ):
                crud.return_value.context_rows.return_value = ([], [])
                facts.return_value.project_task_facts.return_value = []
                task = SimpleNamespace(id=1, task_schema_json={"enabled_rule_keys": []}, manifest_json={
                    "fields": [], "report_generation": {"mode": "TEMPLATE", "style_profile": value},
                })
                service = AuditRuleExecutionService(Mock())
                service.report_section_data = Mock(return_value=[])
                rule = {"kind": "REPORT", "report_definition": {"sections": []}}
                original = deepcopy(task.manifest_json)
                context = service.evaluation_context(task, rule)
                if value == profile():
                    self.assertEqual(context["report_style_profile"], value)
                else:
                    self.assertNotIn("report_style_profile", context)
                    self.assertNotIn("report_style_template_name", context)
                self.assertEqual(context["report_data"], [])
                self.assertEqual(task.manifest_json, original)


if __name__ == "__main__":
    unittest.main()
