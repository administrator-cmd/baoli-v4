"""模型结果的结构入口；业务语义和引用真实性由后续校验负责。"""

import logging
import re
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from app.common.exceptions import BusinessError
from app.modules.audit.field.service.prompts import EXTRACTION_BATCH_SCHEMA, VERDICT_SCHEMA

logger = logging.getLogger(__name__)
_VERDICT = Draft202012Validator(VERDICT_SCHEMA)
_BATCH_FIELD = Draft202012Validator(EXTRACTION_BATCH_SCHEMA["properties"]["fields"]["items"])


def unwrap_result_json(content: str) -> str:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        logger.warning("模型结论使用 Markdown 代码块包裹，已剥离围栏后解析")
        return fenced.group(1).strip()
    return text


def validate_result_shape(value: Any, *, batch_field: bool = False) -> None:
    validator = _BATCH_FIELD if batch_field else _VERDICT
    try:
        validator.validate(value)
    except ValidationError as exc:
        raise BusinessError(
            "AGENT_VERDICT_INVALID", "候选结论不符合结果结构，本次未采信",
            details={"path": list(exc.absolute_path), "reason": exc.message},
        ) from exc
