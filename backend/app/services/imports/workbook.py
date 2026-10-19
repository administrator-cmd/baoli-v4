"""规则库 Excel 读取与工作簿结构整理。"""

from pathlib import Path
from typing import Any
from zipfile import BadZipFile
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from ...config import get_settings
from ...domain.workbook import WorkbookIR, WorkbookRow, WorkbookSheet
from ...common.exceptions import BusinessError
from ...domain.identity import stable_key


def _cell_value(value: Any, max_chars: int | None = None) -> tuple[str | int | float | bool | None, bool]:
    if value is None or isinstance(value, (int, float, bool)):
        return value, False
    text = str(value)
    if max_chars is None:
        return text, False
    return (text[:max_chars], len(text) > max_chars)


def _header_candidates(sheet, max_columns: int) -> list[int]:
    candidates: list[tuple[int, int]] = []
    for row_no in range(1, min(sheet.max_row, 10) + 1):
        populated = sum(
            1
            for column_no in range(1, min(sheet.max_column, max_columns) + 1)
            if sheet.cell(row_no, column_no).value not in (None, "")
        )
        if populated:
            candidates.append((row_no, populated))
    if not candidates:
        return []
    highest = max(count for _, count in candidates)
    return [row_no for row_no, count in candidates if count == highest][:3]


def read_workbook_ir(
    path: Path,
    *,
    workbook_id: str,
    source_filename: str,
    source_size: int,
    source_sha256: str,
    profile_version: str,
    scenario_code: str | None = None,
    schema_key: str | None = None,
    schema_version: str | None = None,
) -> WorkbookIR:
    """读取工作簿 IR：完整原文用于转换，预览另保存截断标记。

    不判断场景：场景由上传入口决定（用户先建场景、再在该场景下上传）。
    版本身份由调用方提供；不按历史模板匹配表名、表头或角色。
    动态导入由 LLM 识别业务定义，字段和规则分别引用当前场景节点。
    """
    settings = get_settings()
    try:
        workbook = load_workbook(path, read_only=False, data_only=False, keep_vba=path.suffix.lower() == ".xlsm")
    except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError) as exc:
        raise BusinessError("WORKBOOK_DAMAGED", "Excel 文件损坏或无法读取") from exc
    if not workbook.sheetnames:
        raise BusinessError("WORKBOOK_NO_SHEET", "Excel 文件中没有工作表")

    sheets: list[WorkbookSheet] = []
    for sheet in workbook.worksheets:
        sheet_id = stable_key("sheet", source_sha256, sheet.title)
        sheet_key = stable_key("sheetkey", source_sha256, sheet.title)
        non_empty_rows = 0
        used_min_row: int | None = None
        used_max_row: int | None = None
        used_min_column: int | None = None
        used_max_column: int | None = None
        rows: list[WorkbookRow] = []
        preview_rows: list[WorkbookRow] = []
        for row_no in range(1, sheet.max_row + 1):
            cells: dict[str, str | int | float | bool | None] = {}
            for column_no in range(1, sheet.max_column + 1):
                cell = sheet.cell(row_no, column_no)
                if cell.value in (None, ""):
                    continue
                value, _ = _cell_value(cell.value)
                address = f"{get_column_letter(column_no)}{row_no}"
                cells[address] = value
                used_min_row = row_no if used_min_row is None else min(used_min_row, row_no)
                used_max_row = row_no if used_max_row is None else max(used_max_row, row_no)
                used_min_column = column_no if used_min_column is None else min(used_min_column, column_no)
                used_max_column = column_no if used_max_column is None else max(used_max_column, column_no)
            if cells:
                non_empty_rows += 1
                rows.append(WorkbookRow(row_no=row_no, cells=cells))
        cells_truncated = False
        preview_row_limit = min(sheet.max_row, settings.workbook_preview_rows)
        preview_column_limit = min(sheet.max_column, settings.workbook_preview_columns)
        for row_no in range(1, preview_row_limit + 1):
            cells: dict[str, str | int | float | bool | None] = {}
            for column_no in range(1, preview_column_limit + 1):
                cell = sheet.cell(row_no, column_no)
                if cell.value in (None, ""):
                    continue
                value, truncated = _cell_value(cell.value, settings.workbook_cell_chars)
                cells_truncated = cells_truncated or truncated
                address = f"{get_column_letter(column_no)}{row_no}"
                cells[address] = value
            if cells:
                preview_rows.append(WorkbookRow(row_no=row_no, cells=cells))
        header_candidates = _header_candidates(sheet, settings.workbook_preview_columns)
        used_range = (
            f"{get_column_letter(used_min_column)}{used_min_row}:{get_column_letter(used_max_column)}{used_max_row}"
            if None not in {used_min_row, used_max_row, used_min_column, used_max_column}
            else None
        )
        sheets.append(
            WorkbookSheet(
                sheet_id=sheet_id,
                sheet_key=sheet_key,
                name=sheet.title,
                original_name=sheet.title,
                visible=sheet.sheet_state == "visible",
                state="EMPTY" if non_empty_rows == 0 else "DATA",
                max_row=sheet.max_row,
                max_column=sheet.max_column,
                non_empty_rows=non_empty_rows,
                used_range=used_range,
                merged_ranges=[str(item) for item in sheet.merged_cells.ranges],
                header_candidates=header_candidates,
                rows=rows,
                preview_rows=preview_rows,
                rows_truncated=sheet.max_row > settings.workbook_preview_rows,
                columns_truncated=sheet.max_column > settings.workbook_preview_columns,
                cells_truncated=cells_truncated,
            )
        )
    workbook.close()
    return WorkbookIR(
        workbook_id=workbook_id,
        source_filename=source_filename,
        source_size=source_size,
        source_sha256=source_sha256,
        profile_version=profile_version,
        scenario_code=scenario_code,
        schema_key=schema_key,
        schema_version=schema_version,
        sheets=sheets,
    )
