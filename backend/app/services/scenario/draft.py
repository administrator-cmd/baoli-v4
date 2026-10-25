"""场景草稿的初始化、锁定、配置写入与事务提交。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...constants.execution import NODE_RULE_EXECUTION
from ...domain import field_strategy, scenario_schema
from ...domain import manifest as manifest_module
from ...domain.rule_library import active
from ...entity.scenario_drafts import (
    ScenarioDraft,
    ScenarioDraftField,
    ScenarioDraftMaterial,
    ScenarioDraftNode,
    ScenarioDraftRule,
    ScenarioDraftSection,
)
from ...entity.scenarios import ScenarioTemplate
from ..shared import configuration as config_store
from ..system.operation_log import record_operation
from ..system.operation_types import OBJECT_SCENARIO
from .lookup import find_latest_published_version, template_or_404


def draft_rows(
    db: Session, draft_id: int
) -> tuple[
    list[ScenarioDraftNode],
    list[ScenarioDraftMaterial],
    list[ScenarioDraftField],
    list[ScenarioDraftRule],
    list[ScenarioDraftSection],
]:
    """草稿的全部业务行：节点实例、材料、字段、规则、报告章节。

    技术容器只用于持久化关联；规则的显式引用决定执行顺序。
    """
    nodes = sorted(config_store.rows(db, ScenarioDraftNode, draft_id), key=lambda item: (item.sequence_no, item.id,))
    materials = sorted(config_store.rows(db, ScenarioDraftMaterial, draft_id), key=lambda item: (item.sort_no, item.id,))
    fields = sorted(config_store.rows(db, ScenarioDraftField, draft_id), key=lambda item: (item.sort_no, item.id,))
    rules = sorted(config_store.rows(db, ScenarioDraftRule, draft_id), key=lambda item: (item.sort_no, item.id,))
    sections = sorted(config_store.rows(db, ScenarioDraftSection, draft_id), key=lambda item: (item.sort_no, item.id,))
    return list(nodes), list(materials), list(fields), list(rules), list(sections)


def next_sort_no(db: Session, model: type, draft_id: int) -> int:
    return config_store.next_sort_no(db, model, draft_id)


def ensure_draft(db: Session, template: ScenarioTemplate) -> ScenarioDraft:
    """取得场景草稿；不存在时创建。

    场景已有已发布版本时，用最新快照初始化缺失的草稿（source=REVISION，
    base_version_no 记录来源版本）；
    没有已发布版本时创建空草稿（source=FORM）。
    """
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
    if draft is not None:
        return draft
    draft = ScenarioDraft(
        scenario_code=template.scenario_code,
        revision_no=1,
        source="FORM",
        base_version_no=None,
        schema_key=template.schema_key,
        schema_version=template.schema_version,
        schema_json=template.schema_json,
    )
    db.add(draft)
    db.flush()
    latest = find_latest_published_version(db, template.scenario_code)
    if latest is not None:
        draft.source = "REVISION"
        draft.base_version_no = latest.version_no
        _load_manifest_into_draft(db, draft, latest.manifest_json)
    else:
        draft.sync_error = None
    db.flush()
    return draft


def _load_manifest_into_draft(db: Session, draft: ScenarioDraft, manifest: dict[str, Any]) -> None:
    """用最新已发布版本的 manifest 初始化缺失的草稿。

    稳定键原样保留：方案对固定场景版本的规则选择依赖这些键。
    系统规则（字段抽取、报告生成）不进入草稿——它们在发布时按同一规则重新派生。

    业务配置事实必须完整带过来：产物来源、币种、报告分组属性、检查等级与风险成立条件
    都是人工维护的配置。少带一项，页面保存一次就会把它永久丢掉。
    """
    # 映射口径只有一份：domain.manifest.editable_config_of（方案复制走同一份）。
    config = manifest_module.editable_config_of(manifest, namespace=draft.scenario_code)
    # 协议版本随 manifest 一起带入（系统只有一份协议）。
    profile_version = manifest.get("profile_version")
    if isinstance(profile_version, str) and profile_version:
        draft.profile_version = profile_version
    schema = scenario_schema.normalize_schema(manifest.get("scenario_schema"))
    draft.schema_key = schema["schema_key"]
    draft.schema_version = schema["schema_version"]
    draft.schema_json = schema
    _write_config_into_draft(db, draft, config, replace_nodes=True)
    if "import_proposals" in manifest:
        draft.config_json = {**draft.config_json, "import_proposals": manifest["import_proposals"]}


def load_import_config_into_draft(
    db: Session, draft: ScenarioDraft, config: dict[str, Any], *, profile_version: str
) -> None:
    """导入替换业务配置；新规则库声明的抽取策略优先于旧配置。"""
    # 新导入直接替换；不为继承旧策略而检查旧字段编号。
    for item in config.get("fields") or []:
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=str(item.get("value_type") or ""),
            extraction_strategy=item.get("extraction_strategy"),
            structure_schema=item.get("structure_schema_json"),
            field_name=str(item.get("name") or item.get("source_ref") or "未命名字段"),
            enabled=active(item),
        )
        item["extraction_strategy"] = extraction_strategy
        item["structure_schema_json"] = structure_schema
    draft.profile_version = profile_version
    config["node_instances"] = [{"node_code": NODE_RULE_EXECUTION, "node_type": NODE_RULE_EXECUTION, "name": "执行生效规则", "sequence_no": 1, "depends_on": [], "config_json": {}, "enabled": True}]
    _write_config_into_draft(db, draft, config, replace_nodes=True)


def _write_config_into_draft(
    db: Session, draft: ScenarioDraft, config: dict[str, Any], *, replace_nodes: bool
) -> None:
    nodes, materials, fields, rules, sections = draft_rows(db, draft.id)
    old_rows = [*materials, *fields, *rules, *sections]
    if replace_nodes:
        old_rows.extend(nodes)
    for row in old_rows:
        config_store.delete(row)
    db.flush()
    # 以下每一段都带顺序：原样恢复用户配置的顺序，不重排。
    if replace_nodes:
        for index, node in enumerate(config["node_instances"], start=1):
            config_store.add_mapping(db, ScenarioDraftNode, draft.id, node, index)
    for index, material in enumerate(config["materials"], start=1):
        config_store.add_mapping(db, ScenarioDraftMaterial, draft.id, material, index)
    for index, field in enumerate(config["fields"], start=1):
        config_store.add_mapping(db, ScenarioDraftField, draft.id, field, index)
    # 系统规则（字段抽取、报告生成）不进草稿——它们在发布时按同一确定性规则重新派生。
    draft_rules = [item for item in config["rules"] if not item["is_system"] and not item["rule_json"].get("calculation_ref")]
    for index, rule in enumerate(draft_rules, start=1):
        config_store.add_mapping(db, ScenarioDraftRule, draft.id, {k: v for k, v in rule.items() if k != "is_system"}, index)
    for index, section in enumerate(config["sections"], start=1):
        config_store.add_mapping(db, ScenarioDraftSection, draft.id, section, index)
    draft.calculation_json = config["calculation_json"]
    db.flush()


def _bump_draft(draft: ScenarioDraft) -> None:
    draft.revision_no = (draft.revision_no or 0) + 1


def _default_draft_nodes(draft_id: int) -> list[ScenarioDraftNode]:
    """只建立执行记录所需的技术容器，不预置业务阶段。"""
    return [ScenarioDraftNode(draft_id=draft_id, node_code=NODE_RULE_EXECUTION, node_type=NODE_RULE_EXECUTION, name="执行生效规则", sequence_no=1, depends_on=[], enabled=True, sort_no=1)]


def _locked_draft(db: Session, scenario_code: str) -> tuple[ScenarioTemplate, ScenarioDraft]:
    template = template_or_404(db, scenario_code, lock=True)
    draft = ensure_draft(db, template)
    return template, draft


def _save_draft(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    *,
    operation_type: str,
    object_name: str,
    detail: dict[str, Any],
) -> ScenarioDraft:
    """记录配置修改并保存草稿；不自动生成场景版本。"""
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=template.scenario_code,
        object_name=object_name,
        operation_type=operation_type,
        detail=detail,
    )
    _bump_draft(draft)
    db.flush()
    _commit_config_change(db, template, draft)
    return draft


def _commit_config_change(db: Session, template: ScenarioTemplate, draft: ScenarioDraft) -> None:
    """提交草稿；完整编译只在展示派生项或实际发布时执行。"""
    draft.sync_error = None
    db.commit()
