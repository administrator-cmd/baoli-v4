from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

from ..config import get_settings
from ..domain.workbook import WorkbookCell, WorkbookIR, WorkbookRow, WorkbookSheet
from ..errors import BusinessError
from ..domain.identity import stable_key
from ..domain import unified_profile


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


def _has_business_table_data(rows: list[WorkbookRow]) -> bool:
    """业务工作表需要有表头行，且至少有一行有数据。"""
    populated_columns = [len(row.cells) for row in rows]
    return sum(column_count >= 2 for column_count in populated_columns) >= 2


def _header_signature(sheets: list[WorkbookSheet]) -> dict[str, list[str]]:
    """工作表名 -> 候选表头行的真实单元格文本。

    两种列形态（编码版 / 中文业务版）由这些表头文本区分；读不到可区分表头时
    detect_profile_version 会明确报错，不按默认版本模糊猜测。
    """
    signature: dict[str, list[str]] = {}
    for sheet in sheets:
        if not sheet.header_candidates:
            continue
        header_row_no = sheet.header_candidates[0]
        header_row = next((row for row in sheet.rows if row.row_no == header_row_no), None)
        if header_row is None:
            continue
        signature[sheet.name] = [str(value).strip() for value in header_row.cells.values() if str(value).strip()]
    return signature


def read_workbook_ir(
    path: Path,
    *,
    workbook_id: str,
    source_filename: str,
    source_size: int,
    source_sha256: str,
) -> WorkbookIR:
    """读取工作簿 IR（工作表、表头候选与截断标记）。

    **不判断场景**：场景由上传入口决定（用户先建场景、再在该场景下上传）。
    这里只按统一模板协议给每张 Sheet 一个**绑定建议**——建议不写库，
    绑定结果必须由用户在绑定步骤确认（见 `import_sheet.bound_node_code`）。
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
            cell_details: list[WorkbookCell] = []
            for column_no in range(1, sheet.max_column + 1):
                cell = sheet.cell(row_no, column_no)
                if cell.value in (None, ""):
                    continue
                value, _ = _cell_value(cell.value)
                address = f"{get_column_letter(column_no)}{row_no}"
                raw_text = str(cell.value)
                truncated_text, truncated = _cell_value(cell.value, settings.workbook_cell_chars)
                cells[address] = value
                cell_details.append(
                    WorkbookCell(
                        address=address,
                        column_no=column_no,
                        raw_text=raw_text,
                        truncated_text=str(truncated_text),
                        truncated=truncated,
                    )
                )
                used_min_row = row_no if used_min_row is None else min(used_min_row, row_no)
                used_max_row = row_no if used_max_row is None else max(used_max_row, row_no)
                used_min_column = column_no if used_min_column is None else min(used_min_column, column_no)
                used_max_column = column_no if used_max_column is None else max(used_max_column, column_no)
            if cells:
                non_empty_rows += 1
                rows.append(WorkbookRow(row_no=row_no, cells=cells, cell_details=cell_details))
        cells_truncated = False
        preview_row_limit = min(sheet.max_row, settings.workbook_preview_rows)
        preview_column_limit = min(sheet.max_column, settings.workbook_preview_columns)
        for row_no in range(1, preview_row_limit + 1):
            cells: dict[str, str | int | float | bool | None] = {}
            cell_details: list[WorkbookCell] = []
            for column_no in range(1, preview_column_limit + 1):
                cell = sheet.cell(row_no, column_no)
                if cell.value in (None, ""):
                    continue
                value, truncated = _cell_value(cell.value, settings.workbook_cell_chars)
                cells_truncated = cells_truncated or truncated
                address = f"{get_column_letter(column_no)}{row_no}"
                cells[address] = value
                cell_details.append(
                    WorkbookCell(
                        address=address,
                        column_no=column_no,
                        raw_text=str(cell.value),
                        truncated_text=str(value),
                        truncated=truncated,
                    )
                )
            if cells:
                preview_rows.append(WorkbookRow(row_no=row_no, cells=cells, cell_details=cell_details))
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
                state="EMPTY" if non_empty_rows == 0 else ("DATA" if _has_business_table_data(rows) else "NO_BUSINESS_DATA"),
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
                # 工作表用途在识别出格式版本后再判定：别名和列协议都随版本不同。
                suggested_role=None,
            )
        )
    workbook.close()
    # 按统一模板协议给每张 Sheet 一个**建议**角色。建议只用于页面预选，
    # 不写库、不生成节点、不决定场景：绑定结果由用户确认后落库。
    profile = unified_profile.load_unified_profile()
    for sheet in sheets:
        header_row = sheet.header_candidates[0] if sheet.header_candidates else None
        header = next((row for row in sheet.rows if row.row_no == header_row), None) if header_row else None
        cells = {str(value).strip() for value in (header.cells.values() if header else []) if str(value).strip()}
        sheet.suggested_role = unified_profile.suggest_sheet_role(profile, sheet.name, cells)
    return WorkbookIR(
        workbook_id=workbook_id,
        source_filename=source_filename,
        source_size=source_size,
        source_sha256=source_sha256,
        profile_version=unified_profile.UNIFIED_PROFILE_VERSION,
        sheets=sheets,
    )
