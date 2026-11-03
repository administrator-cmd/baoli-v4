"""场景草稿规则配置、引用检查与启停。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import NODE_RULE_EXECUTION, OPERATOR_LLM_EXECUTE
from app.domain.identity import stable_key
from app.modules.scenario.draft.model import ScenarioDraft, ScenarioDraftRule
from app.modules.scenario.draft.service.draft import ScenarioDraftService
from app.modules.scenario.template.service.common import ScenarioTemplateCommonService
from app.services.shared import configuration as config_store


class ScenarioDraftRulesService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _normalize_rule_payload(self, draft: ScenarioDraft, payload: dict[str, Any], *, existing_key: str | None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """原文与引用按管理员提交保存，不检查方法、口径、业务阶段或算子映射。"""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise BusinessError("RULE_NAME_REQUIRED", "规则名称不能为空", status_code=400)
        definition = {**(previous or {}), **payload, "name": name, "node": NODE_RULE_EXECUTION, "node_type": NODE_RULE_EXECUTION,
                      "operator": payload.get("operator") or (previous or {}).get("operator") or OPERATOR_LLM_EXECUTE,
                      "source_ref": ScenarioTemplateCommonService()._normalized_source_ref(payload.get("source_ref")), "enabled": bool(payload.get("enabled", True))}
        if payload.get("enabled") is True and definition.get("config_status") in {"PENDING", "DISABLED"}:
            definition["config_status"] = "CLEAR"
        return definition


    def create_draft_rule(self, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
        """在草稿中新增规则。已保存到草稿，发布后才供新方案使用。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        definition = self._normalize_rule_payload( draft, payload, existing_key=None)
        rule_key = stable_key("rule", scenario_code, ScenarioTemplateCommonService()._item_ref())
        config_store.add(
            self.db, ScenarioDraftRule(
                draft_id=draft.id,
                rule_key=rule_key,
                name=definition["name"],
                node_code=definition["node"],
                operator_code=definition["operator"],
                enabled=definition["enabled"],
                rule_json=definition,
                source_ref=definition.get("source_ref"),
                sort_no=ScenarioDraftService(self.db).next_sort_no( ScenarioDraftRule, draft.id),
            )
        )
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_RULE_CREATE",
            object_name=definition["name"],
            detail={"scenario_code": scenario_code, "rule_key": rule_key},
        )
        return draft


    def _draft_rule_or_404(self, draft_id: int, rule_key: str) -> ScenarioDraftRule:
        item = next(iter([item for item in config_store.rows(self.db, ScenarioDraftRule, draft_id) if (item.rule_key == rule_key)]), None)
        if not item:
            raise BusinessError("DRAFT_RULE_NOT_FOUND", "规则配置不存在", status_code=404, details={"rule_key": rule_key})
        return item


    def _downstream_rule_names(self, rules: list[ScenarioDraftRule], rule_key: str) -> list[str]:
        return [
            rule.name
            for rule in rules
            if rule.rule_key != rule_key and (
                (rule.rule_json or {}).get("source_rule_key") == rule_key
                or rule_key in ((rule.rule_json or {}).get("source_rule_keys") or [])
            )
        ]


    def update_draft_rule(self, scenario_code: str, rule_key: str, payload: dict[str, Any]) -> ScenarioDraft:
        """整体替换草稿中这条规则的定义；引用了它的下游规则名记录在操作明细里。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = self._draft_rule_or_404( draft.id, rule_key)
        definition = self._normalize_rule_payload(
        
            draft,
            payload,
            existing_key=rule_key,
            previous={**item.rule_json, "source_ref": item.source_ref} if isinstance(item.rule_json, dict) else None,
        )
        dependents = config_store.rows(self.db, ScenarioDraftRule, draft.id)
        downstream = self._downstream_rule_names(dependents, rule_key)
        item.name = definition["name"]
        item.node_code = definition["node"]
        item.operator_code = definition["operator"]
        item.enabled = definition["enabled"]
        item.rule_json = definition
        item.source_ref = definition.get("source_ref")
        detail: dict[str, Any] = {"scenario_code": scenario_code, "rule_key": rule_key}
        if downstream:
            detail["downstream_rules"] = downstream
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_RULE_UPDATE",
            object_name=definition["name"],
            detail=detail,
        )
        return draft


    def set_draft_rule_enabled(self, scenario_code: str, rule_key: str, enabled: bool) -> ScenarioDraft:
        """启用 / 停用草稿中的规则。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = self._draft_rule_or_404( draft.id, rule_key)
        item.enabled = enabled
        definition = dict(item.rule_json or {})
        definition["enabled"] = enabled
        definition["config_status"] = "CLEAR" if enabled else "DISABLED"
        item.rule_json = definition
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_RULE_ENABLE" if enabled else "DRAFT_RULE_DISABLE",
            object_name=item.name,
            detail={"scenario_code": scenario_code, "rule_key": rule_key},
        )
        return draft


    def delete_draft_rule(self, scenario_code: str, rule_key: str) -> ScenarioDraft:
        """删除草稿中的规则。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = self._draft_rule_or_404( draft.id, rule_key)
        dependents = config_store.rows(self.db, ScenarioDraftRule, draft.id)
        downstream = self._downstream_rule_names(dependents, rule_key)
        if downstream:
            raise BusinessError(
                "RULE_REFERENCED_BY_RISK",
                "该规则仍是以下风险规则的来源规则，请先处理后再删除",
                status_code=409,
                details={"rule_name": item.name, "rule_keys": downstream},
            )
        config_store.delete(item)
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_RULE_DELETE",
            object_name=item.name,
            detail={"scenario_code": scenario_code, "rule_key": rule_key},
        )
        return draft
