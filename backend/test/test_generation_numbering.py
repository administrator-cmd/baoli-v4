"""编号语义回归：只用内存模型替身和临时 Excel，不访问数据库或真实模型。"""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

from app.common.exceptions import BusinessError
from app.domain.generation_assistant import AssistantReply, apply_edits
from app.domain.rulebook_generation import (
    GenerationDocument,
    GenerationSource,
    business_code_column,
    collect_document_problems,
    normalize_business_codes,
)
from app.modules.imports.generation.service.export import GenerationExportService
from app.modules.imports.generation.service.llm import GenerationLlmService


def change_document():
    """表名不参与语义识别，同一批次有多条修改、同一规则可被多次引用。"""
    return GenerationDocument.model_validate({
        "schema_version": "rulebook_generation_v2", "title": "业务资料", "requirements": [], "issues": [],
        "sheets": [{
            "sheet_id": "rules", "name": "当前要求", "source": {"kind": "USER_INPUT"},
            "columns": [{"column_id": "code", "name": "规则ID", "code_role": "IDENTITY"},
                        {"column_id": "body", "name": "要求"}],
            "rows": [{"row_id": "r1", "entry_kind": "RULE", "cells": {"code": "FSCV-001", "body": "保留原要求"},
                      "source": {"kind": "USER_INPUT"}}],
        }, {
            "sheet_id": "history", "name": "资料流转", "source": {"kind": "USER_INPUT"},
            "columns": [{"column_id": "batch", "name": "批次ID", "code_role": "GROUP"},
                        {"column_id": "target", "name": "规则ID", "code_role": "REFERENCE"},
                        {"column_id": "body", "name": "变更详情"}],
            "rows": [{"row_id": f"change-{index}", "entry_kind": "REFERENCE",
                      "cells": {"batch": "BATCH-20260803154921", "target": "FSCV-001", "body": f"第{index}项修改"},
                      "source": {"kind": "USER_INPUT"}} for index in range(10)],
        }],
    })


class GenerationNumberingTests(unittest.TestCase):
    def test_group_and_target_references_need_no_individual_code(self):
        current = normalize_business_codes(change_document())
        self.assertIsNone(business_code_column(current.sheets[1]))
        self.assertTrue(all(row.business_code == "" for row in current.sheets[1].rows))
        self.assertEqual(collect_document_problems(current, set(), require_codes=True), [])

    def test_explicit_role_overrides_legacy_heading_and_marker(self):
        current = change_document()
        current.sheets[1].columns[1].is_business_code = True
        self.assertIsNone(business_code_column(current.sheets[1]))
        self.assertEqual(collect_document_problems(current, set(), require_codes=True), [])

    def test_real_rule_and_material_still_require_codes(self):
        for kind in ("RULE", "MATERIAL"):
            with self.subTest(kind=kind):
                current = change_document()
                current.sheets[0].rows[0].entry_kind = kind
                current.sheets[0].rows[0].cells["code"] = ""
                kinds = {item["kind"] for item in collect_document_problems(current, set(), require_codes=True)}
                self.assertIn("row.business_code_missing", kinds)

    def test_numbered_reference_entries_still_check_missing_codes(self):
        current = change_document()
        current.sheets[0].rows[0].entry_kind = "REFERENCE"
        current.sheets[0].rows[0].cells["code"] = ""
        kinds = {item["kind"] for item in collect_document_problems(current, set(), require_codes=True)}
        self.assertIn("row.business_code_missing", kinds)

    def test_batch_or_reference_cannot_be_sneaked_into_business_code(self):
        for code in ("BATCH-20260803154921", "FSCV-001"):
            with self.subTest(code=code):
                current = change_document()
                current.sheets[1].rows[0].business_code = code
                kinds = {item["kind"] for item in collect_document_problems(current, set())}
                self.assertIn("row.non_identity_business_code", kinds)

    def test_correcting_roles_does_not_restore_old_wrong_number(self):
        before = change_document()
        before.sheets[1].columns[1].code_role = None
        before.sheets[1].rows[0].business_code = "FSCV-001"
        corrected = before.model_copy(deep=True)
        corrected.sheets[1].columns[1].code_role = "REFERENCE"
        corrected.sheets[1].rows[0].business_code = ""
        result = normalize_business_codes(corrected, before)
        self.assertEqual(result.sheets[1].rows[0].business_code, "")
        self.assertEqual(result.sheets[1].rows[0].cells, before.sheets[1].rows[0].cells)

    def test_duplicates_fail_in_document_scope_and_within_local_scope(self):
        current = change_document()
        other = current.sheets[0].model_copy(deep=True)
        other.sheet_id, other.name, other.rows[0].row_id = "other", "另一组要求", "r2"
        current.sheets.append(other)
        problems = collect_document_problems(current, set())
        duplicate = next(item for item in problems if item["kind"] == "row.duplicate_business_code")
        self.assertIn("另一组要求", duplicate["owner"])
        self.assertIn("当前要求", duplicate["reference"])
        for sheet in (current.sheets[0], other):
            sheet.business_code_scope = "SHEET"
        self.assertEqual(collect_document_problems(current, set()), [])
        repeated = other.rows[0].model_copy(deep=True)
        repeated.row_id = "r3"
        other.rows.append(repeated)
        self.assertIn("row.duplicate_business_code", {item["kind"] for item in collect_document_problems(current, set())})

    def test_assistant_can_correct_numbering_roles_without_rewriting_cells(self):
        current = change_document()
        current.sheets[1].columns[1].code_role = None
        cells = [dict(row.cells) for row in current.sheets[1].rows]
        columns = [column.model_dump() for column in current.sheets[1].columns]
        columns[1]["code_role"] = "REFERENCE"
        reply = AssistantReply.model_validate({"kind": "EDIT", "message": "已整理编号语义", "edits": [
            {"op": "update_sheet", "sheet_id": "history", "columns": columns},
        ]})
        result = apply_edits(current, reply)
        self.assertEqual([row.cells for row in result.sheets[1].rows], cells)
        self.assertEqual(collect_document_problems(result, set()), [])

    def test_assistant_can_add_unnumbered_auxiliary_record(self):
        current = change_document()
        row = current.sheets[1].rows[0].model_dump(mode="json")
        row["row_id"] = "new-history"
        reply = AssistantReply.model_validate({"kind": "EDIT", "message": "已补充资料", "edits": [
            {"op": "upsert_row", "sheet_id": "history", "row": row},
        ]})
        result = apply_edits(current, reply)
        self.assertEqual(result.sheets[1].rows[-1].business_code, "")
        self.assertEqual(collect_document_problems(result, set(), require_codes=True), [])

    def test_assistant_still_rejects_new_rule_without_code(self):
        current = change_document()
        row = current.sheets[0].rows[0].model_dump(mode="json")
        row["row_id"], row["business_code"], row["cells"]["code"] = "new-rule", "", ""
        reply = AssistantReply.model_validate({"kind": "EDIT", "message": "新增要求", "edits": [
            {"op": "upsert_row", "sheet_id": "rules", "row": row},
        ]})
        with self.assertRaises(BusinessError):
            apply_edits(current, reply)

    def test_export_preserves_all_batch_rows_and_no_extra_number_column(self):
        current = change_document()
        current.sheets[0].rows[0].related_row_ids = ["change-0"]
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        stream = io.BytesIO()
        workbook.save(stream)
        workbook.close()
        stream.seek(0)
        saved = load_workbook(stream)
        try:
            sheet = saved["资料流转"]
            self.assertEqual([cell.value for cell in sheet[1]], ["批次ID", "规则ID", "变更详情"])
            self.assertEqual(sheet.max_row, 11)
            self.assertEqual([sheet.cell(index, 1).value for index in range(2, 12)], ["BATCH-20260803154921"] * 10)
            self.assertTrue(all(sheet.cell(index, 2).value == "FSCV-001" for index in range(2, 12)))
            linked = [cell.value for cell in saved["当前要求"][2]]
            self.assertTrue(any(isinstance(value, str) and "资料流转" in value for value in linked))
        finally:
            saved.close()

    def test_export_qualifies_sheet_local_codes_in_relations(self):
        current = change_document()
        first = current.sheets[0]
        first.business_code_scope = "SHEET"
        second = first.model_copy(deep=True)
        second.sheet_id, second.name, second.rows[0].row_id = "other", "另一组要求", "r2"
        current.sheets.append(second)
        current.sheets[1].rows[0].related_row_ids = ["r1", "r2"]
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        try:
            values = [cell.value for cell in workbook["资料流转"][2]]
            self.assertIn("当前要求 · FSCV-001；另一组要求 · FSCV-001", values)
        finally:
            workbook.close()

    def test_initial_workflow_accepts_repeated_batch_without_correction(self):
        current = change_document()
        requirements = []
        ir_sheets = []
        source_rows = set()
        for sheet in current.sheets:
            rows = [SimpleNamespace(row_no=1, cells={f"{chr(65 + index)}1": column.name
                                                    for index, column in enumerate(sheet.columns)})]
            for number, row in enumerate(sheet.rows, 2):
                origin = {"sheet_name": sheet.name, "row_no": number}
                row.source = GenerationSource.model_validate({"kind": "ORIGINAL_EXCEL", "origin": origin,
                                                               "requirement_ids": [row.row_id]})
                requirements.append({"requirement_id": row.row_id, "text": "\n".join(row.cells.values()),
                                     "category": "原文资料", "source": row.source.model_dump(mode="json")})
                rows.append(SimpleNamespace(row_no=number, cells={f"{chr(65 + index)}{number}": row.cells[column.column_id]
                                                                  for index, column in enumerate(sheet.columns)}))
                source_rows.add((sheet.name, number))
            ir_sheets.append(SimpleNamespace(name=sheet.name, rows=rows))
        payload = current.model_dump(mode="json")
        payload["requirements"] = requirements
        service = GenerationLlmService()
        with patch.object(service, "_workbook_payload", return_value=([], source_rows)), patch(
            "app.modules.imports.generation.service.llm.call_json",
            side_effect=[{"schema_version": "rulebook_generation_v2", "requirements": requirements, "unclear_items": []},
                         payload, {"problems": []}],
        ) as model:
            result = service.build_document(SimpleNamespace(sheets=ir_sheets), business_background="", generation_requirements="",
                                            operation_no="numbering-regression", job_no="test")
        self.assertEqual(model.call_count, 3)
        self.assertTrue(all(row["business_code"] == "" for row in result["sheets"][1]["rows"]))


if __name__ == "__main__":
    unittest.main()
