"""规则库助手的编号、编辑边界和真实 Excel 导出回归；不调用模型或连接数据库。"""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Border, Font, PatternFill, Side

from app.common.exceptions import BusinessError
from app.domain.generation_assistant import (
    AssistantReply, ReviewTextRepair, apply_edits, apply_review_text_repairs, document_changes,
)
from app.domain.rulebook_generation import (
    GenerationColumn, GenerationDocument, GenerationEntry, GenerationSource, collect_document_problems, normalize_business_codes,
    source_record_coverage_problems,
)
from app.modules.imports.generation.service.export import GenerationExportService
from app.modules.imports.generation.service.llm import GenerationLlmService


def document():
    return normalize_business_codes(GenerationDocument.model_validate({
        "schema_version": "rulebook_generation_v2", "title": "合同业务规则库", "requirements": [], "issues": [],
        "sheets": [{
            "sheet_id": "s1", "name": "合同业务", "columns": [
                {"column_id": "name", "name": "名称"}, {"column_id": "body", "name": "业务内容"},
            ], "source": {"kind": "USER_INPUT"}, "rows": [
                {"row_id": "m1", "business_code": "MAT-001", "entry_kind": "MATERIAL", "cells": {"name": "主合同", "body": "适用说明"},
                 "related_row_ids": ["r1", "d1"], "source": {"kind": "USER_INPUT"}},
                {"row_id": "r1", "business_code": "RUL-001", "entry_kind": "RULE", "cells": {"name": "违约金", "body": "应计算违约金，原文未给算法"},
                 "source": {"kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 2}}},
                {"row_id": "d1", "business_code": "REF-001", "entry_kind": "REFERENCE", "cells": {"name": "主体字典", "body": "用于名称对照"},
                 "source": {"kind": "USER_INPUT"}},
            ],
        }],
    }))


def edit(*operations):
    return AssistantReply.model_validate({"kind": "EDIT", "message": "已整理", "edits": list(operations)})


class NumberingTests(unittest.TestCase):
    def test_source_id_aliases_are_used_without_stealing_relation_ids(self):
        for heading in ("原编号", "规则 ID", "材料ID", "材料规则ID", "质量规则编号", "映射编号", "字典 ID", "风险ID", "报告编号"):
            with self.subTest(heading=heading):
                current = document()
                current.sheets[0].columns.extend([
                    GenerationColumn(column_id="related", name="关联规则ID"),
                    GenerationColumn(column_id="own", name=heading),
                ])
                for index, row in enumerate(current.sheets[0].rows):
                    row.cells.update(related="OTHER-999", own=f"SOURCE-{index:03d}")
                result = normalize_business_codes(current, current)
                self.assertEqual([row.business_code for row in result.sheets[0].rows],
                                 ["SOURCE-000", "SOURCE-001", "SOURCE-002"])
                self.assertEqual(current.sheets[0].rows[0].business_code, "MAT-001")

    def test_original_code_wins_over_old_extra_numbering_column(self):
        current = document()
        current.sheets[0].columns.extend([
            GenerationColumn(column_id="extra", name="编制编号"),
            GenerationColumn(column_id="own", name="材料规则ID"),
        ])
        for index, row in enumerate(current.sheets[0].rows, 1):
            row.cells.update(extra=row.business_code, own=f"CT-MCL-{index:03d}")
        result = normalize_business_codes(current, current)
        self.assertEqual(result.sheets[0].rows[0].business_code, "CT-MCL-001")

    def test_model_can_identify_original_column_without_fixed_header(self):
        current = document()
        current.sheets[0].columns.append(GenerationColumn(column_id="own", name="登记号", is_business_code=True))
        for index, row in enumerate(current.sheets[0].rows, 1):
            row.cells["own"] = f"合同-{index:03d}"
        result = normalize_business_codes(current, current)
        self.assertEqual(result.sheets[0].rows[0].business_code, "合同-001")
        workbook = GenerationExportService(None)._build_workbook(
            result, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        try:
            headers = [cell.value for cell in workbook["合同业务"][1]]
            self.assertIn("登记号", headers)
            self.assertNotIn("编号", headers)
            self.assertNotIn("编制编号", headers)
        finally:
            workbook.close()

    def test_model_fills_only_missing_code_in_existing_column(self):
        current = document()
        current.sheets[0].columns.append(GenerationColumn(column_id="own", name="规则ID"))
        for row in current.sheets[0].rows:
            row.cells["own"] = row.business_code
        current.sheets[0].rows[1].cells["own"] = ""
        current.sheets[0].rows[1].business_code = "CT-CALC-023"
        result = normalize_business_codes(current)
        self.assertEqual(result.sheets[0].rows[1].cells["own"], "CT-CALC-023")
        self.assertEqual(result.sheets[0].rows[0].cells["own"], "MAT-001")
        self.assertEqual(len(result.sheets[0].columns), 3)

    def test_missing_code_is_reported_instead_of_program_generating_one(self):
        current = document()
        current.sheets[0].rows[1].business_code = ""
        result = normalize_business_codes(current)
        self.assertEqual(result.sheets[0].rows[1].business_code, "")
        problems = collect_document_problems(result, {("原始规则", 2)}, require_codes=True)
        self.assertTrue(any(item["kind"] == "row.business_code_missing" for item in problems))

    def test_model_code_stays_stable_when_reordered_and_inserted(self):
        before = document()
        after = before.model_copy(deep=True)
        after.sheets[0].rows.reverse()
        after.sheets[0].rows.insert(0, GenerationEntry(
            row_id="r2", business_code="CT-CALC-023", entry_kind="RULE",
            cells={"name": "新增", "body": "新增要求"}, source=GenerationSource(kind="USER_INPUT"),
        ))
        result = normalize_business_codes(after, before)
        codes = {row.row_id: row.business_code for row in result.sheets[0].rows}
        self.assertEqual(codes["r1"], "RUL-001")
        self.assertEqual(codes["r2"], "CT-CALC-023")
        self.assertEqual(codes["m1"], "MAT-001")

    def test_custom_sequence_high_water_survives_deletion(self):
        before = document()
        before.sheets[0].rows[1].business_code = "CT-CALC-023"
        before = normalize_business_codes(before)
        after = before.model_copy(deep=True)
        after.sheets[0].rows = [row for row in after.sheets[0].rows if row.row_id != "r1"]
        result = normalize_business_codes(after, before)
        self.assertEqual(result.code_counters["ct-calc-"], 23)

    def test_duplicate_original_codes_are_not_hidden_by_extra_numbering(self):
        current = document()
        current.sheets[0].columns.append(GenerationColumn(column_id="own", name="规则ID"))
        for row in current.sheets[0].rows:
            row.cells["own"] = "CT-001"
        result = normalize_business_codes(current)
        self.assertTrue(all(row.business_code == "CT-001" for row in result.sheets[0].rows))
        problems = collect_document_problems(result, {("原始规则", 2)})
        self.assertTrue(any(item["kind"] == "row.duplicate_business_code" for item in problems))

    def test_assistant_can_supply_business_code_without_a_number_column(self):
        current = document()
        current.sheets[0].rows[1].business_code = ""
        result = apply_edits(current, edit({
            "op": "update_row_metadata", "row_id": "r1", "business_code": "CT-CALC-023",
        }))
        self.assertEqual(result.sheets[0].rows[1].business_code, "CT-CALC-023")
        self.assertEqual(result.sheets[0].rows[0], current.sheets[0].rows[0])


class EditBoundaryTests(unittest.TestCase):
    def test_cell_edit_changes_only_requested_text_and_edit_note(self):
        before = document()
        result = apply_edits(before, edit({"op": "update_cells", "row_id": "r1", "cells": {"name": "违约金计算要求"}}))
        expected = before.model_copy(deep=True)
        expected.sheets[0].rows[1].cells["name"] = "违约金计算要求"
        expected.sheets[0].rows[1].source.note = "AI按用户要求整理"
        self.assertEqual(result.model_dump(), expected.model_dump())
        self.assertEqual(before.sheets[0].rows[1].cells["name"], "违约金")
        changes = document_changes(before, result)
        self.assertEqual(changes, [
            {"path": ["sheets", {"sheet_id": "s1"}, "rows", {"row_id": "r1"}, "cells", "name"], "before": "违约金", "after": "违约金计算要求"},
            {"path": ["sheets", {"sheet_id": "s1"}, "rows", {"row_id": "r1"}, "source", "note"], "before": "", "after": "AI按用户要求整理"},
        ])

    def test_removal_diff_keeps_unchanged_rows_out_of_review(self):
        before = document()
        after = apply_edits(before, edit({"op": "remove_row", "row_id": "r1"}))
        changes = document_changes(before, after)
        removed = next(item for item in changes if item["path"][-1] == {"row_id": "r1"})
        self.assertIsNone(removed["after"])
        self.assertEqual(removed["before"]["cells"], before.sheets[0].rows[1].cells)
        self.assertFalse(any({"row_id": "d1"} in item["path"] for item in changes), "删除前一行不能制造后一行的正文变化")
        self.assertFalse(any(item["path"][-1] == "rows" for item in changes), "不能把整表重复交给复核")
        self.assertIn({"path": ["sheets", {"sheet_id": "s1"}, "rows", "$order"],
                       "before": ["m1", "r1", "d1"], "after": ["m1", "d1"]}, changes)

    def test_reorder_diff_contains_only_identity_order(self):
        before = document()
        after = apply_edits(before, edit({"op": "reorder", "sheet_id": "s1", "ids": ["d1", "r1", "m1"]}))
        self.assertEqual(document_changes(before, after), [{
            "path": ["sheets", {"sheet_id": "s1"}, "rows", "$order"],
            "before": ["m1", "r1", "d1"], "after": ["d1", "r1", "m1"],
        }])

    def test_review_repairs_cannot_modify_unaffected_content(self):
        before = document()
        changed = apply_edits(before, edit({"op": "set_document", "title": "新标题"}))
        for target_id in ("s1", "missing"):
            with self.subTest(target_id=target_id), self.assertRaises(BusinessError):
                apply_review_text_repairs(before, changed, [ReviewTextRepair(
                    target="sheet_description", target_id=target_id, text="不能顺带改写未涉及的表",
                )])
        self.assertEqual(changed.sheets, before.sheets)

    def test_cell_edit_rejects_unknown_column_and_row(self):
        for row_id, cells in (("r1", {"unknown": "新内容"}), ("missing", {"name": "新内容"})):
            with self.subTest(row_id=row_id, cells=cells), self.assertRaises(BusinessError):
                apply_edits(document(), edit({"op": "update_cells", "row_id": row_id, "cells": cells}))

    def test_metadata_edit_preserves_body_codes_and_source(self):
        before = document()
        result = apply_edits(before, edit({"op": "update_row_metadata", "row_id": "r1", "related_row_ids": ["m1"]}))
        row = result.sheets[0].rows[1]
        self.assertEqual(row.cells, before.sheets[0].rows[1].cells)
        self.assertEqual(row.business_code, before.sheets[0].rows[1].business_code)
        self.assertEqual(row.source.origin, before.sheets[0].rows[1].source.origin)
        self.assertEqual(row.entry_kind, "RULE")
        self.assertEqual(row.related_row_ids, ["m1"])
        self.assertFalse(before.sheets[0].rows[1].related_row_ids)
        cleared = apply_edits(result, edit({"op": "update_row_metadata", "row_id": "r1", "related_row_ids": []}))
        self.assertEqual(cleared.sheets[0].rows[1].related_row_ids, [])
        no_op = apply_edits(before, edit({"op": "update_row_metadata", "row_id": "r1"}))
        self.assertEqual(no_op, before)

    def test_metadata_rejects_unknown_row_and_validates_links(self):
        with self.assertRaises(BusinessError):
            apply_edits(document(), edit({"op": "update_row_metadata", "row_id": "missing", "entry_kind": "MATERIAL"}))
        result = apply_edits(document(), edit({"op": "update_row_metadata", "row_id": "r1", "related_row_ids": ["missing"]}))
        self.assertIn("row.related_missing_or_self", {item["kind"] for item in collect_document_problems(result, {("原始规则", 2)})})

    def test_compilation_note_can_be_revised_without_changing_provenance(self):
        before = document()
        before.sheets[0].rows[1].source.note = "正文缺算法，待确认"
        row = before.sheets[0].rows[1].model_dump(mode="json")
        row["source"] = {"kind": "USER_INPUT", "note": "原缺项已按用户补充解决"}
        for operation in (
            {"op": "update_row_metadata", "row_id": "r1", "source_note": "原缺项已按用户补充解决"},
            {"op": "upsert_row", "sheet_id": "s1", "row": row},
        ):
            with self.subTest(op=operation["op"]):
                after = apply_edits(before, edit(operation))
                source = after.sheets[0].rows[1].source
                self.assertEqual(source.kind, before.sheets[0].rows[1].source.kind)
                self.assertEqual(source.origin, before.sheets[0].rows[1].source.origin)
                self.assertEqual(source.requirement_ids, before.sheets[0].rows[1].source.requirement_ids)
                self.assertEqual(source.note, "原缺项已按用户补充解决；AI按用户要求整理")
                self.assertEqual(before.sheets[0].rows[1].source.note, "正文缺算法，待确认")
        row["source"] = {"kind": "USER_INPUT"}
        retained = apply_edits(before, edit({"op": "upsert_row", "sheet_id": "s1", "row": row}))
        self.assertEqual(retained.sheets[0].rows[1].source.note, "正文缺算法，待确认")

    def test_explanation_leaves_document_unchanged(self):
        before = document()
        result = apply_edits(before, AssistantReply(kind="EXPLAIN", message="算法未在原文定义"))
        self.assertEqual(result.model_dump(), before.model_dump())

    def test_explanation_cannot_silently_apply_edits(self):
        reply = AssistantReply.model_validate({"kind": "EXPLAIN", "message": "说明", "edits": [{"op": "set_document", "title": "新标题"}]})
        with self.assertRaises(BusinessError):
            apply_edits(document(), reply)

    def test_noop_update_does_not_invent_source_edit(self):
        before = document()
        row = before.sheets[0].rows[1].model_dump()
        row["business_code"] = ""
        result = apply_edits(before, edit({"op": "upsert_row", "sheet_id": "s1", "row": row}))
        self.assertEqual(result.model_dump(), before.model_dump())

    def test_one_row_update_preserves_unrelated_content_and_source(self):
        before = document()
        row = before.sheets[0].rows[1].model_dump()
        row["cells"]["name"] = "违约金计算要求"
        row["source"] = {"kind": "USER_INPUT"}
        result = apply_edits(before, edit({"op": "upsert_row", "sheet_id": "s1", "row": row}))
        self.assertEqual(result.sheets[0].rows[0], before.sheets[0].rows[0])
        actual = result.sheets[0].rows[1]
        self.assertEqual(actual.source.origin, before.sheets[0].rows[1].source.origin)
        self.assertEqual(actual.cells["body"], "应计算违约金，原文未给算法")
        self.assertEqual(actual.business_code, before.sheets[0].rows[1].business_code)

    def test_removal_cleans_relations_and_does_not_mutate_input(self):
        before = document()
        result = apply_edits(before, edit({"op": "remove_row", "row_id": "r1"}))
        self.assertNotIn("r1", result.sheets[0].rows[0].related_row_ids)
        self.assertIn("r1", before.sheets[0].rows[0].related_row_ids)

    def test_issue_scope_update_preserves_position_and_keeps_revised_evidence(self):
        before = document()
        from app.domain.rulebook_generation import GenerationIssue
        before.issues = [GenerationIssue(
            issue_id=f"UNC-00{index}", title="口径未明确", affected_row_ids=["r1"],
            source=GenerationSource(kind="ORIGINAL_EXCEL", origin={"sheet_name": "原始规则", "row_no": 1}),
        ) for index in (1, 2, 3)]
        issue = before.issues[1].model_dump(mode="json")
        issue.update(detail="已根据原文第二行明确范围", source={
            "kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 2},
        })
        after = apply_edits(before, edit({"op": "upsert_issue", "issue": issue}))
        self.assertEqual([item.issue_id for item in after.issues], ["UNC-001", "UNC-002", "UNC-003"])
        self.assertEqual(after.issues[1].source.origin.row_no, 2)
        self.assertEqual(after.issues[1].source.note, "AI按用户要求整理")
        self.assertEqual(before.issues[1].source.origin.row_no, 1)
        self.assertFalse(collect_document_problems(after, {("原始规则", 1), ("原始规则", 2)}))
        issue["source"]["origin"]["row_no"] = 999
        invalid = apply_edits(before, edit({"op": "upsert_issue", "issue": issue}))
        self.assertIn("source.origin_missing", {item["kind"] for item in collect_document_problems(invalid, {("原始规则", 2)})})

    def test_nonempty_sheet_cannot_be_dropped(self):
        with self.assertRaises(BusinessError):
            apply_edits(document(), edit({"op": "remove_sheet", "sheet_id": "s1"}))

    def test_bad_reorder_cannot_omit_content(self):
        with self.assertRaises(BusinessError):
            apply_edits(document(), edit({"op": "reorder", "sheet_id": "s1", "ids": ["r1", "m1"]}))

    def test_dangling_reference_and_fabricated_origin_are_reported(self):
        current = document()
        current.sheets[0].rows[0].related_row_ids.append("missing")
        problems = collect_document_problems(current, set())
        kinds = {item["kind"] for item in problems}
        self.assertIn("row.related_missing_or_self", kinds)
        self.assertIn("source.origin_missing", kinds)


class WorkbookTests(unittest.TestCase):
    def test_existing_material_table_is_reused_and_inverse_links_are_preserved(self):
        current = document()
        original = current.sheets[0]
        material = original.model_copy(deep=True)
        material.sheet_id, material.name, material.rows = "materials", "原材料清单", [original.rows[0]]
        original.rows = original.rows[1:]
        material.rows[0].related_row_ids = []
        original.rows[0].related_row_ids = ["m1"]
        current.sheets.insert(0, material)
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        try:
            self.assertNotIn("材料目录", workbook.sheetnames)
            self.assertIn("RUL-001", [cell.value for cell in workbook["原材料清单"][2]])
        finally:
            workbook.close()

    def test_existing_range_relations_avoid_duplicates_but_do_not_hide_missing_links(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                current = document()
                sheet = current.sheets[0]
                sheet.columns.append(GenerationColumn(column_id="links", name="关联业务规则编号"))
                for row in sheet.rows:
                    row.cells["links"] = ""
                second = sheet.rows[1].model_copy(deep=True)
                second.row_id, second.business_code = "r2", "RUL-002"
                sheet.rows.append(second)
                material = sheet.rows[0]
                material.related_row_ids = ["r1", "r2"]
                material.cells["links"] = "RUL-001～002"
                if missing:
                    third = second.model_copy(deep=True)
                    third.row_id, third.business_code = "r3", "RUL-003"
                    sheet.rows.append(third)
                    material.related_row_ids.append("r3")
                workbook = GenerationExportService(None)._build_workbook(
                    current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
                )
                try:
                    headers = [cell.value for cell in workbook["合同业务"][1]]
                    self.assertEqual("关联条目编号" in headers, missing)
                    self.assertEqual("材料目录" in workbook.sheetnames, missing)
                    values = [cell.value for cell in workbook["合同业务"][2]]
                    self.assertIn("RUL-001～002", values)
                    if missing:
                        self.assertEqual(values[headers.index("关联条目编号")], "RUL-003")
                finally:
                    workbook.close()

    def test_edit_tracking_note_does_not_add_an_export_column(self):
        from app.domain.rulebook_generation import GenerationIssue
        current = apply_edits(document(), edit({"op": "update_cells", "row_id": "r1", "cells": {"name": "违约金计算"}}))
        self.assertIn("AI按用户要求整理", current.sheets[0].rows[1].source.note)
        current.issues = [GenerationIssue(issue_id="UNC-001", title="算法口径", affected_row_ids=["r1"],
                          source=GenerationSource(kind="USER_INPUT", note="AI按用户要求整理"))]
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        try:
            self.assertNotIn("依据与编制说明", [cell.value for cell in workbook["合同业务"][1]])
            self.assertIn("违约金计算", [cell.value for row in workbook["合同业务"] for cell in row])
            self.assertFalse(any("AI按用户要求整理" in str(cell.value) for sheet in workbook for row in sheet for cell in row))
        finally:
            workbook.close()

    def test_catalog_label_uses_column_order_after_json_key_reordering(self):
        current = document()
        current.sheets[0].rows[0].cells = {"body": "适用说明", "name": "主合同"}
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        self.assertEqual(workbook["材料目录"].cell(2, 2).value, "主合同 · 适用说明")
        workbook.close()

    def test_export_uses_original_column_even_when_old_metadata_differs(self):
        current = document()
        current.sheets[0].columns.append(GenerationColumn(column_id="own", name="原编号"))
        for row in current.sheets[0].rows:
            row.cells["own"] = row.business_code
        for duplicate in (True, False):
            if not duplicate:
                current.sheets[0].rows[0].cells["own"] = "OLD-001"
            workbook = GenerationExportService(None)._build_workbook(
                current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
            )
            headers = [cell.value for cell in workbook["合同业务"][1]]
            self.assertNotIn("编制编号", headers)
            self.assertNotIn("编号", headers)
            self.assertEqual(workbook["合同业务"].cell(2, headers.index("原编号") + 1).value, current.sheets[0].rows[0].cells["own"])
            self.assertIn("原编号", headers)
            workbook.close()

    def test_generated_headings_do_not_duplicate_business_columns(self):
        current = document()
        current.sheets[0].columns[0].name = "编制编号"
        current.sheets[0].columns[1].name = "关联条目编号"
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        headers = [cell.value for cell in workbook["合同业务"][1]]
        self.assertEqual(len(headers), len(set(headers)))
        self.assertIn("编制编号", headers)
        self.assertIn("关联条目编号", headers)
        workbook.close()

    def test_catalog_uses_real_rule_and_reference_codes(self):
        current = document()
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        values = list(workbook["材料目录"].values)
        flattened = [str(value) for row in values for value in row if value is not None]
        self.assertIn("MAT-001", flattened)
        self.assertIn("RUL-001", flattened)
        self.assertIn("REF-001", flattened)
        self.assertFalse(any("row_id" in value for value in flattened))
        workbook.close()

    def test_formula_like_business_text_remains_literal(self):
        current = document()
        current.sheets[0].rows[1].cells["body"] = "=合同金额*违约天数"
        workbook = GenerationExportService(None)._build_workbook(
            current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        cells = [cell for row in workbook["合同业务"] for cell in row if cell.value == "=合同金额*违约天数"]
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].data_type, "s")
        workbook.close()

    def test_source_styles_columns_and_groups_survive_saved_export(self):
        current = document()
        current.sheets[0].rows = current.sheets[0].rows[1:2]
        row = current.sheets[0].rows[0]
        row.source.origin.row_no = 3
        current.sheets[0].source = GenerationSource.model_validate({
            "kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "原始规则", "row_no": 1},
        })
        current.sheets[0].columns[1].description = "这段栏目说明不应挤进表头"
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "来源.xlsx"
            original = Workbook()
            ws = original.active
            ws.title = "原始规则"
            ws.append(["名称", "业务内容"])
            ws.append(["主合同", None])
            ws.merge_cells("A2:B2")
            ws.append(["违约金", "原有内容"])
            ws.column_dimensions["A"].width = 23
            ws.column_dimensions["B"].width = 41
            ws.row_dimensions[3].height = 28
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = Font(name="宋体", size=10, bold=True)
                cell.fill = PatternFill("solid", fgColor="DDDDDD")
            ws["B3"].font = Font(name="宋体", size=10)
            ws["B3"].border = Border(bottom=Side(style="thin"))
            original.save(source)
            original.close()
            workbook = GenerationExportService(None)._build_workbook(
                current, revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
                source_path=source,
            )
            stream = io.BytesIO()
            workbook.save(stream)
            workbook.close()
            stream.seek(0)
            saved = load_workbook(stream)
            try:
                result = saved["合同业务"]
                # 新增编制编号后，原业务两列仍使用各自宽度和样式。
                self.assertEqual([cell.value for cell in result[1]][:3], ["编号", "名称", "业务内容"])
                self.assertEqual(result.column_dimensions["B"].width, 23)
                self.assertEqual(result.column_dimensions["C"].width, 41)
                self.assertEqual(result["C3"].value, row.cells["body"])
                self.assertEqual(result["C3"].font.name, "宋体")
                self.assertEqual(result["C3"].border.bottom.style, "thin")
                self.assertEqual(result["C3"].fill.patternType, None)
                self.assertEqual(result.row_dimensions[3].height, 28)
                self.assertEqual(result["C1"].fill.fgColor.rgb, "00DDDDDD")
                self.assertIn("A2:C2", set(map(str, result.merged_cells.ranges)))
                self.assertEqual(result["A2"].value, "主合同")
                self.assertEqual(result.freeze_panes, "A2")
                self.assertNotIn("使用说明", saved.sheetnames)
            finally:
                saved.close()

    def test_plain_export_adds_no_banner_or_fill(self):
        workbook = GenerationExportService(None)._build_workbook(
            document(), revision=1, source_filename="来源.xlsx", business_background="", generation_requirements="",
        )
        try:
            ws = workbook["合同业务"]
            self.assertEqual(ws["A1"].value, "编号")
            self.assertEqual(ws["A2"].value, "MAT-001")
            self.assertFalse(ws.merged_cells.ranges)
            self.assertTrue(all(cell.fill.patternType is None for row in ws for cell in row))
            self.assertNotIn("依据与编制说明", [cell.value for cell in ws[1]])
        finally:
            workbook.close()


class SourceCoverageTests(unittest.TestCase):
    def test_initial_content_review_repairs_unsupported_business_clause(self):
        valid = document().model_dump(mode="json")
        wrong = document().model_dump(mode="json")
        wrong["sheets"][0]["rows"][1]["cells"]["body"] = "违约金按合同金额的30%计算"
        inventory = {"schema_version": valid["schema_version"], "requirements": [], "unclear_items": []}
        service = GenerationLlmService()
        with patch.object(service, "_workbook_payload", return_value=([], {("原始规则", 2)})), patch(
            "app.modules.imports.generation.service.llm.call_json",
            side_effect=[inventory, wrong, {"problems": ["原文没有30%算法，保留真实要求与未明确口径"]}, valid, {"problems": []}],
        ) as model:
            saved = service.build_document(SimpleNamespace(sheets=[]), business_background="", generation_requirements="",
                                           operation_no="initial-review", job_no="test")
        self.assertEqual(saved["sheets"][0]["rows"][1]["cells"]["body"], "应计算违约金，原文未给算法")
        self.assertEqual(model.call_count, 5)
        self.assertEqual(model.call_args_list[3].args[0]["correction"]["problems"][0]["kind"], "content.review_failed")

    def test_initial_content_review_failure_never_returns_a_document(self):
        payload = document().model_dump(mode="json")
        inventory = {"schema_version": payload["schema_version"], "requirements": [], "unclear_items": []}
        service = GenerationLlmService()
        with patch.object(service, "_workbook_payload", return_value=([], {("原始规则", 2)})), patch(
            "app.modules.imports.generation.service.llm.call_json",
            side_effect=[inventory, payload, {"problems": ["独立条款遗漏"]}, payload, {"problems": ["独立条款仍遗漏"]}],
        ), self.assertRaises(BusinessError) as failure:
            service.build_document(SimpleNamespace(sheets=[]), business_background="", generation_requirements="",
                                   operation_no="initial-review-failure", job_no="test")
        self.assertEqual(failure.exception.code, "LLM_CONTENT_REVIEW_FAILED")
        self.assertIn("独立条款仍遗漏", str(failure.exception.details))

    def test_initial_workflow_rejects_omitted_numbered_reference_after_one_correction(self):
        ir = SimpleNamespace(sheets=[SimpleNamespace(name="字典", rows=[
            SimpleNamespace(row_no=1, cells={"A1": "字典 ID", "B1": "含义"}),
            SimpleNamespace(row_no=2, cells={"A2": "D-007", "B2": "原字典含义"}),
        ])])
        service = GenerationLlmService()
        response = {"schema_version": "rulebook_generation_v2", "requirements": [], "unclear_items": []}
        with patch.object(service, "_workbook_payload", return_value=([], {("字典", 1), ("字典", 2)})), patch(
            "app.modules.imports.generation.service.llm.call_json", side_effect=[response, response],
        ) as model, self.assertRaises(BusinessError) as failure:
            service.build_document(ir, business_background="", generation_requirements="", operation_no="test-coverage", job_no="test")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(failure.exception.code, "LLM_CONTRACT_INVALID")
        self.assertEqual(model.call_args_list[1].args[0]["correction"]["problems"][0]["kind"], "coverage.source_record_missing")

    def test_source_id_inventory_ignores_group_titles_and_relation_columns(self):
        ir = SimpleNamespace(sheets=[SimpleNamespace(name="任意业务表", rows=[
            SimpleNamespace(row_no=1, cells={"A1": "字典 ID", "B1": "关联规则ID"}),
            SimpleNamespace(row_no=2, cells={"A2": "D-007", "B2": "X-999"}),
            SimpleNamespace(row_no=3, cells={"A3": "下一组材料", "B3": None}),
            SimpleNamespace(row_no=4, cells={"A4": "-", "B4": "X-001"}),
        ])])
        self.assertEqual(GenerationLlmService()._source_records(ir), [
            {"sheet_name": "任意业务表", "row_no": 2, "codes": ["D-007"]},
        ])

    def test_reference_record_cannot_be_omitted_or_replaced_by_metadata(self):
        row = GenerationEntry(row_id="ref", entry_kind="REFERENCE", cells={"body": "原字典含义"}, source=GenerationSource.model_validate({
            "kind": "ORIGINAL_EXCEL", "origin": {"sheet_name": "字典", "row_no": 2}, "note": "D-007",
        }))
        records = [{"sheet_name": "字典", "row_no": 2, "codes": ["D-007"]}]
        self.assertEqual(source_record_coverage_problems([], records)[0]["kind"], "coverage.source_record_missing")
        self.assertEqual(source_record_coverage_problems([row], records, require_code=True)[0]["kind"], "coverage.original_code_missing")
        row.cells["body"] = "D-0070 原字典含义"
        self.assertTrue(source_record_coverage_problems([row], records, require_code=True), "编号子串不能代替原编号")
        row.cells["body"] = "D-007 原字典含义"
        self.assertEqual(source_record_coverage_problems([row], records, require_code=True), [])


if __name__ == "__main__":
    unittest.main()
