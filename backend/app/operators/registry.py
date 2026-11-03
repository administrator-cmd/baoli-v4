"""可被工具调用的算子实现；当前只保留风险推导。"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.common.exceptions import BusinessError


@tool("RISK_TRIGGER")
def risk_trigger(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """按当前场景的完整风险条件，由大模型判断真实来源结果是否构成风险。"""
    from app.modules.audit.rule.service.risk_llm import AuditRuleRiskLlmService

    return AuditRuleRiskLlmService().evaluate_risk(parameters)


REGISTRY: dict[str, BaseTool] = {handler.name: handler for handler in (risk_trigger,)}


def execute_operator(operator: str, parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """按算子码分发到对应 handler；算子不在注册表时明确报错。"""
    handler = REGISTRY.get(operator)
    if not handler:
        raise BusinessError("OPERATOR_NOT_EXECUTABLE", "当前节点没有可执行的已注册算子", details={"operator": operator})
    return handler.invoke({"parameters": parameters, "inputs": inputs})
