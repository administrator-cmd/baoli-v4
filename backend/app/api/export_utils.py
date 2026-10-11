"""列表导出的公共实现。

导出与当前列表使用同一后端查询与筛选作用域：先按与页面一致的筛选条件取全量行，
再输出 UTF-8（带 BOM，Excel 直接可读）的 CSV。导出不设静默截断上限——
行数就是筛选结果的行数。
"""

import csv
import io
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi.responses import Response

# 导出时把常见状态码转成中文口径；未收录的取值原样输出，不猜测。
EXPORT_LABELS: dict[str, str] = {
    "READY": "待执行",
    "RUNNING": "执行中",
    "WAITING_REVIEW": "等待人工复核",
    "BLOCKED": "已阻断",
    "COMPLETED": "已完成",
    "FAILED": "执行失败",
    "PENDING": "待处理",
    "RESOLVED": "已复核",
    "INVALIDATED": "已失效",
    "SUCCEEDED": "已完成",
    "INTERRUPTED": "已中断",
    "ACCEPTED": "执行中",
    "SUPERSEDED": "已被替换",
    "REMOVED": "已移除",
    "UPLOADED": "已上传",
    "WAITING_UPLOAD": "等待上传",
    "PREPARED": "处理完成",
    "TYPE_REVIEW": "材料类型待复核",
    "TYPE_REJECTED": "材料类型不符",
    "PENDING_REVIEW": "待复核",
    "EVIDENCE_INSUFFICIENT": "证据不足",
    "NOT_APPLICABLE": "不适用",
    "NOT_EXECUTED": "未执行",
    "PASSED": "正常",
    "ABNORMAL": "异常",
    "VERIFIED": "已核对",
    "UNVERIFIED": "未核实",
    "UNLOCATABLE": "无法定位",
    "GENERATED": "已生成",
    "ARCHIVED": "已归档",
    "SCENARIO": "审核场景",
    "AUDIT_PLAN": "审核方案",
    "AUDIT_TASK": "审核任务",
    "IMPORT_JOB": "规则库导入",
    "AUDIT_REPORT": "审核报告",
}


def export_label(value: Any) -> Any:
    if isinstance(value, str):
        return EXPORT_LABELS.get(value, value)
    return value


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(export_label(value))


def csv_response(filename: str, headers: list[str], rows: list[list[Any]]) -> Response:
    buffer = io.StringIO()
    # UTF-8 BOM：Excel 直接打开 CSV 不乱码。
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([format_cell(value) for value in row])
    content = buffer.getvalue().encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# 导出取全量：与页面相同的筛选条件，不做静默截断。
EXPORT_PAGE_SIZE = 1_000_000
