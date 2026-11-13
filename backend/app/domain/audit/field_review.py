"""人工核对等待点的执行记录契约。"""

from typing import Any

from app.common.exceptions import BusinessError


def reviewed_extraction_keys(result: dict[str, Any], *, waiting: bool = False) -> set[str]:
    record = result.get("field_review")
    if not waiting and (record is None or (isinstance(record, dict) and not record.get("closed_at"))):
        return set()
    if (not isinstance(record, dict) or "extraction_rule_keys" not in record
            or not isinstance(record["extraction_rule_keys"], list)
            or any(not isinstance(key, str) or not key for key in record["extraction_rule_keys"])):
        raise BusinessError(
            "AUDIT_EXTRACTION_RULE_LIST_MISSING", "执行记录缺少抽取规则清单，无法继续", status_code=409,
        )
    return set(record["extraction_rule_keys"])
