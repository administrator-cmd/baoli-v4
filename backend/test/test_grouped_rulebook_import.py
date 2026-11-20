"""导入边界回归：真实 Excel 读取和最终契约，模型及存储与业务库隔离。"""

import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.modules.imports.job.service import grouped_import
from app.modules.imports.job.service.llm import ImportsJobLlmService
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService


class GroupedImportTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory()))
        self.path = root / "job" / "source.xlsx"
        self.path.parent.mkdir()
        wb = Workbook()
        ws = wb.active
        ws.title = "材料"
        ws.append(["编号", "材料名"])
        ws.append(["M1", "主合同"])
        ws.append(["M2", "补充协议"])
        ws = wb.create_sheet("抽取")
        ws.append(["编号", "字段名", "来源"])
        ws.append(["F1", "金额", "主合同"])
        ws.append(["F2", "金额", "补充协议"])
        wb.save(self.path)
        wb.close()
        reader = ImportsJobWorkbookService()
        sha = reader.file_sha256(self.path)
        self.ir = reader.read_workbook_ir(
            self.path,
            workbook_id="x",
            source_filename="source.xlsx",
            source_size=self.path.stat().st_size,
            source_sha256=sha,
            profile_version="1",
        )
        settings = get_settings()
        stack.enter_context(patch.object(settings, "data_dir", root))
        stack.enter_context(patch.object(settings, "rulebook_grouped_import", True))
        stack.enter_context(patch.object(settings, "rulebook_group_objects", 2))
        self.service = ImportsJobLlmService()
        self.fp = self.service.initial_fingerprint("1", "", "", sha, None)
        self.calls = []
        self.fail = False
        self.split = False
        self.bad_binding = False
        self.lookup = False
        self.missing = False
        stack.enter_context(patch.object(grouped_import, "call_json", side_effect=self.answer))

    def answer(self, payload, prompt, **kwargs):
        owners = payload["owned"]
        self.calls.append(tuple(owners))
        if "catalog" not in payload:
            entries = [
                dict(group=g, name=n, source_id=i, row=r, links=links)
                for g, n, i, r, links in [
                    ("materials", "主合同", "M1", 2, []),
                    ("materials", "补充协议", "M2", 3, []),
                    ("fields", "金额", "F1", 5, ["M1"]),
                    ("fields", "金额", "F2", 6, ["M2"]),
                ]
            ]
            return {"entries": entries, "non_definition_rows": [1, 4]}
        if self.lookup and owners == ["obj_3"]:
            if "lookup_results" not in payload:
                self.assertNotIn("obj_4", [row[0] for row in payload["catalog"]])
                return {"need_names": ["金额"]}
            self.assertEqual(payload["lookup_results"]["金额"], ["obj_3", "obj_4"])
        result = {k: [] for k in payload["response_schema"]["required"]}
        if self.split and owners == ["obj_1", "obj_2"]:
            raise BusinessError("LLM_OUTPUT_TRUNCATED", "输出截断")
        if self.fail and owners[0] == "obj_3":
            raise BusinessError("LLM_RESULT_UNKNOWN", "结果未知")
        for ref, group, name, source, sheet, row, _ in payload["catalog"]:
            if ref not in owners:
                continue
            if self.missing and ref == "obj_4":
                continue
            item = dict(
                candidate_ref=ref,
                name=name,
                source_id=source,
                required=True,
                origin=dict(sheet_name=sheet, row_no=row),
                config_status="CLEAR",
                interpretation=dict(basis="原表列出", missing_details=[]),
            )
            if group == "fields":
                raw_rows = [r[2] for s in payload["workbook"] if s["sheet"] == "材料" for r in s["rows"]]
                self.assertTrue(any(("M1" if source == "F1" else "M2") in r.values() for r in raw_rows))
                item.update(
                    instruction="抽取金额",
                    value_type="AMOUNT",
                    material_ref=(
                        "invalid"
                        if self.bad_binding and "correction" not in payload
                        else ("obj_1" if source == "F1" else "obj_2")
                    ),
                )
            result[group].append(item)
        return result

    def convert(self, resume=False):
        return self.service.convert_workbook(
            self.ir,
            operation_no="test",
            job_no="job",
            source_path="job/source.xlsx",
            fingerprint=self.fp,
            mode="RESUME_REFINEMENT" if resume else "FULL",
        )

    def test_resume_after_truncation_and_channel_failure(self):
        self.split = self.fail = True
        with self.assertRaisesRegex(BusinessError, "结果未知"):
            self.convert()
        self.calls.clear()
        self.fail = False
        result = self.convert(True)
        self.assertEqual(self.calls, [("obj_3", "obj_4")])
        self.assertEqual(
            [(i["name"], i["material_ref"]) for i in result["fields"]], [("金额", "obj_1"), ("金额", "obj_2")]
        )
        self.assertEqual(result["fields"][1]["origin"]["rows"][0]["cells"]["C3"], "补充协议")

    def test_explicit_chapter_list_cannot_be_saved_as_one_section(self):
        from app.domain.imports.workbook_proposals import prepare_sheets
        from app.modules.imports.job.service.grouped_plan import IndexBlock, SourceBook

        book = SourceBook(prepare_sheets(self.ir))
        book.rows[2]["D2"] = "1. 财务分析2. 风险提示"
        entry = dict(group="report_sections", name="财务分析", row=2, section_cell="D2")
        block = IndexBlock(entries=[entry], non_definition_rows=[])
        with self.assertRaisesRegex(BusinessError, "遗漏原文明确列出的报告章节"):
            book.validate_index(block, [2], book.pack([2]))
        repaired = IndexBlock(entries=[entry, {**entry, "name": "风险提示"}], non_definition_rows=[])
        book.validate_index(repaired, [2], book.pack([2]))

    def test_lookup_includes_both_same_name_fields(self):
        self.lookup = True
        with patch.object(get_settings(), "rulebook_group_objects", 1):
            self.assertEqual(len(self.convert()["fields"]), 2)
        self.assertEqual(self.calls.count(("obj_3",)), 2)

    def test_small_book_requires_only_two_requests(self):
        with patch.object(get_settings(), "rulebook_group_objects", 8):
            result = self.convert()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(result["fields"]), 2)
        self.assertEqual(set(result), set(grouped_import.KEYS))

    def test_missing_field_blocks_assembly(self):
        self.missing = True
        with self.assertRaisesRegex(BusinessError, "分组校验失败"):
            self.convert()
        self.missing = False
        self.calls.clear()
        self.assertEqual(len(self.convert(True)["fields"]), 2)
        self.assertEqual(self.calls, [("obj_3", "obj_4")])

    def test_invalid_cross_group_reference_repairs_only_fields(self):
        self.bad_binding = True
        result = self.convert()
        self.assertEqual(self.calls.count(("obj_1", "obj_2")), 1)
        self.assertEqual(self.calls.count(("obj_3", "obj_4")), 2)
        self.assertEqual(result["fields"][0]["material_ref"], "obj_1")
