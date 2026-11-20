"""分组导入只安排原行和身份；分类、条件和绑定均由模型读取原文决定。"""

import json
import re
from collections import defaultdict
from typing import Literal

from openpyxl.utils.cell import range_boundaries
from pydantic import BaseModel, ConfigDict, Field

from app.common.exceptions import BusinessError
from app.domain.imports.workbook_proposals import PreparedSheet

Group = Literal["materials", "fields", "calculations", "rules", "report_sections", "supporting_content"]
GROUPS = ("materials", "fields", "calculations", "rules", "report_sections", "supporting_content")


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: Group
    name: str = Field(min_length=1)
    source_id: str | None = None
    row: int
    section_cell: str | None = None
    produces: str | None = None
    related: list[int] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class IndexBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[Entry]
    non_definition_rows: list[int]


def size(payload):
    return len(json.dumps(payload, ensure_ascii=False))


def invalid(message, **details):
    return BusinessError("LLM_CONTRACT_INVALID", message, status_code=502, details=details)


class SourceBook:
    def __init__(self, sheets: dict[str, PreparedSheet]):
        self.rows = {}
        self.context = {}
        self.origins = {}
        self.merges = defaultdict(list)
        for prepared in sheets.values():
            sheet = prepared.sheet
            ids = {}
            for row in sheet.rows:
                rid = len(self.rows) + 1
                ids[row.row_no] = rid
                self.origins[rid] = (sheet.name, row.row_no)
                self.rows[rid] = row.cells
            headers = {ids[n] for n in sheet.header_candidates if n in ids}
            heading = None
            for n, rid in ids.items():
                self.context[rid] = headers | ({heading} if heading else set())
                if len(self.rows[rid]) == 1:
                    heading = rid
            for region in sorted(sheet.merged_ranges):
                _, first, _, last = range_boundaries(region)
                members = [rid for n, rid in ids.items() if first <= n <= last]
                for rid in members:
                    self.merges[rid].append(region)
                    if first in ids:
                        self.context[rid].add(ids[first])

    def pack(self, selected):
        visible = set(selected)
        for rid in selected:
            visible.update(self.context[rid])
        sheets = {}
        bounds = defaultdict(set)
        for rid in sorted(visible):
            sheet, row = self.origins[rid]
            sheets.setdefault(sheet, []).append([rid, row, self.rows[rid]])
            bounds[sheet].update(self.merges[rid])
        return [{"sheet": k, "rows": v, "merges": sorted(bounds[k])} for k, v in sheets.items()]

    def validate_index(self, block, owners, packed):
        allowed = {r[0] for s in packed for r in s["rows"]}
        covered = set(block.non_definition_rows)
        defined = set()
        for item in block.entries:
            if item.row not in owners or not set(item.related) <= allowed:
                raise invalid("目录对象引用了本块以外的来源", name=item.name, row=item.row)
            if item.group == "calculations" and not (item.produces or "").strip():
                raise invalid(
                    "计算项必须指出原文要求产出的可复用结果；检查通过与否归入规则",
                    name=item.name,
                    row=item.row,
                )
            if item.group == "report_sections":
                cell = self.rows[item.row].get(item.section_cell)
                if not isinstance(cell, str) or item.name not in cell:
                    raise invalid("章节名称必须取自报告原行的章节单元格", row=item.row, name=item.name)
            defined.update({item.row, *item.related} & set(owners))
        # 只解析模型已识别的章节单元格。可机械核对的连续编号清单不能少报章节。
        section_cells = {(i.row, i.section_cell) for i in block.entries if i.group == "report_sections"}
        for row, addr in section_cells:
            text = self.rows[row][addr].strip()
            marks = list(re.finditer(r"(?<![0-9])([1-9][0-9]*)[.．、](?![0-9])\s*", text))
            if (
                len(marks) < 2
                or marks[0].start() != 0
                or [int(m[1]) for m in marks] != list(range(1, len(marks) + 1))
            ):
                continue
            titles = [
                text[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
                for i, m in enumerate(marks)
            ]
            names = {
                i.name
                for i in block.entries
                if (i.row, i.section_cell) == (row, addr) and i.group == "report_sections"
            }
            missing = [
                name
                for i, name in enumerate(titles)
                if name not in names
                and text[marks[i].start() : marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
                not in names
            ]
            if missing:
                raise invalid("目录遗漏原文明确列出的报告章节", row=row, cell=addr, missing=missing)
        if (covered | defined) != set(owners) or covered & {item.row for item in block.entries}:
            raise invalid(
                "目录未交代全部原行或误把定义行排除", missing=sorted(set(owners) - defined - covered)
            )


def directory(entries):
    """确定全局内部身份；同名、同编号的候选都保留给模型区分。"""
    labels = defaultdict(set)
    symbols = {}
    for i, item in enumerate(entries, 1):
        ref = f"obj_{i}"
        symbols[ref] = item
        for label in (item.name, item.source_id):
            if label:
                labels[label].add(ref)
    edges = defaultdict(set)
    for ref, item in symbols.items():
        for name in item.links:
            matches = set(labels.get(name, ()))
            # 短名称可能是材料原名的一部分，补送所有候选原行，不据此自动建立绑定。
            if not matches and len(name.strip()) >= 2:
                matches.update(r for r, candidate in symbols.items() if name in candidate.name)
            for other in matches:
                edges[ref].add(other)
                edges[other].add(ref)
    return symbols, edges


def section_schema(schema, groups):
    """保留选中根属性及其可达的定义，避免把其他五类完整 schema 反复发送。"""
    props = {key: value for key, value in schema["properties"].items() if key in groups}
    defs = {}

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                key = value["$ref"].rsplit("/", 1)[1]
                if key not in defs:
                    defs[key] = schema["$defs"][key]
                    visit(defs[key])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(props)
    return {"type": "object", "properties": props, "required": list(props), "$defs": defs}
