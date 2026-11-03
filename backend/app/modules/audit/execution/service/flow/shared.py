"""规则实例定位、Agent 延迟加载与模型工具定义。"""

from typing import Any

from langchain_core.utils.function_calling import convert_to_openai_tool

from app.common.exceptions import BusinessError


def agent_module() -> Any:
    """延迟导入 Agent 实现。

    `flow` 位于 audit 包内，`agent` 位于其父包，但 agent 依赖 integrations.llm 与
    services.audit.tools.runtime；模块级导入会让「只加载 flow」也拉起模型网关。
    延迟导入保持 import 图干净（同手法见审核与导入模块的 operations）。
    """
    from app.modules.audit.rule.service import agent

    return agent


def openai_tools(allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """根据本次循环的实际工具列表构造模型参数。"""
    definitions = agent_module().AuditRuleAgentService().tool_definitions(allowed_tools)
    return [
        convert_to_openai_tool(definition)
        for definition in sorted(definitions, key=lambda item: str(item.get("name")))
    ]


def rule_of_instance(manifest: dict[str, Any], node_code: str, instance: dict[str, Any]) -> dict[str, Any]:
    """按规则实例取出本节点的规则定义（已发布配置，冻结不变）。"""
    rule_key = instance.get("rule_key")
    for rule in manifest["rules"]:
        if rule.get("node") == node_code and rule.get("rule_key") == rule_key:
            return rule
    raise BusinessError(
        "AGENT_RULE_MISSING",
        "规则评估关联的规则不在任务的已发布配置中，本次未继续",
        status_code=500,
        details={"node_code": node_code, "rule_key": rule_key},
    )
