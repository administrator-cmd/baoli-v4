"""提交后静态审查九项修复的回归测试，不访问数据库、模型或文件。

覆盖能在进程内判定修复效果的四项：批量抽取的工具渠道故障、续跑范围的分段统计、
旧结果加数标签补全、字段事实身份唯一性；另验证详情读取共用一份当前字段事实配对。
前端三项（草稿清空、确认后的刷新、未上传材料的按钮禁用）没有前端测试运行器，
只由 typecheck 与 lint 覆盖。

用法（在 backend 下）：`PYTHONPATH=. python test/test_field_review_fixes.py -v`
"""

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.common.exceptions import BusinessError
from app.domain.rule_library import _validate_field_fact_identity
from app.services.audit import field_review, resume
from app.services.audit.flow import extraction_batch
from app.services.audit.flow.plan import ReviewBoundary
from app.services.audit.tasks.detail import _comparison_sources


def _field(key: str, **extra):
    return {
        "field_key": key,
        "name": key,
        "material_key": "m1",
        "value_type": "TEXT",
        **extra,
    }


class FieldFactIdentityTests(unittest.TestCase):
    """修复 4：身份唯一性覆盖全部字段，不再只查启用规则的产出字段。"""

    def test_duplicate_identity_rejected_even_without_output_rule(self):
        fields = [
            _field("f1", source_ref="FEX-001"),
            _field("f2", source_ref="FEX-001"),
        ]
        with self.assertRaises(BusinessError) as caught:
            _validate_field_fact_identity(fields)
        self.assertEqual(caught.exception.code, "RULE_FIELD_IDENTITY_CONFLICT")
        self.assertEqual(caught.exception.details["field_keys"], ["f1", "f2"])

    def test_distinct_scope_allows_same_business_code(self):
        fields = [
            _field("f1", source_ref="FEX-001", period_scope="CURRENT_PERIOD"),
            _field("f2", source_ref="FEX-001", period_scope="LAST_PERIOD"),
        ]
        _validate_field_fact_identity(fields)

    def test_missing_source_ref_falls_back_to_field_key(self):
        fields = [_field("f1"), _field("f2")]
        _validate_field_fact_identity(fields)


class ComparisonSourceTests(unittest.TestCase):
    """修复 3：旧结果里的空标签占位（["", ""]）也要补齐。"""

    def _task(self):
        return SimpleNamespace(task_schema_json={"fields": [
            {"field_key": "f1", "name": "合计金额"},
            {"field_key": "f2", "name": "分项金额"},
        ]})

    def test_empty_label_placeholders_are_backfilled(self):
        result = {"tool_findings": [{
            "comparison": {"left_index": 0, "right_index": 0, "addends": [100, 60], "addend_labels": ["", ""]},
            "input_snapshot": [
                {"material_key": "m1", "field_key": "f1", "raw_value": "160"},
                {"material_key": "m1", "field_key": "f2", "raw_value": "60"},
            ],
        }]}
        documents = {"m1": SimpleNamespace(material_name="主合同")}
        fixed = _comparison_sources(result, task=self._task(), documents_by_material_key=documents)
        self.assertEqual(fixed["tool_findings"][0]["comparison"]["addend_labels"], ["主合同 · 分项金额"])

    def test_existing_labels_are_kept(self):
        result = {"tool_findings": [{
            "comparison": {"addends": [100, 60], "addend_labels": ["原标签"]},
            "input_snapshot": [
                {"material_key": "m1", "field_key": "f1", "raw_value": "160"},
                {"material_key": "m1", "field_key": "f2", "raw_value": "60"},
            ],
        }]}
        documents = {"m1": SimpleNamespace(material_name="主合同")}
        fixed = _comparison_sources(result, task=self._task(), documents_by_material_key=documents)
        self.assertEqual(fixed["tool_findings"][0]["comparison"]["addend_labels"], ["原标签"])


def _rule(key: str, kind: str = "RULE"):
    return {"rule_key": key, "kind": kind, "inputs": [], "outputs": [], "source_rule_keys": []}


def _entry(key: str, kind: str = "RULE", cursor: int = 0):
    return {"cursor": cursor, "rule_key": key, "rule_code": key, "node_code": "node", "rule": _rule(key, kind)}


def _saved(*keys: str):
    return {key: SimpleNamespace(status="PASSED", loop_status="DONE") for key in keys}


class ResumePlanTests(unittest.TestCase):
    """修复 2：只有核对后段算「等待核对」，已结束的抽取段计入保留。"""

    def setUp(self):
        self.plan = [
            _entry("ex1", "EXTRACTION", 0), _entry("ex2", "EXTRACTION", 1),
            _entry("aft1", cursor=2), _entry("aft2", cursor=3),
        ]
        self.task = SimpleNamespace(manifest_json={"rules": [entry["rule"] for entry in self.plan]})
        self.execution = SimpleNamespace(id=7, result_json={})

    def _plan(self, saved, *, allowed, after):
        with patch.object(resume, "_saved_results", return_value=saved):
            return resume.resume_plan(
                MagicMock(), self.task, self.execution,
                plan=self.plan, allowed_keys=allowed, deferred_keys=after,
            )

    def test_confirmed_execution_counts_extraction_as_preserved(self):
        plan = self._plan(
            _saved("ex1", "ex2", "aft1", "aft2"),
            allowed={"aft1", "aft2"}, after={"aft1", "aft2"},
        )
        self.assertEqual(plan.deferred, ())
        self.assertEqual(set(plan.closed), {"ex1", "ex2"})
        self.assertEqual(plan.summary(), {"preserved": 4, "pending": 0, "dependent": 0, "waiting_review": 0})

    def test_unconfirmed_and_closed_reports_after_segment_only(self):
        plan = self._plan(_saved("ex1", "ex2"), allowed=set(), after={"aft1", "aft2"})
        self.assertEqual(set(plan.deferred), {"aft1", "aft2"})
        self.assertEqual(set(plan.closed), {"ex1", "ex2"})
        self.assertEqual(plan.summary(), {"preserved": 2, "pending": 0, "dependent": 0, "waiting_review": 2})

    def test_running_extraction_segment_keeps_after_segment_deferred(self):
        plan = self._plan(_saved("ex1"), allowed={"ex1", "ex2"}, after={"aft1", "aft2"})
        self.assertEqual(set(plan.deferred), {"aft1", "aft2"})
        self.assertEqual(plan.closed, ())
        self.assertEqual(plan.summary(), {"preserved": 1, "pending": 1, "dependent": 0, "waiting_review": 2})


class ReviewBoundaryPhaseTests(unittest.TestCase):
    """修复 2 依赖的分段口径：确认后只执行核对后段，抽取段不重跑。"""

    boundary = ReviewBoundary(extraction=("ex1",), after=("aft1",))

    def test_phase_keys_by_state(self):
        self.assertEqual(self.boundary.phase_keys(confirmed=False), {"ex1"})
        self.assertEqual(self.boundary.phase_keys(confirmed=False, extraction_closed=True), set())
        self.assertEqual(self.boundary.phase_keys(confirmed=True), {"aft1"})


class FieldReviewPairReuseTests(unittest.TestCase):
    """修复 9：传入已配对的当前字段事实后，不再重复查询。"""

    task = SimpleNamespace(
        id=1, scenario_version_id=1, input_revision=3,
        task_schema_json={"fields": [
            {"field_key": "f1", "material_key": "m1", "name": "金额", "extraction_enabled": True},
        ]},
    )

    def test_digest_and_confirmation_view_use_provided_pairs(self):
        with patch.object(field_review, "_current_field_fact_pairs") as pairs_fn:
            digest = field_review.field_review_digest(MagicMock(), self.task, {})
            view = field_review.confirmation_view(
                MagicMock(), self.task, None,
                required=False, after_review_rule_count=3, pairs={},
            )
        pairs_fn.assert_not_called()
        self.assertEqual(digest, field_review.field_review_digest(MagicMock(), self.task, {}))
        self.assertEqual(view["missing_fields"], [])
        self.assertFalse(view["required"])

    def test_missing_fields_without_upload_reports_reason(self):
        document = SimpleNamespace(id=10, material_key="m1", material_name="主合同")
        with patch.object(field_review, "_current_field_fact_pairs") as pairs_fn, \
                patch.object(field_review, "_current_upload_map", return_value={}), \
                patch.object(field_review, "_task_documents", return_value=[document]):
            missing = field_review.missing_fields(MagicMock(), self.task, {})
        pairs_fn.assert_not_called()
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["reason"], field_review.MISSING_MATERIAL)
        self.assertFalse(missing[0]["material_uploaded"])


class ExtractionBatchChannelFailureTests(unittest.TestCase):
    """修复 1：工具内部模型调用遇到渠道故障时，本批不再请求模型。"""

    def test_tool_channel_failure_stops_batch_without_second_model_request(self):
        db = MagicMock()
        task = SimpleNamespace(task_no="task-test", id=1)
        execution = SimpleNamespace(id=5, result_json={"attempt_no": 1})
        rules = [
            {"rule_key": "r1", "output_definitions": [{"field_key": "f1", "material_key": "m1"}]},
            {"rule_key": "r2", "output_definitions": [{"field_key": "f2", "material_key": "m1"}]},
        ]
        entries = [{"rule": rule, "cursor": index} for index, rule in enumerate(rules)]
        agent = MagicMock()
        agent.available_tools.return_value = []
        agent.initial_batch_messages.return_value = []
        failure = {"code": "AUDIT_LLM_CHANNEL_UNAVAILABLE", "message": "渠道不可用"}
        turns = [
            {
                "pending_tool_calls": [{"tool_call_id": "call-1", "tool_name": "RISK_TRIGGER"}],
                "messages": [], "loop_status": "RUNNING", "loop_count": 1,
            },
            # 修复前会继续请求模型；这里放一条可解析的结果，被调用即说明修复失效。
            {
                "last_model_response": {"content": '{"fields": []}'},
                "messages": [], "loop_status": "RUNNING", "loop_count": 2,
            },
        ]
        with patch.object(extraction_batch, "agent_step", return_value=nullcontext()), \
                patch.object(extraction_batch, "task_or_404", return_value=task), \
                patch.object(extraction_batch, "_task_manifest",
                             return_value={"materials": [{"material_key": "m1", "name": "主合同"}]}), \
                patch.object(extraction_batch, "execution_in_progress", return_value=execution), \
                patch.object(extraction_batch, "execution_attempt_no", return_value=1), \
                patch.object(extraction_batch, "agent_module", return_value=agent), \
                patch.object(extraction_batch, "evaluation_context",
                             return_value={"upstream_facts": [], "upstream_results": []}), \
                patch.object(extraction_batch, "graph_call_model", side_effect=turns) as call_model, \
                patch.object(extraction_batch, "graph_execute_tool_calls", return_value={
                    "tool_results": [{"tool_call_id": "call-1"}],
                    "unresolved_items": [],
                    "channel_failure": failure,
                }), \
                patch.object(extraction_batch, "graph_append_tool_results") as append_results, \
                patch.object(extraction_batch, "revoke_outputs"), \
                patch.object(extraction_batch, "save_rule_result",
                             return_value=SimpleNamespace(status="NOT_EXECUTED", result_json={})):
            outcomes = extraction_batch.graph_execute_extraction_batch(
                db, "task-test", "node", 5, entries,
            )
        self.assertEqual(call_model.call_count, 1)
        append_results.assert_not_called()
        self.assertEqual(len(outcomes), 2)
        for outcome in outcomes:
            self.assertEqual(outcome["channel_failure"], failure)
            self.assertEqual(outcome["loop_status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
