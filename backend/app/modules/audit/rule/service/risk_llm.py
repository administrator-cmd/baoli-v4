"""共用风险判断：业务条件来自场景，来源结果由运行期提供。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import RiskStatus
from app.integrations.llm.gateway import call_json
from app.modules.audit.field.service.prompts import RISK_PROMPT


class RiskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: RiskStatus
    message: str = Field(min_length=1)
    evidence_source_indices: list[int]


class AuditRuleRiskLlmService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def evaluate_risk(self, parameters: dict[str, Any]) -> dict[str, Any]:
        condition = parameters.get("risk_condition")
        sources = parameters.get("source_results", [])
        response_contract = RiskResponse.model_json_schema()
        if sources:
            response_contract["properties"]["evidence_source_indices"]["items"].update(minimum=0, maximum=len(sources) - 1)
        else:
            response_contract["properties"]["evidence_source_indices"]["maxItems"] = 0
        try:
            response = RiskResponse.model_validate(call_json({
                "risk_condition": condition,
                "requirement": parameters.get("requirement"),
                "sources": sources,
                "response_contract": response_contract,
            }, RISK_PROMPT))
        except ValidationError as exc:
            raise BusinessError("LLM_RISK_RESPONSE_INVALID", "大模型风险判断不符合结果契约",
                                status_code=502, details={"reason": str(exc)}) from exc
        if any(index < 0 or index >= len(sources) for index in response.evidence_source_indices):
            raise BusinessError("LLM_RISK_REFERENCE_INVALID", "大模型引用了本次未提供的风险来源", status_code=502)
        return {
            **response.model_dump(mode="json", exclude={"evidence_source_indices"}),
            "risk_level": parameters.get("risk_level"), "action": parameters.get("action"),
            "evidence_sources": [
                {key: sources[index].get(key) for key in ("node_code", "rule_key", "rule_result_id", "status")}
                for index in response.evidence_source_indices
            ],
        }
