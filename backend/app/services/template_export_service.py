"""将已发布场景的完整业务 JSON 导出为 Excel，不依赖固定规则库模板。"""

import json
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..domain.manifest import is_system_rule

HEADER_FILL = PatternFill("solid", fgColor="174A5B")
EXCEL_CELL_TEXT_LIMIT = 32767


def _rule_export_status(rule: dict[str, Any]) -> Any:
    """导出当前启停事实；历史解析状态不能覆盖页面保存的 enabled。"""
    if not rule.get("enabled", True):
        return "DISABLED"
    status = rule.get("config_status")
    return "CLEAR" if status == "DISABLED" else status


def _build_dynamic_export(manifest: dict[str, Any]) -> bytes:
    """自由规则库导出业务配置及停用原文，不要求业务编号或固定 Sheet 角色。"""
    workbook = Workbook()
    workbook.remove(workbook.active)
    collections = (
        ("材料定义", manifest["materials"], "name"),
        ("字段定义", manifest["fields"], "name"),
        ("审核规则", [{**item, "config_status": _rule_export_status(item)}
                  for item in manifest["rules"] if not is_system_rule(item)], "name"),
        ("计算定义", manifest.get("calculations") or [], "rule_name"),
        ("报告章节", manifest.get("report_template") or [], "section_name"),
        ("流程节点", manifest["nodes"], "name"),
        ("停用来源", manifest.get("disabled_source_rows") or [], "source_id"),
    )
    for title, rows, name_key in collections:
        sheet = workbook.create_sheet(title)
        content = []
        for row in rows:
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            parts = [encoded[offset:offset + EXCEL_CELL_TEXT_LIMIT]
                     for offset in range(0, len(encoded), EXCEL_CELL_TEXT_LIMIT)]
            content.append([row.get(name_key), *parts])
        column_count = max((len(row) for row in content), default=2)
        sheet.append(["业务名称", "完整配置 JSON", *[
            f"JSON 续段{index}" for index in range(1, column_count - 1)
        ]])
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 32
        for row in content:
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    # 业务名称和 JSON 片段是原文，前导等号或错误码不能改变单元格类型。
                    cell.data_type = "s"
        for cell in sheet[1]:
            if cell.column > 1:
                sheet.column_dimensions[cell.column_letter].width = 100
            cell.fill = HEADER_FILL
            cell.font = Font(color="FFFFFF", bold=True)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def build_scenario_version_export(manifest: dict[str, Any]) -> bytes:
    """所有场景统一导出业务 JSON 与停用来源，长 JSON 按 Excel 单元格容量分段。"""
    return _build_dynamic_export(manifest)
