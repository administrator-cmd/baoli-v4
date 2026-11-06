"""解析任务冻结的报告风格；历史无效档案只影响风格，不阻断业务报告。"""

from typing import Any

from app.common.exceptions import BusinessError
from app.modules.audit.report_template.service.analysis import validate_style_profile


def resolve_report_generation(config: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    config = config or {}
    requested_mode = config.get("mode", "RULES")
    metadata = {
        "mode": requested_mode,
        "requested_mode": requested_mode,
        "template_no": config.get("template_no"),
        "template_name": config.get("template_name"),
    }
    if requested_mode != "TEMPLATE":
        return metadata, None
    try:
        profile = validate_style_profile(config.get("style_profile"))
    except BusinessError as exc:
        if exc.code != "REPORT_TEMPLATE_PROFILE_INVALID":
            raise
        metadata.update({
            "mode": "RULES",
            "fallback_code": exc.code,
            "fallback_reason": "模板档案无效，本次按规则生成",
        })
        return metadata, None
    return metadata, profile
