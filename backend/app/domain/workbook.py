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
    # 统一模板协议的版本标识。系统只有一份协议，这里记录「按哪一版协议解释」。
    profile_version: str
    # 上传入口的**场景身份**（用户先建场景、再在该场景下上传）。它不由表头推断：
    # 猜错场景会拿另一套列协议去解释这份 Excel，报出一堆与真实问题无关的缺列错误，
    # 而用户手里的文件根本没变。历史作业没有这一项，读取方按「未记录场景」处理。
    scenario_code: str | None = None
    # 新导入记录场景协议身份；历史 IR 与草稿行转换的内部 IR 没有该身份。
    schema_key: str | None = None
    schema_version: str | None = None
    sheets: list[WorkbookSheet]


def workbook_source_index(ir: WorkbookIR) -> dict[str, dict[str, Any]]:
    """工作表 key → {名称, 行号→列集合, 行号→单元格}，供来源追溯按坐标还原原文。"""
    result: dict[str, dict[str, Any]] = {}
    for sheet in ir.sheets:
        rows = {row.row_no: set(row.cells) for row in sheet.rows}
        values = {row.row_no: row.cells for row in sheet.rows}
        result[sheet.sheet_key] = {"name": sheet.name, "rows": rows, "values": values}
    return result
