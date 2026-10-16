from typing import Any, Literal

from pydantic import BaseModel


class WorkbookRow(BaseModel):
    row_no: int
    cells: dict[str, str | int | float | bool | None]


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

    def snapshot(self) -> dict[str, Any]:
        """页面要读的那部分：表级摘要加有上限的预览行。

        完整 rows 只用于这一次的模型转换，不落库——落库后既没有读取方，
        又和原始 Excel 文件重复。
        """
        return {
            "state": self.state,
            "max_row": self.max_row,
            "max_column": self.max_column,
            "non_empty_rows": self.non_empty_rows,
            "used_range": self.used_range,
            "merged_ranges": self.merged_ranges,
            "header_candidates": self.header_candidates,
            "rows_truncated": self.rows_truncated,
            "columns_truncated": self.columns_truncated,
            "cells_truncated": self.cells_truncated,
            "preview_rows": [row.model_dump(mode="json") for row in self.preview_rows],
        }


class WorkbookIR(BaseModel):
    workbook_id: str
    source_filename: str
    source_size: int
    source_sha256: str
    # 内部执行契约版本身份，不约束上传 Excel 的布局。
    profile_version: str
    # 上传入口的场景身份（用户先建场景、再在该场景下上传）。它不由表头推断：
    # 字段及规则使用该场景的实际节点；历史作业没有这一项时按未记录场景处理。
    scenario_code: str | None = None
    # 新导入记录场景协议身份；历史 IR 与草稿行转换的内部 IR 没有该身份。
    schema_key: str | None = None
    schema_version: str | None = None
    sheets: list[WorkbookSheet]
