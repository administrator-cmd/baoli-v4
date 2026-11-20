"""Regression cases for current values and missing inputs without a model or database."""

import csv
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.graphs.audit_rule_runner import run_rule_evaluation
from app.modules.audit.field.service import current_values
from app.modules.audit.field.service.value_store import AuditFieldValueStoreService
from app.modules.audit.rule.service.input_validation import missing_rule_inputs


class CurrentValueTests(unittest.TestCase):
    def test_invalidation_leaves_only_current_effective_values_and_preserves_history(self):
        task = SimpleNamespace(id=1, task_no="TASK_test", created_at=None, value_file_json=None)
        fact = SimpleNamespace(audit_task_id=1, is_current=True, status="PRESENT", fact_code="amount",
                               material_key="m1", period_scope="2026", statement_scope=None, entity_scope=None,
                               value_type="DECIMAL", revision_no=1)
        amount = Decimal("9007199254740993.0100")
        records = {7: fact, 8: SimpleNamespace(**{**vars(fact), "status": "NOT_PRESENT", "fact_code": "optional"})}
        db = Mock(info={})
        db.get.side_effect = lambda model, record_id: records[record_id]
        read_table = AuditFieldValueStoreService()._read_table
        with TemporaryDirectory() as tmp, patch.object(current_values, "audit_directory", return_value="audit/tasks/test"), +                patch.object(current_values, "resolve_storage_path", side_effect=lambda relative: Path(tmp, relative)), +                patch.object(current_values, "AuditFieldValueFilesService"), +                patch.object(current_values.AuditFieldCRUD, "current_fact_ids", side_effect=lambda task_id: {
                    record_id for record_id, item in records.items() if item.is_current
                }):
            store = current_values.CurrentTaskValues(db)
            context = lambda *args: ({"业务类别": "字段事实", "名称": "金额", "材料名称": "合同"}, {})
            store.write(task, None, [("TaskFact", 7, {"value": amount}), ("TaskFact", 8, {"value": None})],
                        read_table=read_table, business_context=context, format_version=7)
            batch = task.value_file_json["batches"][0]
            self.assertEqual(set(batch["files"]), {"values.csv", "history.json"})
            with Path(tmp, batch["files"]["values.csv"]).open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual([(row["事实ID"], row["当前值"]) for row in rows], [("7", str(amount))])
            fact.is_current = False
            store.write(task, batch, [], read_table=read_table, business_context=context, format_version=7)
            db.info.clear()
            batch = task.value_file_json["batches"][0]
            self.assertEqual(batch["record_count"], 0)
            self.assertEqual(store.find(task, batch, "TaskFact", 7, read_table)["value"], amount)

    def test_typed_values_survive_one_csv_cell(self):
        for value in (0, False, "001", date(2026, 11, 18), {"kind": "decimal", "value": "literal"}):
            with self.subTest(value=value):
                kind, cell = current_values._value_cells(value)
                row = {"值类型": kind, "当前值": cell, **dict.fromkeys(current_values.FACT_ATTRIBUTES.values(), "")}
                restored = current_values._fact_payload(row)["value"]
                self.assertEqual(restored, value)
                self.assertIs(type(restored), type(value))


class MissingInputTests(unittest.TestCase):
    def test_comparison_distinguishes_confirmed_absence_from_failed_extraction(self):
        inputs = [{"field_key": code, "status": status, "normalized_value": value} for code, status, value in (
            ("confirmed_absent", "NOT_PRESENT", None), ("uncertain", "EVIDENCE_INSUFFICIENT", None),
            ("zero", "PRESENT", 0), ("false", "REVIEWED", False),
        )]
        compare = missing_rule_inputs({"execution_mode": "VALUE_COMPARE"}, inputs, [])
        calculate = missing_rule_inputs({"execution_mode": "CALCULATE"}, inputs, [])
        self.assertEqual([item["field_key"] for item in compare], ["uncertain"])
        self.assertEqual([item["field_key"] for item in calculate], ["confirmed_absent", "uncertain"])

    def test_preflight_verdict_is_saved_without_model_call(self):
        verdict = {"status": "EVIDENCE_INSUFFICIENT", "missing_items": ["合同金额"]}
        nodes = Mock()
        nodes.graph_load_rule_instance.return_value = {
            "rule_instance": {"rule_key": "r1"}, "evaluation_id": "e1", "messages": None,
            "loop_count": 0, "last_model_response": None, "verdict_errors": [],
        }
        nodes.graph_prepare_agent_context.return_value = {
            "candidate_verdict": verdict, "loop_status": "DONE", "unresolved_items": [],
        }
        nodes.graph_save_evaluation_result.return_value = {
            "rule_outcome": {"status": "EVIDENCE_INSUFFICIENT", "loop_count": 0},
        }
        run_rule_evaluation(None, nodes, task_no="t", node_code="n", execution_id=1, rule_cursor=0,
                            planned_rule={"rule_key": "r1", "execution_mode": "VALUE_COMPARE"})
        nodes.graph_call_model.assert_not_called()
        self.assertEqual(nodes.graph_save_evaluation_result.call_args.args[5], verdict)
