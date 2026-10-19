"""系统数据库状态与外部服务配置状态查询。"""

from typing import Any
from ...config import get_settings
from ...repositories.system import status as system_repository


def _configuration_status(required: dict[str, bool]) -> dict[str, Any]:
    missing = [name for name, configured in required.items() if not configured]
    if len(missing) == len(required):
        status = "UNCONFIGURED"
    elif missing:
        status = "CONFIGURATION_INCOMPLETE"
    else:
        status = "CONFIGURED_UNVERIFIED"
    return {"status": status, "missing": missing}


def status_view() -> dict[str, Any]:
    database = system_repository.database_status()
    settings = get_settings()
    return {
        "service": "RUNNING" if database == "CONNECTED" else "DEGRADED",
        "database": database,
        "external_services": {
            "大模型": _configuration_status(
                {
                    "BAOLI_LLM_BASEURL": bool(settings.llm_baseurl),
                    "BAOLI_LLM_APIKEY": bool(settings.llm_api_key_value),
                    "BAOLI_LLM_MODEL_NAME": bool(settings.llm_model_name),
                }
            ),
            "材料识别": _configuration_status({"BAOLI_SERVICE_8018_URL": bool(settings.service_8018_url)}),
            "文件解析": _configuration_status({"BAOLI_SERVICE_8010_URL": bool(settings.service_8010_url)}),
            "文字识别": _configuration_status(
                {
                    "BAOLI_OCR_SERVICE_URL": bool(settings.ocr_service_url),
                    "BAOLI_OCR_TOKEN": bool(settings.ocr_token_value),
                    "BAOLI_OCR_MODEL": bool(settings.ocr_model),
                }
            ),
            "印章识别": _configuration_status({"BAOLI_SEAL_SERVICE_URL": bool(settings.seal_service_url)}),
        },
    }
