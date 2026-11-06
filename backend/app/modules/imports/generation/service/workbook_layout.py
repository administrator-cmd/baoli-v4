"""沿用上传工作簿的表格样式；只映射格式，不从原件恢复已删除的业务正文。"""

import math
import re
import unicodedata
from copy import copy
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

from app.common.exceptions import BusinessError
from app.domain.rulebook_generation import GenerationSource, is_business_code_column


def _heading(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def _display_width(value: Any) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1 for char in str(value or ""))


def _copy_cell_style(source, target) -> None:
    # 两本工作簿的 style_id 不可直接通用，逐项复制以注册到目标工作簿。
    for name in ("font", "fill", "border", "alignment", "protection"):
        setattr(target, name, copy(getattr(source, name)))
    target.number_format = source.number_format


class GenerationWorkbookLayout:
    def __init__(self, source_path: Path | None = None) -> None:
        self.source = None
        if source_path is not None:
            try:
                self.source = load_workbook(source_path, data_only=False, keep_links=False)
            except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError) as exc:
                raise BusinessError("GENERATION_SOURCE_UNREADABLE", "上传的 Excel 无法读取，不能沿用原表格式", status_code=409) from exc

    def close(self) -> None:
        if self.source is not None:
            self.source.close()

    def prepare(self, workbook: Workbook) -> None:
        if self.source is not None:
            workbook.loaded_theme = self.source.loaded_theme

    def source_sheet(self, title: str, source: GenerationSource | None, row_sources: list[GenerationSource]):
        if self.source is None:
            return None
        if source and source.origin and source.origin.sheet_name in self.source.sheetnames:
            return self.source[source.origin.sheet_name]
        names = [item.origin.sheet_name for item in row_sources if item.origin and item.origin.sheet_name in self.source.sheetnames]
        if names:
            return self.source[max(dict.fromkeys(names), key=names.count)]
        return self.source[title] if title in self.source.sheetnames else None

    def add_sheet(self, workbook: Workbook, title: str, headers: list[str], rows: list[list[Any]], *,
                  write_value, source: GenerationSource | None = None,
                  row_sources: list[GenerationSource] | None = None, style_only: bool = False):
        row_sources = row_sources or []
        template = None if style_only else self.source_sheet(title, source, row_sources)
        # 新增目录和事项表只借用原表字体/边框/表头样式，不借用原表内容或合并结构。
        style_template = template or (self.source.worksheets[0] if self.source is not None else None)
        ws = workbook.create_sheet(title=title)
        header_row, columns = self._columns(template, headers)
        if style_template is not None:
            ws.sheet_format = copy(style_template.sheet_format)
            ws.sheet_properties = copy(style_template.sheet_properties)
            ws.sheet_view.showGridLines = style_template.sheet_view.showGridLines
            ws.sheet_view.zoomScale = style_template.sheet_view.zoomScale
            ws.page_setup = copy(style_template.page_setup)
            ws.page_margins = copy(style_template.page_margins)
            ws.print_options = copy(style_template.print_options)
        if template is not None:
            ws.freeze_panes = template.freeze_panes
            # 原文件自带的前言才保留，不插入系统标题或说明行。
            for row in (template.iter_rows(max_row=header_row - 1) if header_row > 1 else []):
                for cell in row:
                    target = ws.cell(cell.row, cell.column)
                    _copy_cell_style(cell, target)
                    write_value(target, cell.value)
            for merged in template.merged_cells.ranges:
                if merged.max_row < header_row:
                    ws.merge_cells(str(merged))

        style_header, _ = self._columns(style_template, headers)
        body_row = self._body_row(style_template, style_header)
        for index, heading in enumerate(headers, 1):
            original_column = columns.get(index)
            if style_template is not None:
                style_column = original_column or min(index, style_template.max_column)
                _copy_cell_style(style_template.cell(style_header, style_column), ws.cell(header_row, index))
            else:
                ws.cell(header_row, index).font = Font(name="宋体", size=10, bold=True)
            write_value(ws.cell(header_row, index), heading)
            dimension = ws.column_dimensions[get_column_letter(index)]
            if template is not None and original_column is not None:
                self._column_dimension(template, original_column, dimension)
            else:
                longest = max([_display_width(heading), *(_display_width(row[index - 1]) for row in rows)])
                dimension.width = min(48, max(18, longest + 2))
        if style_template is not None:
            ws.row_dimensions[header_row].height = style_template.row_dimensions[style_header].height

        groups = self._groups(template, header_row, row_sources)
        destination_row = header_row + 1
        for index, values in enumerate(rows):
            origin = row_sources[index].origin if index < len(row_sources) else None
            for group in groups.pop(index, []):
                for column in range(1, len(headers) + 1):
                    _copy_cell_style(template.cell(group.min_row, columns.get(column, group.min_col)), ws.cell(destination_row, column))
                write_value(ws.cell(destination_row, 1), template.cell(group.min_row, group.min_col).value)
                ws.row_dimensions[destination_row].height = template.row_dimensions[group.min_row].height
                ws.merge_cells(start_row=destination_row, start_column=1, end_row=destination_row, end_column=len(headers))
                destination_row += 1
            source_row = origin.row_no if template is not None and origin and origin.sheet_name == template.title else body_row
            if style_template is not None:
                ws.row_dimensions[destination_row].height = style_template.row_dimensions[source_row].height
            for column, value in enumerate(values, 1):
                target = ws.cell(destination_row, column)
                original_column = columns.get(column)
                if style_template is not None:
                    style_column = original_column or min(column, style_template.max_column)
                    _copy_cell_style(style_template.cell(source_row, style_column), target)
                else:
                    target.font = Font(name="宋体", size=10)
                    target.alignment = Alignment(vertical="top", wrap_text=True)
                write_value(target, value)
                if template is None:
                    alignment = copy(target.alignment)
                    alignment.wrap_text = True
                    alignment.vertical = "top"
                    target.alignment = alignment
            if template is None:
                # 新增目录/事项没有原行高可沿用，只按真实文本展开，避免长关联和问题被遮住。
                lines = max((sum(max(1, math.ceil(_display_width(line) / max(ws.column_dimensions[get_column_letter(column)].width - 2, 1)))
                                 for line in str(value or "").splitlines() or [""])
                             for column, value in enumerate(values, 1)), default=1)
                font_size = ws.cell(destination_row, 1).font.sz or 10
                ws.row_dimensions[destination_row].height = min(409, max(15, lines * (font_size + 3) + 2))
            destination_row += 1
        if template is not None:
            if template.auto_filter.ref:
                ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{max(header_row, destination_row - 1)}"
            ws.print_title_rows = template.print_title_rows
            if template.print_area:
                ws.print_area = f"A1:{get_column_letter(len(headers))}{destination_row - 1}"
        return ws

    def _columns(self, template, headers: list[str]) -> tuple[int, dict[int, int]]:
        if template is None:
            return 1, {}
        normalized = [_heading(header) for header in headers]
        candidates = range(1, min(template.max_row, 20) + 1)
        header_row = max(candidates, key=lambda row: (
            sum(_heading(cell.value) in normalized for cell in template[row] if cell.value),
            -row,
        ))
        available = {cell.column: _heading(cell.value) for cell in template[header_row] if cell.value}
        mapped = {}
        # 精确名称优先，新增编号列不得挤占真实业务栏目的格式。
        for target, header in enumerate(headers, 1):
            matching = [column for column, value in available.items() if value == _heading(header)]
            if len(matching) == 1:
                mapped[target] = matching[0]
                available.pop(matching[0])
        for target, header in enumerate(headers, 1):
            if target in mapped:
                continue
            own_code = [column for column, value in available.items() if is_business_code_column(header) and is_business_code_column(value)]
            scores = sorted(((SequenceMatcher(None, _heading(header), value).ratio(), column) for column, value in available.items()), reverse=True)
            column = own_code[0] if len(own_code) == 1 else (
                scores[0][1] if scores and scores[0][0] >= .75 and (len(scores) == 1 or scores[0][0] > scores[1][0]) else None
            )
            if column is not None:
                mapped[target] = column
                available.pop(column)
        return header_row, mapped

    def _body_row(self, template, header_row: int) -> int:
        if template is None:
            return 2
        group_rows = {merged.min_row for merged in template.merged_cells.ranges if merged.min_row == merged.max_row}
        return next((row for row in range(header_row + 1, template.max_row + 1)
                     if row not in group_rows and any(cell.value is not None for cell in template[row])), header_row + 1)

    def _column_dimension(self, template, column: int, target) -> None:
        # 列维度可能以 A:C 形式分组，不能只按单列键读取。
        source = next((item for item in template.column_dimensions.values() if item.min <= column <= item.max), None)
        if source is not None:
            for name in ("width", "hidden", "outlineLevel", "collapsed", "bestFit"):
                setattr(target, name, getattr(source, name))
        else:
            target.width = template.sheet_format.defaultColWidth or 13

    def _groups(self, template, header_row: int, sources: list[GenerationSource]) -> dict[int, list]:
        if template is None:
            return {}
        positions = [(index, item.origin.row_no) for index, item in enumerate(sources)
                     if item.origin and item.origin.sheet_name == template.title]
        # 用户重新排序后不能再套原分组，避免把条目放进错误材料组。
        if [row for _, row in positions] != sorted({row for _, row in positions}):
            return {}
        result = {}
        source_columns = [cell.column for cell in template[header_row] if cell.value is not None]
        last_column = max(source_columns, default=1)
        groups = sorted((merged for merged in template.merged_cells.ranges
                         if merged.min_row == merged.max_row and merged.min_row > header_row
                         and merged.min_col == 1 and merged.max_col >= last_column), key=lambda item: item.min_row)
        for index, merged in enumerate(groups):
            following = next(((index, row) for index, row in positions if row > merged.min_row), None)
            next_group = groups[index + 1].min_row if index + 1 < len(groups) else template.max_row + 1
            if following and following[1] < next_group:
                result.setdefault(following[0], []).append(merged)
        return result
