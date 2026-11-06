"""旧任务风格降级、报告元数据与续跑回归；不连接数据库或模型。"""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.common.exceptions import BusinessError
from app.modules.audit.execution.service.resume import AuditExecutionResumeService
from app.modules.audit.report.model import AuditReport
from app.modules.audit.report.schema import ReportDetailResponse, ReportResponse
from app.modules.audit.report.service.query import AuditReportQueryService
from app.modules.audit.report.service.views import AuditReportViewsService
from app.modules.audit.report_template.service.generation import resolve_report_generation
from app.modules.audit.rule.service.rule_execution import AuditRuleExecutionService


def style_profile():
    return {"version": "1.0", "document_overview": "审核报告", "sections": [{"name": "结论"}]}


def generation(profile):
    return {"mode": "TEMPLATE", "template_no": "TPL-old", "template_name": "原选模板", "style_profile": profile}


FALLBACK = {
    "mode": "RULES", "requested_mode": "TEMPLATE", "template_no": "TPL-old", "template_name": "原选模板",
    "fallback_code": "REPORT_TEMPLATE_PROFILE_INVALID", "fallback_reason": "模板档案无效，本次按规则生成",
}


class ReportTemplateFallbackTests(unittest.TestCase):
    def test_invalid_frozen_profiles_fall_back_without_mutating_manifest(self):
        for profile in [None, {}, {**style_profile(), "version": "2.0"},
                        {**style_profile(), "expression": {"text": "中" * 4000}}]:
            with self.subTest(profile_type=type(profile)):
                config = generation(profile)
                original = deepcopy(config)
                metadata, style = resolve_report_generation(config)
                self.assertEqual(metadata, FALLBACK)
                self.assertIsNone(style)
                self.assertEqual(config, original)

    def test_valid_template_and_rules_modes_do_not_warn(self):
        config = generation(style_profile())
        metadata, style = resolve_report_generation(config)
        self.assertEqual(metadata["mode"], "TEMPLATE")
        self.assertEqual(style["document_overview"], "审核报告")
        self.assertNotIn("fallback_reason", metadata)
        for rules in [None, {}, {"mode": "RULES", "style_profile": {}}]:
            metadata, style = resolve_report_generation(rules)
            self.assertEqual(metadata["mode"], "RULES")
            self.assertNotIn("fallback_reason", metadata)
            self.assertIsNone(style)

    def test_unrelated_business_and_runtime_errors_still_propagate(self):
        for failure in [BusinessError("STORAGE_UNAVAILABLE", "不可用"), RuntimeError("unexpected")]:
            with self.subTest(error=type(failure)), patch(
                "app.modules.audit.report_template.service.generation.validate_style_profile", side_effect=failure,
            ):
                with self.assertRaises(type(failure)) as raised:
                    resolve_report_generation(generation({}))
                self.assertIs(raised.exception, failure)

    def test_report_create_and_update_save_effective_mode_without_changing_business_signals(self):
        for existing in [None, AuditReport(report_no="REP-old")]:
            with self.subTest(update=existing is not None), patch(
                "app.modules.audit.rule.service.rule_execution.AuditReportCRUD",
            ) as reports, patch("app.modules.audit.rule.service.rule_execution.AuditRuleCRUD") as rules:
                report_rule = {"rule_key": "report", "kind": "REPORT", "name": "报告",
                               "report_definition": {"formats": []}}
                risk_rule = {"rule_key": "risk", "rule_role": "RISK"}
                task = SimpleNamespace(id=1, input_revision=3, task_schema_json={"enabled_rule_keys": ["risk", "report"]},
                                       manifest_json={"rules": [risk_rule, report_rule], "report_generation": generation({})})
                original = deepcopy(task.manifest_json)
                execution = SimpleNamespace(id=2, status="RUNNING", result_json={})
                rules.return_value.results_for_scope.return_value = [
                    SimpleNamespace(id=7, rule_key="risk", status="ABNORMAL", message="实际风险"),
                ]
                reports.return_value.by_task_execution_rule.return_value = existing
                verdict = {"outputs": [], "report": {"content_type": "text/markdown", "content": "真实规则报告"}}
                AuditRuleExecutionService(Mock()).save_outputs(task, execution, report_rule, verdict, evaluation_id="eval")
                report = existing if existing is not None else reports.return_value.add.call_args.args[0]
                self.assertEqual(report.snapshot_json["report_generation"], FALLBACK)
                self.assertEqual(report.snapshot_json["rule_result_ids"], [7])
                self.assertEqual(report.content_md, "真实规则报告")
                self.assertTrue(report.scope_execution_complete)
                self.assertTrue(report.risk_conclusion_available)
                self.assertEqual(report.reasons_json["unresolved"], [])
                self.assertEqual(task.manifest_json, original)
                if existing is not None:
                    reports.return_value.add.assert_not_called()

    def test_only_profile_error_gets_new_resume_eligibility(self):
        task = SimpleNamespace(status="BLOCKED")
        service = AuditExecutionResumeService(Mock())
        execution = SimpleNamespace(status="FAILED", failure_code="REPORT_TEMPLATE_PROFILE_INVALID", failure_message="档案无效")
        self.assertIsNone(service.resume_blocked_reason(task, execution))
        self.assertIs(service.resumable_execution(task, execution), execution)
        for code in ["RULE_INPUT_FIELD_UNDECLARED", "RULE_EXECUTION_MODE_MISSING", "REPORT_TEMPLATE_NOT_READY"]:
            execution.failure_code = code
            self.assertIsNotNone(service.resume_blocked_reason(task, execution))
            self.assertIsNone(service.resumable_execution(task, execution))


class ReportGenerationReadTests(unittest.TestCase):
    def setUp(self):
        self.rule = {"rule_key": "report", "kind": "REPORT", "report_definition": {"sections": []}}
        self.task = SimpleNamespace(id=1, task_no="TASK", task_name="审核任务", input_revision=3,
                                    task_schema_json={"enabled_rule_keys": ["report"]},
                                    manifest_json={"rules": [self.rule], "report_generation": generation(style_profile())})
        self.report = AuditReport(id=8, report_no="REP", report_name="审核报告", audit_task_id=1, node_execution_id=2,
                                  content_type="text/markdown", content_md="正文", input_revision=3,
                                  scope_execution_complete=True, risk_conclusion_available=False, reasons_json={},
                                  snapshot_json={"rule_key": "report", "report_generation": deepcopy(FALLBACK)})

    def test_task_report_api_preserves_saved_generation_metadata_and_signals(self):
        with patch("app.modules.audit.report.service.views.AuditTaskLookupService") as tasks, \
             patch("app.modules.audit.report.service.views.AuditReportCRUD") as reports, \
             patch("app.modules.audit.report.service.views.current_execution_ids", return_value={2}), \
             patch("app.modules.audit.report.service.views.pending_rerun_by_execution", return_value={}), \
             patch("app.modules.audit.report.service.views.AuditReportValidityService") as validity:
            tasks.return_value.task_or_404.return_value = self.task
            reports.return_value.reports_for_task.return_value = [self.report]
            validity.return_value.result_signals.return_value = {}
            validity.return_value.report_current_reason.return_value = None
            response = ReportResponse.model_validate(AuditReportViewsService(Mock()).report_view("TASK"))
        self.assertEqual(response.report_generation.model_dump(), FALLBACK)
        self.assertEqual(response.status, "COMPLETE")
        self.assertEqual(response.reasons, [])
        self.assertTrue(response.scope_execution_complete)
        self.assertFalse(response.risk_conclusion_available)

    def test_report_detail_reads_snapshot_and_does_not_invent_missing_historical_metadata(self):
        with patch("app.modules.audit.report.service.query.crud.AuditReportCRUD") as reports, \
             patch("app.modules.audit.report.service.query.AuditReportValidityService") as validity:
            reports.return_value.report_by_no.return_value = self.report
            reports.return_value.report_context.return_value = (self.task, None, None, None)
            validity.return_value.result_signals.return_value = {}
            validity.return_value.report_current_reason.return_value = None
            service = AuditReportQueryService(Mock())
            response = ReportDetailResponse.model_validate(service.report_detail("REP"))
            self.assertEqual(response.report_generation.model_dump(), FALLBACK)
            self.report.snapshot_json.pop("report_generation")
            historical = ReportDetailResponse.model_validate(service.report_detail("REP"))
            self.assertIsNone(historical.report_generation)
        self.assertEqual(self.task.manifest_json["report_generation"]["mode"], "TEMPLATE")


if __name__ == "__main__":
    unittest.main()
