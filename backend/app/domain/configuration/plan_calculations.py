"""方案计算项的启停选择，只解释保存的配置。"""

from typing import Any

from app.common.exceptions import BusinessError
from app.domain.configuration.calculation_config import calculation_store
from app.domain.configuration.rule_library import active


def plan_calculation_config(
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    candidates = calculation_store(context.get("calculation_json"))
    codes = [str(item.get("rule_code") or "") for item in candidates]
    if not all(codes) or len(set(codes)) != len(codes):
        raise BusinessError("PLAN_CALC_CODE_INVALID", "方案的计算规则编号缺失或重复，无法确定启停范围")
    saved = context.get("calculation_enabled")
    if saved is None:
        # 旧方案尚未保存独立启停选择时，沿用它复制自场景版本的明确状态。
        enabled = {str(item["rule_code"]): active(item) for item in candidates}
    elif isinstance(saved, dict) and set(saved) == set(codes) and all(type(value) is bool for value in saved.values()):
        enabled = {str(item["rule_code"]): active({**item, "enabled": saved[str(item["rule_code"])]}) for item in candidates}
    else:
        raise BusinessError("PLAN_CALC_SELECTION_INVALID", "方案的计算规则启停选择与规则集合不一致")
    return candidates, enabled


def selected_calculations(
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates, enabled = plan_calculation_config(context)
    return [
        {
            **item,
            "enabled": enabled[str(item["rule_code"])],
            "status": "CLEAR" if enabled[str(item["rule_code"])] else (
                "PENDING" if item.get("status") == "PENDING" else "DISABLED"
            ),
        }
        for item in candidates
    ]
