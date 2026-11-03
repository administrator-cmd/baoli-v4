"""场景草稿与方案共用的原始计算定义操作，不访问数据库。"""

from copy import deepcopy
from typing import Any

from app.common.exceptions import BusinessError


def calculation_store(raw: Any) -> list[dict[str, Any]]:
    """读取原始计算定义；不转换旧执行器的编译格式。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        if any(not isinstance(item, dict) for item in raw):
            raise BusinessError("RULE_LIBRARY_CALCULATION_FORMAT_INVALID", "计算定义必须是对象列表")
        return raw
    raise BusinessError("RULE_LIBRARY_CALCULATION_FORMAT_INVALID", "计算配置须为规则库原始定义列表，不再转换旧编译格式")


def calculations_referencing_field(raw: Any, field_code: str) -> list[str]:
    """按原始计算定义的精确字段来源查引用。"""
    candidates = calculation_store(raw)
    return [
        item.get("rule_name") or item["rule_code"]
        for item in candidates
        if any(parameter.get("source_type") == "FIELD" and parameter.get("source_code") == field_code
               for parameter in item.get("parameters") or [])
        or field_code in (item.get("depends_on_field_codes") or [])
    ]


def remap_calculation_field_codes(raw: Any, code_changes: dict[str, str]) -> Any:
    """同一字段改编号时同步当前计算引用；一次映射支持批量交换编号。"""
    updated = deepcopy(raw)
    candidates = calculation_store(updated)
    for item in candidates:
        for parameter in item.get("parameters") or []:
            if parameter.get("source_type") == "FIELD" and parameter.get("source_code") in code_changes:
                parameter["source_code"] = code_changes[parameter["source_code"]]
        if "depends_on_field_codes" in item:
            item["depends_on_field_codes"] = [
                code_changes.get(code, code) for code in item["depends_on_field_codes"]
            ]
    return updated
