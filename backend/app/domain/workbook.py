from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkbookCell(BaseModel):
    address: str
    column_no: int
    raw_text: str
    truncated_text: str
    truncated: bool


class WorkbookRow(BaseModel):
    row_no: int
    cells: dict[str, str | int | float | bool | None]
    cell_details: list[WorkbookCell]


class WorkbookSheet(BaseModel):
    sheet_id: str
    sheet_key: str
    name: str
    original_name: str
    visible: bool
    state: Literal["DATA", "EMPTY", "NO_BUSINESS_DATA"]
    max_row: int
    max_column: int
    non_empty_rows: int
    used_range: str | None
    merged_ranges: list[str]
    header_candidates: list[int]
    rows: list[WorkbookRow]
    preview_rows: list[WorkbookRow]
    rows_truncated: bool
    columns_truncated: bool
    cells_truncated: bool
    suggested_role: str | None = None


class WorkbookIR(BaseModel):
    workbook_id: str
    source_filename: str
    source_size: int
    source_sha256: str
    profile_version: str
    sheets: list[WorkbookSheet]


def workbook_source_index(ir: WorkbookIR) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sheet in ir.sheets:
        rows = {row.row_no: set(row.cells) for row in sheet.rows}
        values = {row.row_no: row.cells for row in sheet.rows}
        result[sheet.sheet_key] = {"name": sheet.name, "rows": rows, "values": values}
    return result
