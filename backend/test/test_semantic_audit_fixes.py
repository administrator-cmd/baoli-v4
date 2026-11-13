"""针对本轮静态审查修复的纯单元回归，不访问数据库、文件或外部服务。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.common.exceptions import BusinessError
from app.domain.configuration.rule_library import execution_order
from app.modules.audit.field.service import evidence
from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
from app.modules.audit.task.service.documents import _assert_task_input_editable


class EvidenceMatchingTests(unittest.TestCase):
    def test_multiple_quotes_normalize_each_source_text_once(self):
        service = AuditFieldEvidenceService()
        with patch(
            "app.modules.audit.field.service.evidence._locatable_text",
            wraps=evidence._locatable_text,
        ) as normalize:
            result = service._check_evidence_contents(
                ["合同 编号：ABC-123", "签订 日期：2026年11月"],
                ["ABC-123", "2026年11月", " \t"],
            )
        self.assertEqual(result, ["FOUND", "FOUND", "NOT_CHECKABLE"])
        # 两段材料正文各规范化一次，再分别规范化三个引文。
        self.assertEqual(normalize.call_count, 5)


class TaskMaterialEditabilityTests(unittest.TestCase):
    def test_failed_task_can_repair_inputs_but_completed_task_stays_locked(self):
        _assert_task_input_editable(SimpleNamespace(status="FAILED"), action="替换材料")
        with self.assertRaises(BusinessError) as caught:
            _assert_task_input_editable(SimpleNamespace(status="COMPLETED"), action="替换材料")
        self.assertEqual(caught.exception.code, "TASK_DOCUMENT_LOCKED")
        with self.assertRaises(BusinessError) as caught:
            _assert_task_input_editable(SimpleNamespace(status="RUNNING"), action="替换材料")
        self.assertEqual(caught.exception.code, "AUDIT_TASK_RUNNING")


class ExecutionPlanCopyTests(unittest.TestCase):
    def test_ordering_does_not_mutate_input_and_keeps_nested_data_shared(self):
        nested = {"column": ["value"]}
        rule = {
            "rule_key": "rule_1",
            "kind": "RULE",
            "nested": nested,
            "unavailable_dependencies": ["old"],
            "dependency_details": [{"status": "old"}],
        }
        ordered = execution_order([rule], all_rules=[rule])
        self.assertEqual(rule["unavailable_dependencies"], ["old"])
        self.assertEqual(rule["dependency_details"], [{"status": "old"}])
        self.assertIsNot(ordered[0], rule)
        self.assertIs(ordered[0]["nested"], nested)


if __name__ == "__main__":
    unittest.main()
