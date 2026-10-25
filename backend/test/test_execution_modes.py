"""规则执行方式校验、排序与组合汇总的回归测试，不访问数据库或模型。"""

import unittest

from app.common.exceptions import BusinessError
from app.domain.rule_library import _validate_execution_modes, execution_order
from app.graphs.audit_rule_runner import run_rule_evaluation
from app.services.audit.flow.aggregate import build_aggregate_verdict


def _rule(key: str, mode: str, **extra):
    return {
        "rule_key": key,
        "rule_code": key.upper(),
        "name": key,
        "kind": "RULE",
        "enabled": True,
        "execution_mode": mode,
        "inputs": [],
        "outputs": [],
        "source_rule_keys": [],
        **extra,
    }


class ExecutionModeValidationTests(unittest.TestCase):
    def test_unknown_mode_blocks_publish(self):
        with self.assertRaises(BusinessError) as caught:
            _validate_execution_modes([_rule("r1", "UNKNOWN")])
        self.assertEqual(caught.exception.code, "RULE_EXECUTION_MODE_MISSING")
        self.assertEqual(caught.exception.details["rules"][0]["name"], "r1")

    def test_aggregate_requires_sources(self):
        with self.assertRaises(BusinessError) as caught:
            _validate_execution_modes([_rule("r1", "AGGREGATE")])
        self.assertEqual(caught.exception.code, "RULE_EXECUTION_MODE_INVALID")

    def test_compare_requires_inputs(self):
        with self.assertRaises(BusinessError) as caught:
            _validate_execution_modes([_rule("r1", "VALUE_COMPARE")])
        self.assertEqual(caught.exception.code, "RULE_EXECUTION_MODE_INVALID")


class ExecutionOrderTests(unittest.TestCase):
    def test_aggregate_after_source_rule(self):
        source = _rule("source", "VALUE_COMPARE", inputs=[{"field_key": "f1", "material_key": "m1"}])
        aggregate = _rule("aggregate", "AGGREGATE", source_rule_keys=["source"])
        ordered = execution_order([aggregate, source], all_rules=[aggregate, source])
        self.assertEqual([rule["rule_key"] for rule in ordered], ["source", "aggregate"])


class AggregateVerdictTests(unittest.TestCase):
    def _verdict(self, statuses):
        rule = _rule("aggregate", "AGGREGATE", source_rule_keys=[key for key, _ in statuses])
        results = [
            {"rule_key": key, "rule_name": key, "status": status, "message": f"{key}:{status}"}
            for key, status in statuses
        ]
        return build_aggregate_verdict(rule, results)

    def test_any_abnormal_wins(self):
        verdict = self._verdict([("a", "PASSED"), ("b", "ABNORMAL"), ("c", "EVIDENCE_INSUFFICIENT")])
        self.assertEqual(verdict["status"], "ABNORMAL")

    def test_unresolved_without_abnormal(self):
        verdict = self._verdict([("a", "PASSED"), ("b", "EVIDENCE_INSUFFICIENT")])
        self.assertEqual(verdict["status"], "EVIDENCE_INSUFFICIENT")

    def test_all_passed(self):
        verdict = self._verdict([("a", "PASSED"), ("b", "COMPUTED")])
        self.assertEqual(verdict["status"], "PASSED")

    def test_missing_source(self):
        rule = _rule("aggregate", "AGGREGATE", source_rule_keys=["a", "b"])
        verdict = build_aggregate_verdict(rule, [
            {"rule_key": "a", "rule_name": "a", "status": "PASSED", "message": "ok"},
        ])
        self.assertEqual(verdict["status"], "EVIDENCE_INSUFFICIENT")
        self.assertTrue(verdict["missing_items"])


class _AggregateRunner:
    """只实现组合汇总路径，模型调用一旦发生即失败。"""

    def __init__(self):
        self.saved = None

    def graph_load_rule_instance(self, *args, **kwargs):
        return {
            "rule_instance": {"rule_key": "r1", "rule_code": "R1", "rule_name": "汇总", "inputs": []},
            "messages": None, "allowed_tools": [], "loop_count": 0,
            "evaluation_id": "evaluation:1:R1", "tool_results": [],
            "unresolved_items": [], "pending_tool_calls": [],
            "last_model_response": None, "verdict_errors": [],
        }

    def graph_build_aggregate_verdict(self, *args, **kwargs):
        return {"candidate_verdict": {"status": "PASSED", "message": "汇总完成", "basis": []},
                "loop_status": "DONE", "unresolved_items": [], "last_error": None}

    def graph_call_model(self, *args, **kwargs):
        raise AssertionError("组合汇总不应调用模型")

    def graph_save_evaluation_result(self, *args, **kwargs):
        self.saved = args
        return {"rule_outcome": {"status": "PASSED", "loop_count": 0, "rule_key": "r1"}}


class AggregateExecutionTests(unittest.TestCase):
    def test_aggregate_skips_model(self):
        nodes = _AggregateRunner()
        outcome = run_rule_evaluation(
            None, nodes, task_no="t", node_code="n", execution_id=1, rule_cursor=0,
            planned_rule={"rule_key": "r1", "execution_mode": "AGGREGATE"},
        )
        self.assertEqual(outcome["status"], "PASSED")
        self.assertIsNotNone(nodes.saved)


if __name__ == "__main__":
    unittest.main()
