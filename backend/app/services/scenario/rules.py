"""场景草稿规则配置、引用检查与启停。"""

from typing import Any
from sqlalchemy.orm import Session
from ...repositories.shared import configuration as config_store
from ...common.exceptions import BusinessError
from ...entity import ScenarioDraft, ScenarioDraftRule
from ...domain.identity import stable_key
from ...repositories.scenario.draft import next_sort_no
from .common import _item_ref, _normalized_source_ref
from .draft import _locked_draft, _save_draft


def _normalize_rule_payload(db: Session, draft: ScenarioDraft, payload: dict[str, Any], *, existing_key: str | None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """原文与引用按管理员提交保存，不检查方法、口径、业务阶段或算子映射。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("RULE_NAME_REQUIRED", "规则名称不能为空", status_code=400)
    definition = {**(previous or {}), **payload, "name": name, "node": "RULE_EXECUTION", "node_type": "RULE_EXECUTION",
                  "operator": payload.get("operator") or (previous or {}).get("operator") or "LLM_EXECUTE",
                  "source_ref": _normalized_source_ref(payload.get("source_ref")), "enabled": bool(payload.get("enabled", True))}
    if payload.get("enabled") is True and definition.get("config_status") in {"PENDING", "DISABLED"}:
        definition["config_status"] = "CLEAR"
    return definition


def create_draft_rule(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增规则。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    definition = _normalize_rule_payload(db, draft, payload, existing_key=None)
    rule_key = stable_key("rule", scenario_code, _item_ref())
    config_store.add(
        db, ScenarioDraftRule(
            draft_id=draft.id,
            rule_key=rule_key,
            name=definition["name"],
            node_code=definition["node"],
            operator_code=definition["operator"],
            enabled=definition["enabled"],
            rule_json=definition,
            source_ref=definition.get("source_ref"),
            sort_no=next_sort_no(db, ScenarioDraftRule, draft.id),
        )
    )
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_CREATE",
        object_name=definition["name"],
        detail={"scenario_code": scenario_code, "rule_key": rule_key},
    )
    return draft


def _draft_rule_or_404(db: Session, draft_id: int, rule_key: str) -> ScenarioDraftRule:
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftRule, draft_id) if (item.rule_key == rule_key)]), None)
    if not item:
        raise BusinessError("DRAFT_RULE_NOT_FOUND", "规则配置不存在", status_code=404, details={"rule_key": rule_key})
    return item


def _downstream_rule_names(rules: list[ScenarioDraftRule], rule_key: str) -> list[str]:
    return [
        rule.name
        for rule in rules
        if rule.rule_key != rule_key and (
            (rule.rule_json or {}).get("source_rule_key") == rule_key
            or rule_key in ((rule.rule_json or {}).get("source_rule_keys") or [])
        )
    ]


def update_draft_rule(db: Session, scenario_code: str, rule_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """整体替换草稿中这条规则的定义；引用了它的下游规则名记录在操作明细里。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_rule_or_404(db, draft.id, rule_key)
    definition = _normalize_rule_payload(
        db,
        draft,
        payload,
        existing_key=rule_key,
        previous={**item.rule_json, "source_ref": item.source_ref} if isinstance(item.rule_json, dict) else None,
    )
    dependents = config_store.rows(db, ScenarioDraftRule, draft.id)
    downstream = _downstream_rule_names(dependents, rule_key)
    item.name = definition["name"]
    item.node_code = definition["node"]
    item.operator_code = definition["operator"]
    item.enabled = definition["enabled"]
    item.rule_json = definition
    item.source_ref = definition.get("source_ref")
    detail: dict[str, Any] = {"scenario_code": scenario_code, "rule_key": rule_key}
    if downstream:
        detail["downstream_rules"] = downstream
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_UPDATE",
        object_name=definition["name"],
        detail=detail,
    )
    return draft


def set_draft_rule_enabled(db: Session, scenario_code: str, rule_key: str, enabled: bool) -> ScenarioDraft:
    """启用 / 停用草稿中的规则。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_rule_or_404(db, draft.id, rule_key)
    item.enabled = enabled
    definition = dict(item.rule_json or {})
    definition["enabled"] = enabled
    definition["config_status"] = "CLEAR" if enabled else "DISABLED"
    item.rule_json = definition
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_ENABLE" if enabled else "DRAFT_RULE_DISABLE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "rule_key": rule_key},
    )
    return draft


def delete_draft_rule(db: Session, scenario_code: str, rule_key: str) -> ScenarioDraft:
    """删除草稿中的规则。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_rule_or_404(db, draft.id, rule_key)
    dependents = config_store.rows(db, ScenarioDraftRule, draft.id)
    downstream = _downstream_rule_names(dependents, rule_key)
    if downstream:
        raise BusinessError(
            "RULE_REFERENCED_BY_RISK",
            "该规则仍是以下风险规则的来源规则，请先处理后再删除",
            status_code=409,
            details={"rule_name": item.name, "rule_keys": downstream},
        )
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "rule_key": rule_key},
    )
    return draft
