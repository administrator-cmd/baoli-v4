"""审核场景的后台管理：基本资料、配置草稿与发布。

业务模型：场景身份 = scenario_template（名称/业务类型/说明/启停 + 版本计数器）；
配置草稿 = scenario_draft 及其材料/字段/规则/报告章节，每次写入递增 revision_no，发布必须
携带当前修订号；发布 = 用 compile_form_manifest 把草稿编译成与导入流程同构的 manifest，
形成不可覆盖的 scenario_version 并写入关系投影；已发布版本是不可变的执行快照，后续修改当前
草稿并发布新快照。规则库导入草稿的首次发布保留 import_job_id/source=IMPORT，后续修订才标为
REVISION；页面直接建立的版本不伪造 Excel 作业。

所有写操作都在同一事务内追加 operation_log；删除材料/字段前先返回引用清单，不自动换对象、
不级联删除规则。
"""

import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..repositories import configuration_store as config_store

from ..domain import (
    field_strategy,
    manifest as manifest_module,
    scenario_schema,
    node_profile,
)
from ..domain.audit_vocab import SCOPE_NOT_APPLICABLE
from ..domain.manifest import (
    FORM_VALUE_TYPES,
    compile_form_manifest,
    manifest_scope_hash,
)
from ..common.exceptions import SAFE_DETAIL_KEYS, BusinessError
from ..entity import (
    AuditPlan,
    ImportJob,
    ScenarioDraft,
    ScenarioDraftField,
    ScenarioDraftNode,
    ScenarioDraftSheet,
    ScenarioDraftMaterial,
    ScenarioDraftRule,
    ScenarioDraftSection,
    ScenarioMaterial,
    ScenarioNode,
    ScenarioRule,
    ScenarioTemplate,
    ScenarioVersion,
)
from ..domain.identity import public_no, stable_key
from .operation_log_service import record_operation
from .operation_types import OBJECT_SCENARIO
from .template_registry_service import allocate_version

logger = logging.getLogger(__name__)

SYSTEM_OPERATORS = manifest_module.SYSTEM_OPERATORS
# 草稿字段允许的数据类型：与 manifest 的表单契约共用同一份定义，
# 包含大写金额（DECIMAL_CN），不绑定 Excel 模板列。
DRAFT_VALUE_TYPES = FORM_VALUE_TYPES
# 字段的产物来源取值：当前只有印章识别一种。
FIELD_ARTIFACT_SOURCES = {"SEAL"}
# 字段的取数期间与报表口径：由字段自己声明，计算参数按这两项定位字段事实
# 不涉及这些口径的业务声明「不适用」，计算参数沿用来源字段的相同定位键。
DRAFT_SOURCE_LABELS = {"FORM": "页面编辑", "EXCEL": "规则库导入", "COPY": "复制场景", "REVISION": "版本修订"}
def template_or_404(
    db: Session,
    scenario_code: str,
    *,
    lock: bool = False,
) -> ScenarioTemplate:
    """按场景编码取场景；取不到即 404，写路径可先锁行。"""
    statement = select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code)
    if lock:
        statement = statement.with_for_update()
    template = db.scalar(statement)
    if not template:
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "审核场景不存在或已被删除",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    return template


def version_or_404(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion:
    """按场景编码与版本号取已发布版本；取不到即 404。"""
    version = db.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_code == scenario_code,
            ScenarioVersion.version_no == version_no,
        )
    )
    if not version:
        raise BusinessError(
            "SCENARIO_VERSION_NOT_FOUND",
            "场景版本不存在",
            status_code=404,
            details={"scenario_code": scenario_code, "version_no": version_no},
        )
    return version


# 草稿读写


def _latest_published_version(db: Session, scenario_code: str) -> ScenarioVersion | None:
    return db.scalar(
        select(ScenarioVersion)
        .where(ScenarioVersion.scenario_code == scenario_code, ScenarioVersion.status == "PUBLISHED")
        .order_by(ScenarioVersion.version_no.desc())
        .limit(1)
    )


def _draft_rows(
    db: Session, draft_id: int
) -> tuple[
    list[ScenarioDraftNode],
    list[ScenarioDraftSheet],
    list[ScenarioDraftMaterial],
    list[ScenarioDraftField],
    list[ScenarioDraftRule],
    list[ScenarioDraftSection],
]:
    """草稿的全部业务行：节点实例、选用的 Sheet、材料、字段、规则、报告章节。

    技术容器只用于持久化关联；规则的显式引用决定执行顺序。
    """
    sheets = sorted(config_store.rows(db, ScenarioDraftSheet, draft_id), key=lambda item: (item.sort_no, item.id,))
    nodes = sorted(config_store.rows(db, ScenarioDraftNode, draft_id), key=lambda item: (item.sequence_no, item.id,))
    materials = sorted(config_store.rows(db, ScenarioDraftMaterial, draft_id), key=lambda item: (item.sort_no, item.id,))
    fields = sorted(config_store.rows(db, ScenarioDraftField, draft_id), key=lambda item: (item.sort_no, item.id,))
    rules = sorted(config_store.rows(db, ScenarioDraftRule, draft_id), key=lambda item: (item.sort_no, item.id,))
    sections = sorted(config_store.rows(db, ScenarioDraftSection, draft_id), key=lambda item: (item.sort_no, item.id,))
    return list(nodes), list(sheets), list(materials), list(fields), list(rules), list(sections)


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
    latest = _latest_published_version(db, template.scenario_code)
    if latest is not None:
        draft.source = "REVISION"
        draft.base_version_no = latest.version_no
        _load_manifest_into_draft(db, draft, latest.manifest_json)
    else:
        # 没有可派生的已发布版本，这是一份空配置，必然不能发布。
        # 与 create_scenario 同一口径：配置视图里的「能不能发布」必须是真实状态。
        # 这里不产生版本，只刷新阻断原因。
        _refresh_publish_readiness(db, template, draft)
    db.flush()
    return draft


def _field_scope(payload: dict[str, Any], key: str) -> str:
    """口径取自规则库原文；未涉及时使用存储唯一键所需的非空标识。"""
    return str(payload.get(key) or "").strip() or SCOPE_NOT_APPLICABLE


def _item_ref() -> str:
    return public_no("ref")


def _node_type_label(node_type: str) -> str:
    """技术执行容器的展示名称。"""
    return node_profile.load_node_profile()["display_name"]


def _next_sort_no(db: Session, model: type, draft_id: int) -> int:
    return config_store.next_sort_no(db, model, draft_id)


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
    existing_fields = config_store.rows(db, ScenarioDraftField, draft.id)
    strategies: dict[str, tuple[str, dict[str, Any] | None]] = {}
    strategy_owners: dict[str, str] = {}
    for existing in existing_fields:
        if not existing.source_ref:
            continue
        source_ref = str(existing.source_ref)
        previous_owner = strategy_owners.get(source_ref)
        if previous_owner is not None:
            raise BusinessError(
                "SCENARIO_FIELD_SOURCE_REF_DUPLICATE",
                "场景草稿中存在重复字段编号，无法确定 Excel 再次导入时应保留哪一套抽取配置",
                status_code=409,
                details={
                    "source_ref": source_ref,
                    "field_keys": [previous_owner, existing.field_key],
                },
            )
        strategy_owners[source_ref] = existing.field_key
        strategies[source_ref] = (existing.extraction_strategy, existing.structure_schema_json)
    for item in config.get("fields") or []:
        preserved = strategies.get(str(item.get("source_ref") or ""))
        if preserved is not None and item.get("extraction_strategy") is None:
            item["extraction_strategy"], item["structure_schema_json"] = preserved
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=str(item.get("value_type") or ""),
            extraction_strategy=item.get("extraction_strategy"),
            structure_schema=item.get("structure_schema_json"),
            field_name=str(item.get("name") or item.get("source_ref") or "未命名字段"),
        )
        item["extraction_strategy"] = extraction_strategy
        item["structure_schema_json"] = structure_schema
    draft.profile_version = profile_version
    config["nodes"] = [{"node_code": "RULE_EXECUTION", "node_type": "RULE_EXECUTION", "name": "执行生效规则", "sequence_no": 1, "depends_on": [], "config_json": {}, "enabled": True}]
    _write_config_into_draft(db, draft, config, replace_nodes=True)


def _write_config_into_draft(
    db: Session, draft: ScenarioDraft, config: dict[str, Any], *, replace_nodes: bool
) -> None:
    nodes, sheets, materials, fields, rules, sections = _draft_rows(db, draft.id)
    old_rows = [*sheets, *materials, *fields, *rules, *sections]
    if replace_nodes:
        old_rows.extend(nodes)
    for row in old_rows:
        config_store.delete(row)
    db.flush()
    # 以下每一段都带顺序：原样恢复用户配置的顺序，不重排。
    if replace_nodes:
        for index, node in enumerate(config["nodes"], start=1):
            config_store.add_mapping(db, ScenarioDraftNode, draft.id, node, index)
    for index, sheet in enumerate(config["sheets"], start=1):
        config_store.add_mapping(db, ScenarioDraftSheet, draft.id, sheet, index)
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
    draft.disabled_source_rows = list(config.get("disabled_source_rows") or [])
    draft.calculation_json = config["calculation_json"]
    db.flush()


def _bump_draft(draft: ScenarioDraft) -> None:
    draft.revision_no = (draft.revision_no or 0) + 1


def draft_config_view(db: Session, template: ScenarioTemplate) -> dict[str, Any]:
    """草稿的业务视图；首次访问会建立草稿并落库（真实记录，刷新后一致）。"""
    draft = ensure_draft(db, template)
    # 首次访问创建草稿会落库：它是真实记录，刷新后仍然一致。
    db.commit()
    return _draft_config_payload(db, draft)


def _draft_config_payload(db: Session, draft: ScenarioDraft) -> dict[str, Any]:
    nodes, sheets, materials, fields, rules, sections = _draft_rows(db, draft.id)
    material_names = {item.material_key: item.name for item in materials}
    field_map = {item.field_key: item for item in fields}
    rule_names = {item.rule_key: item.name for item in rules}
    # 技术容器用于执行记录关联，不是用户配置的业务阶段。
    node_items = [
        {
            "node_code": item.node_code,
            "node_type": item.node_type,
            "node_type_label": _node_type_label(item.node_type),
            "name": item.name,
            "sequence_no": item.sequence_no,
            "depends_on": list(item.depends_on or []),
            "config_json": item.config_json,
            "enabled": item.enabled,
            "updated_at": item.updated_at,
        }
        for item in nodes
    ]
    material_items = [
        {
            "material_key": item.material_key,
            "name": item.name,
            "required": item.required,
            "description": item.description,
            "source_ref": item.source_ref,
            "origin": item.origin_json,
            "sort_no": item.sort_no,
            "updated_at": item.updated_at,
        }
        for item in materials
    ]
    field_items = [
        {
            "field_key": item.field_key,
            "material_key": item.material_key,
            "material_name": material_names.get(item.material_key),
            "name": item.name,
            "value_type": item.value_type,
            "node_code": getattr(item, "node_code", None),
            "extraction_strategy": item.extraction_strategy,
            "structure_schema": item.structure_schema_json,
            "unit": item.unit,
            # 币种与产物来源是金额口径与主体核对的配置事实，草稿视图必须一并返回，
            # 否则页面看不到、也改不了它们。
            "currency": item.currency,
            "artifact_source": item.artifact_source,
            "period_scope": item.period_scope,
            "statement_scope": item.statement_scope,
            "required": item.required,
            "instruction": item.instruction,
            "source_ref": item.source_ref,
            "origin": item.origin_json,
            "sort_no": item.sort_no,
            "updated_at": item.updated_at,
        }
        for item in fields
    ]
    rule_map = {rule.rule_key: rule for rule in rules}
    rule_items = [_draft_rule_view(item, material_names, field_map, rule_names, rule_map) for item in rules]
    # 派生项直接取实际编译结果，草稿展示与发布使用同一身份和启停。
    compiled = compile_draft_manifest(db, template_or_404(db, draft.scenario_code), draft)
    stored_keys = {item.rule_key for item in rules}
    for rule in compiled["rules"]:
        if rule["rule_key"] in stored_keys or rule.get("calculation_ref"):
            continue
        rule_items.append({**rule, "system": True,
                           "materials": [{"material_key": key, "material_name": material_names.get(key)} for key in rule.get("materials") or []],
                           "inputs": [{**entry, "field_name": field_map[entry["field_key"]].name if entry["field_key"] in field_map else None} for entry in rule.get("inputs") or []],
                           "outputs": [{"field_key": key, "field_name": field_map[key].name if key in field_map else None} for key in rule.get("outputs") or []],
                           "updated_at": None})
    section_items = [
        {
            "section_key": item.section_key,
            "title": item.title,
            "content_instruction": item.content_instruction,
            "field_keys": list(item.field_keys or []),
            "field_names": [field_map[key].name for key in (item.field_keys or []) if key in field_map],
            "sort_no": item.sort_no,
            # 报告分组属性：页面据此按报告展示与维护，而不是把章节当成
            # 无归属的附加章节。
            "report_code": item.report_code,
            "report_name": item.report_name,
            "material_keys": list(item.material_keys or []),
            "material_names": [
                material_names.get(key, key) for key in (item.material_keys or [])
            ],
            "formats": list(item.formats or []),
            "section_order": item.section_order,
            "content_type": item.content_type,
            "result_sort": item.result_sort,
            "risk_level_order": list(getattr(item, "risk_level_order", None) or []),
            "config_status": item.config_status,
            "origin": item.origin_json,
            "updated_at": item.updated_at,
        }
        for item in sections
    ]
    return {
        "scenario_code": draft.scenario_code,
        "revision_no": draft.revision_no,
        "source": draft.source,
        "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
        "base_version_no": draft.base_version_no,
        "schema_key": draft.schema_key,
        "schema_version": draft.schema_version,
        "schema_json": draft.schema_json,
        # 草稿记录的执行契约版本身份。
        "profile_version": draft.profile_version,
        # 与场景详情使用同一份当前发布条件，避免代码修复后仍显示历史错误。
        # 可发布不代表已发布，生效版本由场景版本号表示。
        "sync_error": _publish_readiness_error(db, template_or_404(db, draft.scenario_code), draft),
        "updated_at": draft.updated_at,
        # 节点实例：用户配置的流程（存在、顺序、依赖、启停）。
        # 页面流程编排读取节点；来源工作表清单由导入成功写入，不在节点面板另改。
        "nodes": node_items,
        # 来源表清单保存真实表名；历史模板保留其角色和节点绑定。
        "sheets": [
            {
                "sheet_role": item.sheet_role,
                "sheet_role_label": getattr(item, "sheet_name", None) or "未记录工作表名称",
                "sheet_key": getattr(item, "sheet_key", None),
                "sheet_name": getattr(item, "sheet_name", None),
                "node_code": item.node_code,
                "sort_no": item.sort_no,
            }
            for item in sheets
        ],
        "materials": material_items,
        "fields": field_items,
        "rules": rule_items,
        "sections": section_items,
        "disabled_source_rows": list(draft.disabled_source_rows or []),
        # 场景计算定义的核对视图。没有计算规则时为空列表，不编一份。
        "calculations": _calculation_view_rows(draft.calculation_json),
        "business_rule_count": len(rules),
        "system_rule_count": sum(bool(item.get("system")) for item in rule_items),
    }


def _draft_rule_view(
    item: ScenarioDraftRule,
    material_names: dict[str, str],
    field_map: dict[str, ScenarioDraftField],
    rule_names: dict[str, str],
    rule_map: dict[str, ScenarioDraftRule],
) -> dict[str, Any]:
    definition = item.rule_json if isinstance(item.rule_json, dict) else {}
    parameters = definition.get("parameters") if isinstance(definition.get("parameters"), dict) else {}
    inputs = []
    for entry in definition.get("inputs", []):
        field = field_map.get(entry.get("field_key"))
        inputs.append(
            {
                "field_key": entry.get("field_key"),
                "material_key": entry.get("material_key"),
                "field_name": field.name if field else None,
                "material_name": material_names.get(entry.get("material_key")),
                "value_type": field.value_type if field else None,
                "unit": field.unit if field else None,
            }
        )
    applicability = definition.get("applicability") if isinstance(definition.get("applicability"), dict) else {}
    material_keys = list(dict.fromkeys(str(key) for key in (definition.get("materials") or []) if str(key)))
    if item.operator_code == "RISK_TRIGGER":
        source_keys = list(dict.fromkeys([
            *(definition.get("source_rule_keys") or []),
            *([definition["source_rule_key"]] if definition.get("source_rule_key") else []),
        ]))
        for source_key in source_keys:
            source = rule_map.get(source_key)
            if source is None or not isinstance(source.rule_json, dict):
                continue
            source_definition = source.rule_json
            for material_key in source_definition.get("materials") or []:
                if material_key not in material_keys:
                    material_keys.append(material_key)
            for source_input in source_definition.get("inputs") or []:
                material_key = source_input.get("material_key")
                if material_key and material_key not in material_keys:
                    material_keys.append(material_key)
            for field_key in source_definition.get("outputs") or []:
                field = field_map.get(field_key)
                if field is not None and field.material_key not in material_keys:
                    material_keys.append(field.material_key)
    return {
        "rule_key": item.rule_key,
        "name": item.name,
        "node": item.node_code,
        "operator": item.operator_code,
        "enabled": item.enabled,
        "system": False,
        "inputs": inputs,
        "materials": [
            {"material_key": key, "material_name": material_names.get(key)}
            for key in material_keys
        ],
        "outputs": [
            {"field_key": key, "field_name": field_map[key].name, "material_key": field_map[key].material_key}
            for key in definition.get("outputs", []) if key in field_map
        ],
        "parameters": parameters,
        "requirement": str(definition.get("requirement") or ""),
        "applicability": {"kind": applicability.get("kind", "ALWAYS")},
        "source_rule_key": definition.get("source_rule_key"),
        "source_rule_name": rule_names.get(definition.get("source_rule_key")) if definition.get("source_rule_key") else None,
        "risk_level": parameters.get("risk_level"),
        "risk_action": parameters.get("action"),
        "source_ref": item.source_ref,
        "origin": item.origin_json,
        # 业务配置事实：页面要能显示检查等级、合格要求原文、风险等级原文、
        # 成立条件、多来源与配置状态，不能只给执行参数。
        "check_level": definition.get("check_level"),
        "check_operator": definition.get("check_operator"),
        "risk_level_value": definition.get("risk_level_value"),
        "risk_condition": definition.get("risk_condition"),
        "source_rule_keys": list(definition.get("source_rule_keys") or []),
        "source_rule_codes": list(definition.get("source_rule_codes") or []),
        "config_status": definition.get("config_status"),
        "quality_supported": definition.get("quality_supported"),
        "updated_at": item.updated_at,
    }


def list_scenarios_page(
    db: Session,
    *,
    name: str | None,
    category: str | None,
    enabled: bool | None,
    published_from: datetime | None,
    published_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """场景分页列表（真实后端筛选与 total）。"""
    conditions = []
    if name:
        conditions.append(ScenarioTemplate.scenario_name.like(f"%{name.strip()}%"))
    if category:
        conditions.append(ScenarioTemplate.category == category)
    if enabled is not None:
        conditions.append(ScenarioTemplate.enabled.is_(enabled))
    if published_from or published_to:
        version_conditions = [ScenarioVersion.scenario_code == ScenarioTemplate.scenario_code]
        if published_from:
            version_conditions.append(ScenarioVersion.published_at >= published_from)
        if published_to:
            version_conditions.append(ScenarioVersion.published_at <= published_to)
        conditions.append(select(ScenarioVersion.id).where(*version_conditions).exists())

    total = db.scalar(select(func.count()).select_from(ScenarioTemplate).where(*conditions)) or 0
    templates = db.scalars(
        select(ScenarioTemplate)
        .where(*conditions)
        .order_by(ScenarioTemplate.updated_at.desc(), ScenarioTemplate.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    codes = [item.scenario_code for item in templates]
    latest_versions: dict[str, tuple[int, datetime | None]] = {}
    draft_by_code: dict[str, ScenarioDraft] = {}
    draft_counts: dict[str, dict[str, int]] = {}
    if codes:
        for scenario_code, version_no, published_at in db.execute(
            select(ScenarioVersion.scenario_code, ScenarioVersion.version_no, ScenarioVersion.published_at)
            .where(ScenarioVersion.scenario_code.in_(codes), ScenarioVersion.status == "PUBLISHED")
        ).all():
            latest = latest_versions.get(scenario_code)
            if latest is None or version_no > latest[0]:
                latest_versions[scenario_code] = (version_no, published_at)
        for draft in db.scalars(select(ScenarioDraft).where(ScenarioDraft.scenario_code.in_(codes))).all():
            draft_by_code[draft.scenario_code] = draft
        for scenario_code, draft in draft_by_code.items():
            draft_counts[scenario_code] = {
                "material": len(draft.config_json["materials"]),
                "field": len(draft.config_json["fields"]),
                "rule": len(draft.config_json["rules"]),
            }

    items = []
    for template in templates:
        counts = draft_counts.get(template.scenario_code, {"material": 0, "field": 0, "rule": 0})
        latest = latest_versions.get(template.scenario_code)
        draft = draft_by_code.get(template.scenario_code)
        items.append(
            {
                "scenario_code": template.scenario_code,
                "scenario_name": template.scenario_name,
                "category": template.category,
                "description": template.description,
                "enabled": template.enabled,
                "published_version_no": latest[0] if latest else None,
                "published_at": latest[1] if latest else None,
                "latest_version_no": template.latest_version_no,
                "material_count": counts["material"],
                "field_count": counts["field"],
                "rule_count": counts["rule"],
                "draft_revision": draft.revision_no if draft else None,
                "draft_source": draft.source if draft else None,
                "draft_base_version_no": draft.base_version_no if draft else None,
                # 当前配置是否已生效：NULL 表示已生效。页面据此把“配置不完整”如实标出来，
                # 而不是让用户以为刚保存的内容已经生效。
                "sync_error": draft.sync_error if draft else None,
                "updated_at": template.updated_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def list_admin_scenario_options(db: Session) -> list[dict[str, Any]]:
    """管理员用的场景下拉：未删除的全部场景及其实时状态。

    导入页与场景管理页需要在场景发布之前就选中它（先建场景、再上传规则库），
    因此这里不能按「已发布」过滤——那会让管理员无法在新建场景下上传。
    """
    templates = db.scalars(
        select(ScenarioTemplate).order_by(ScenarioTemplate.scenario_name)
    ).all()
    latest: dict[str, int] = {}
    if templates:
        for scenario_code, version_no in db.execute(
            select(ScenarioVersion.scenario_code, func.max(ScenarioVersion.version_no))
            .where(ScenarioVersion.status == "PUBLISHED")
            .group_by(ScenarioVersion.scenario_code)
        ).all():
            latest[scenario_code] = int(version_no)
    return [
        {
            "scenario_code": item.scenario_code,
            "scenario_name": item.scenario_name,
            "category": item.category,
            "enabled": item.enabled,
            "latest_version_no": latest.get(item.scenario_code, 0),
            "available": item.enabled and item.scenario_code in latest,
        }
        for item in templates
    ]


def list_scenario_options(db: Session) -> list[dict[str, Any]]:
    """业务用户可用的场景下拉选项（方案、任务等选择器）。

    只列出「未删除 + 已启用 + 已有已发布版本」的场景：草稿、未发布、停用或已删除的
    场景不能用于创建方案。把不可用的场景也列出来、再在创建时报错，
    会让业务用户在一堆选不动的选项里试错。

    `available` 恒为 true；保留该字段是为了让页面无需自己推断，
    并让接口在将来放宽条件时不必改契约。
    """
    templates = db.scalars(
        select(ScenarioTemplate).order_by(ScenarioTemplate.scenario_name)
    ).all()
    latest: dict[str, int] = {}
    if templates:
        for scenario_code, version_no in db.execute(
            select(ScenarioVersion.scenario_code, func.max(ScenarioVersion.version_no))
            .where(ScenarioVersion.status == "PUBLISHED")
            .group_by(ScenarioVersion.scenario_code)
        ).all():
            latest[scenario_code] = int(version_no)
    return [
        {
            "scenario_code": item.scenario_code,
            "scenario_name": item.scenario_name,
            "category": item.category,
            "enabled": item.enabled,
            "latest_version_no": latest[item.scenario_code],
            "available": True,
        }
        for item in templates
        if item.enabled and item.scenario_code in latest
    ]


def _default_draft_nodes(draft_id: int) -> list[ScenarioDraftNode]:
    """只建立执行记录所需的技术容器，不预置业务阶段。"""
    return [ScenarioDraftNode(draft_id=draft_id, node_code="RULE_EXECUTION", node_type="RULE_EXECUTION", name="执行生效规则", sequence_no=1, depends_on=[], enabled=True, sort_no=1)]


def create_scenario(db: Session, payload: dict[str, Any]) -> ScenarioTemplate:
    """新建场景模板并建立带标准节点集的空草稿。"""
    name = str(payload.get("scenario_name") or "").strip()
    if not name:
        raise BusinessError("SCENARIO_NAME_REQUIRED", "场景名称不能为空", status_code=400)
    schema = scenario_schema.normalize_schema(payload.get("schema_json"))
    scenario_code = public_no("SCN")
    template = ScenarioTemplate(
        scenario_code=scenario_code,
        scenario_name=name,
        category=(str(payload.get("category")).strip() or None) if payload.get("category") else None,
        description=(str(payload.get("description")).strip() or None) if payload.get("description") else None,
        enabled=bool(payload.get("enabled", True)),
        latest_version_no=0,
        schema_key=schema["schema_key"],
        schema_version=schema["schema_version"],
        schema_json=schema,
    )
    db.add(template)
    db.flush()
    draft = ScenarioDraft(
        scenario_code=scenario_code,
        revision_no=0,
        source="FORM",
        base_version_no=None,
        schema_key=schema["schema_key"],
        schema_version=schema["schema_version"],
        schema_json=schema,
    )
    db.add(draft)
    db.flush()
    # 节点是流程的必填输入（编译时缺失即报错）。新场景直接带上协议声明的标准节点集：
    # 空着会让导入的「绑定节点」没有东西可选，用户得先把 9 个节点手工建一遍。
    for row in _default_draft_nodes(draft.id):
        config_store.add(db, row)
    db.flush()
    # 新建场景是空配置，必然不能发布（没有材料位）。这里就写下真实的阻断原因，
    # 否则页面会把一个空场景谎报成「可以发布」。不产生任何版本。
    _refresh_publish_readiness(db, template, draft)
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=name,
        operation_type="CREATE",
        detail={"scenario_code": scenario_code},
    )
    db.commit()
    db.refresh(template)
    return template


def update_scenario(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioTemplate:
    """编辑场景基本资料（只覆盖提交的字段）。

    场景名称会进入下一次发布的版本。名称变化时锁定草稿并递增修订号，
    使旧的 expected_revision 不能把另一位管理员改过的名称发布出去。
    保存只更新场景草稿与发布就绪状态；生成生效版本必须由管理员显式发布。
    """
    template = template_or_404(db, scenario_code, lock=True)
    config_changed = False
    if "scenario_name" in payload and payload["scenario_name"] is not None:
        name = str(payload["scenario_name"]).strip()
        if not name:
            raise BusinessError("SCENARIO_NAME_REQUIRED", "场景名称不能为空", status_code=400)
        if name != template.scenario_name:
            template.scenario_name = name
            config_changed = True
    if "schema_json" in payload:
        schema = scenario_schema.normalize_schema(payload["schema_json"])
        current = scenario_schema.normalize_schema(template.schema_json)
        if schema != current:
            template.schema_key = schema["schema_key"]
            template.schema_version = schema["schema_version"]
            template.schema_json = schema
            config_changed = True
    if "category" in payload:
        category = payload.get("category")
        template.category = (str(category).strip() or None) if category is not None else None
    if "description" in payload:
        description = payload.get("description")
        template.description = (str(description).strip() or None) if description is not None else None
    if "enabled" in payload and payload["enabled"] is not None:
        template.enabled = bool(payload["enabled"])
    if config_changed:
        draft = db.scalar(
            select(ScenarioDraft)
            .where(ScenarioDraft.scenario_code == template.scenario_code)
            .with_for_update()
        )
        if draft is None:
            draft = ensure_draft(db, template)
            db.flush()
            draft = db.scalar(
                select(ScenarioDraft)
                .where(ScenarioDraft.scenario_code == template.scenario_code)
                .with_for_update()
            )
        draft.schema_key = template.schema_key
        draft.schema_version = template.schema_version
        draft.schema_json = template.schema_json
        _bump_draft(draft)
        db.flush()
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=template.scenario_name,
        operation_type="UPDATE",
        detail={"scenario_code": scenario_code},
    )
    if config_changed:
        _commit_config_change(db, template, draft)
    else:
        db.commit()
    db.refresh(template)
    return template


def set_scenario_enabled(db: Session, scenario_code: str, enabled: bool) -> ScenarioTemplate:
    """启用 / 停用场景。"""
    template = template_or_404(db, scenario_code, lock=True)
    template.enabled = enabled
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=template.scenario_name,
        operation_type="ENABLE" if enabled else "DISABLE",
        detail={"scenario_code": scenario_code},
    )
    db.commit()
    db.refresh(template)
    return template


def delete_scenario(db: Session, scenario_code: str) -> None:
    """实际删除场景身份和 JSON 草稿；独立方案、任务与已发布执行快照不级联删除。"""
    template = template_or_404(db, scenario_code, lock=True)
    draft = db.scalar(
        select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code).with_for_update()
    )
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=template.scenario_name,
        operation_type="DELETE",
        detail={"scenario_code": scenario_code, "deletion_mode": "PHYSICAL"},
    )
    if draft is not None:
        db.delete(draft)
    db.delete(template)
    db.commit()


def scenario_detail(db: Session, scenario_code: str) -> dict[str, Any]:
    """场景详情：基本资料、当前发布版本号与配置状态。"""
    template = template_or_404(db, scenario_code)
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    plan_count = db.scalar(
        select(func.count()).select_from(AuditPlan).where(AuditPlan.scenario_code == scenario_code, AuditPlan.deleted_at.is_(None))
    ) or 0
    return {
        "scenario_code": template.scenario_code,
        "schema_key": template.schema_key,
        "schema_version": template.schema_version,
        "schema_json": template.schema_json,
        "scenario_name": template.scenario_name,
        "category": template.category,
        "description": template.description,
        "enabled": template.enabled,
        "latest_version_no": template.latest_version_no,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
        "plan_count": int(plan_count),
        "draft": {
            "revision_no": draft.revision_no,
            "source": draft.source,
            "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
            "base_version_no": draft.base_version_no,
            # 按当前编译逻辑显示发布条件，不能用历史错误锁住发布按钮。
            "sync_error": _publish_readiness_error(db, template, draft),
            "updated_at": draft.updated_at,
        }
        if draft
        else None,
    }


# 草稿：材料 / 字段 / 规则 / 报告章节


def _locked_draft(db: Session, scenario_code: str) -> tuple[ScenarioTemplate, ScenarioDraft]:
    template = template_or_404(db, scenario_code, lock=True)
    draft = ensure_draft(db, template)
    return template, draft


def update_draft_disabled_source_row(
    db: Session, scenario_code: str, row_key: str, payload: dict[str, Any]
) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    expected_revision = payload["expected_revision"]
    if expected_revision != draft.revision_no:
        raise BusinessError(
            "SCENARIO_DRAFT_REVISION_CONFLICT",
            f"配置在本次编辑期间已被修改（当前修订 {draft.revision_no}），请刷新后重试",
            status_code=409,
            details={"scenario_code": scenario_code, "expected": expected_revision, "actual": draft.revision_no},
        )
    rows = list(draft.disabled_source_rows or [])
    index = next((i for i, item in enumerate(rows) if item["row_key"] == row_key), None)
    if index is None:
        raise BusinessError("DISABLED_SOURCE_ROW_NOT_FOUND", "停用来源行不存在", status_code=404)
    row = dict(rows[index])
    role = row["sheet_role"]
    stored_columns = {column["key"]: column for column in row["columns"]}
    if len(stored_columns) != len(row["columns"]):
        raise BusinessError("DISABLED_SOURCE_ROW_COLUMN_DUPLICATE", "停用来源行存在重复列标识", status_code=409)
    values = payload["values"]
    unknown = sorted(set(values) - set(stored_columns))
    if unknown:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_COLUMN_UNKNOWN",
            "修改内容包含当前工作表不存在的列",
            details={"row_key": row_key, "columns": unknown},
        )
    updated_columns = [
        {**column, "value": values[column["key"]].strip()} if column["key"] in values else dict(column)
        for column in row["columns"]
    ]
    row["columns"] = updated_columns
    rows[index] = row
    draft.disabled_source_rows = rows
    _save_draft(
        db,
        template,
        draft,
        operation_type="UPDATE",
        object_name=template.scenario_name,
        detail={"action": "EDIT_DISABLED_SOURCE_ROW", "row_key": row_key, "sheet_role": role, "columns": sorted(values)},
    )
    db.refresh(draft)
    return draft


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
    """刷新草稿的发布就绪状态并提交；发布只能走管理员显式发布入口。"""
    _refresh_publish_readiness(db, template, draft)
    db.commit()


def _calculation_view_rows(raw: Any) -> list[dict[str, Any]]:
    """按原始计算定义展示，不读取旧编译产物。"""
    candidates = _calculation_store(raw)
    source = candidates
    return [
        {
            "rule_code": item.get("rule_code"),
            "name": item.get("rule_name") or item.get("name"),
            "node_code": item.get("node_code") or item.get("node_instance_code"),
            "formula": item.get("formula"),
            "requirement": item.get("requirement", ""),
            "business_context": item.get("business_context", {}),
            "result_code": item.get("result_code"),
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "status": item.get("status"),
            "origin": item.get("origin"),
            "parameter_sources": [
                {
                    "code": parameter.get("parameter_code"),
                    "origin": parameter.get("origin"),
                }
                for parameter in item.get("parameters") or []
            ],
        }
        for item in source
        if isinstance(item, dict)
    ]


def assess_publish(db: Session, template: ScenarioTemplate, draft: ScenarioDraft) -> dict[str, Any]:
    """发布只编译身份与配置结构；单条业务可执行性由运行结果表达。"""
    _, sheets, _, _, _, _ = _draft_rows(db, draft.id)
    if not sheets:
        raise BusinessError("SCENARIO_SHEET_REQUIRED", "场景尚未选入规则库工作表，请先导入规则库", status_code=409)
    return compile_draft_manifest(db, template, draft)


def _refresh_publish_readiness(
    db: Session, template: ScenarioTemplate, draft: ScenarioDraft
) -> bool:
    """重新计算「这份草稿现在能不能发布」，把阻断原因写进 `draft.sync_error`。

    与 `publish_draft` 调用同一个 `assess_publish`。业务问题与能力缺口不阻断发布。
    本函数不产生任何版本；保存草稿必须成功，因此只捕获
    BusinessError。其它异常继续抛出。
    """
    draft.sync_error = _publish_readiness_error(db, template, draft)
    return draft.sync_error is None


def _publish_readiness_error(
    db: Session, template: ScenarioTemplate, draft: ScenarioDraft,
) -> dict[str, Any] | None:
    """按当前配置和编译逻辑计算发布原因，不写数据库或创建版本。"""
    try:
        assess_publish(db, template, draft)
    except BusinessError as failure:
        logger.info(
            "场景配置尚不能发布：scenario_code=%s code=%s message=%s",
            template.scenario_code,
            failure.code,
            failure.message,
        )
        return _sync_error_detail(failure)
    return None


def _sync_error_detail(failure: BusinessError) -> dict[str, Any]:
    """把编译失败整理成页面可直显的结构：错误码、中文原因与定位明细。

    不在这里附「下一步」文案：那属于页面表达，后端只回事实，避免两处维护同一套提示。
    """
    detail = failure.details if isinstance(failure.details, dict) else {}
    return {
        "code": failure.code,
        "message": failure.message,
        "details": {key: value for key, value in detail.items() if key in SAFE_DETAIL_KEYS},
    }


def _publish_manifest(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    manifest: dict[str, Any],
    *,
    change_summary: str | None,
) -> tuple[ScenarioVersion, bool]:
    """把一份已编译的 manifest 落成场景版本；内容未变时复用最新版本。

    调用方必须已经校验过 manifest（compile_form_manifest 会拒绝不完整配置）。
    返回 (version, reused)；reused=True 表示内容与最新版本一致，未产生新版本。
    本函数不提交事务，由调用方决定提交边界。
    """
    latest = _latest_published_version(db, template.scenario_code)
    # 用范围指纹比较（而不是 manifest_hash）：后者含编译时间，两次编译必然不同，
    # 会导致“内容没变再保存”每次都生成一个仅时间不同的重复版本。
    if (
        latest is not None
        and manifest_scope_hash(latest.manifest_json) == manifest_scope_hash(manifest)
        and latest.scenario_name == template.scenario_name
    ):
        return latest, True

    base_version = version_or_404(db, template.scenario_code, draft.base_version_no) if draft.base_version_no else None
    source = "REVISION" if base_version is not None else "FORM"
    import_job_id: int | None = None
    # 导入得到的草稿第一次发布必须保留 Excel 作业溯源；否则动态字段和规则虽然
    # 已进入 manifest，却会在版本层被错误标成页面创建，且无法回到原始规则库。
    # 同一个作业只能关联一个版本，后续基于该草稿的页面修订统一记为 REVISION。
    if draft.source == "EXCEL":
        import_job_no = draft.config_json.get("import_job_no") if isinstance(draft.config_json, dict) else None
        if not isinstance(import_job_no, str) or not import_job_no:
            raise BusinessError(
                "DRAFT_IMPORT_PROVENANCE_MISSING",
                "规则库导入草稿缺少原始导入作业编号，不能发布为可追溯版本",
                status_code=409,
                details={"scenario_code": template.scenario_code},
            )
        import_job = db.scalar(select(ImportJob).where(ImportJob.job_no == import_job_no))
        if import_job is None or import_job.scenario_code != template.scenario_code:
            raise BusinessError(
                "DRAFT_IMPORT_PROVENANCE_INVALID",
                "规则库导入草稿引用的原始导入作业不存在或不属于当前场景",
                status_code=409,
                details={"scenario_code": template.scenario_code, "import_job_no": import_job_no},
            )
        existing_import_version = db.scalar(
            select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == import_job.id)
        )
        if existing_import_version is None:
            source = "IMPORT"
            import_job_id = import_job.id
    version_no = allocate_version(db, template.scenario_code, template.scenario_name)
    version = ScenarioVersion(
        import_job_id=import_job_id,
        scenario_code=template.scenario_code,
        scenario_name=template.scenario_name,
        schema_key=draft.schema_key,
        schema_version=draft.schema_version,
        schema_json=draft.schema_json,
        version_no=version_no,
        status="PUBLISHED",
        source=source,
        base_version_id=base_version.id if base_version else None,
        change_summary=change_summary,
        manifest_hash=manifest["manifest_hash"],
        manifest_json=manifest,
        published_at=datetime.now(UTC),
    )
    db.add(version)
    db.flush()
    for material in manifest["materials"]:
        db.add(
            ScenarioMaterial(
                scenario_version_id=version.id,
                material_key=material["material_key"],
                name=material["name"],
                required=material["required"],
                source_json=material["source"],
            )
        )
    for node in manifest["nodes"]:
        # 与导入路径同一口径：节点投影必须带上能力类型、依赖 DAG 与节点级配置。
        # 能力类型只能来自 manifest 的显式声明；缺失时按旧格式确定性迁移，
        # 迁移不出来就明确报错（编译期已拦截，这里是第二道）。
        projected_type = node.get("node_type")
        if not isinstance(projected_type, str) or not projected_type:
            raise BusinessError(
                "MANIFEST_NODE_TYPE_UNRESOLVED",
                f"已发布版本里的节点“{node['node_code']}”没有能力类型，无法写入节点投影",
                status_code=500,
                details={"node_code": node["node_code"]},
            )
        db.add(
            ScenarioNode(
                scenario_version_id=version.id,
                node_code=node["node_code"],
                node_type=projected_type,
                name=node["name"],
                sequence_no=node["sequence_no"],
                depends_on=list(node.get("depends_on") or []),
                config_json=node.get("config_json"),
                enabled=node["enabled"],
            )
        )
    for rule in manifest["rules"]:
        db.add(
            ScenarioRule(
                scenario_version_id=version.id,
                rule_key=rule["rule_key"],
                rule_code=rule.get("rule_code"),
                legacy_rule_key=rule.get("legacy_rule_key") or rule["rule_key"],
                name=rule["name"],
                node_code=rule["node"],
                operator_code=rule["operator"],
                enabled=rule["enabled"],
                rule_json=rule,
                source_json=rule["source"],
            )
        )
    # 版本内容与草稿一致：记录基准版本，供后续编辑继续以它为基准。
    draft.base_version_no = version_no
    return version, False


def create_draft_material(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增材料位。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
    source_ref = _normalized_source_ref(payload.get("source_ref"))
    material_key = stable_key("mat", scenario_code, _item_ref())
    config_store.add(
        db, ScenarioDraftMaterial(
            draft_id=draft.id,
            material_key=material_key,
            name=name,
            required=bool(payload.get("required", True)),
            description=str(payload.get("description") or ""),
            source_ref=source_ref,
            sort_no=_next_sort_no(db, ScenarioDraftMaterial, draft.id),
        )
    )
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_CREATE",
        object_name=name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def _draft_material_or_404(db: Session, draft_id: int, material_key: str) -> ScenarioDraftMaterial:
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftMaterial, draft_id) if (item.material_key == material_key)]), None)
    if not item:
        raise BusinessError("DRAFT_MATERIAL_NOT_FOUND", "材料配置不存在", status_code=404, details={"material_key": material_key})
    return item


def update_draft_material(db: Session, scenario_code: str, material_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """修改草稿中的材料位（只覆盖提交的字段）。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_material_or_404(db, draft.id, material_key)
    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
        item.name = name
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "description" in payload and payload["description"] is not None:
        item.description = str(payload["description"])
    if "source_ref" in payload:
        source_ref = _normalized_source_ref(payload.get("source_ref"))
        item.source_ref = source_ref
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_UPDATE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def delete_draft_material(db: Session, scenario_code: str, material_key: str) -> ScenarioDraft:
    """删除草稿中的材料位；仍被字段或规则引用时 409，不级联删除。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_material_or_404(db, draft.id, material_key)
    fields = [item for item in config_store.rows(db, ScenarioDraftField, draft.id) if (item.material_key == material_key)]
    rules = config_store.rows(db, ScenarioDraftRule, draft.id)
    referencing_rules = [
        rule.name
        for rule in rules
        if material_key in (rule.rule_json or {}).get("materials", [])
        or any(entry.get("material_key") == material_key for entry in (rule.rule_json or {}).get("inputs", []))
    ]
    if fields or referencing_rules:
        raise BusinessError(
            "MATERIAL_REFERENCED",
            "该材料仍被以下字段或规则引用，请先在对应配置中移除后再删除",
            status_code=409,
            details={
                "material_name": item.name,
                "field_keys": [f"{field.name}（{field.field_key}）" for field in fields],
                "rule_keys": referencing_rules,
            },
        )
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def _assert_field_source_ref_available(
    db: Session, draft_id: int, source_ref: str | None, *, field_key: str | None = None
) -> None:
    code = source_ref or field_key
    if code is None:
        return
    existing = next((item for item in config_store.rows(db, ScenarioDraftField, draft_id)
                     if (_normalized_source_ref(item.source_ref) or item.field_key) == code
                     and (field_key is None or item.field_key != field_key)), None)
    if existing is not None:
        raise BusinessError(
            "FIELD_CODE_DUPLICATE",
            f"字段编号“{code}”已由字段“{existing.name}”使用",
            status_code=409,
            details={"source_ref": code, "field_key": existing.field_key},
        )


def create_draft_field(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增字段。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("FIELD_NAME_REQUIRED", "字段名称不能为空", status_code=400)
    material_key = str(payload.get("material_key") or "")
    _draft_material_or_404(db, draft.id, material_key)
    value_type = str(payload.get("value_type") or "STRING")
    if value_type not in DRAFT_VALUE_TYPES:
        raise BusinessError(
            "FIELD_TYPE_INVALID",
            "字段类型不受支持",
            status_code=400,
            details={"value_type": value_type, "allowed": sorted(DRAFT_VALUE_TYPES)},
        )
    extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
        value_type=value_type,
        extraction_strategy=payload.get("extraction_strategy"),
        structure_schema=payload.get("structure_schema"),
        field_name=name,
    )
    source_ref = _normalized_source_ref(payload.get("source_ref"))
    _assert_field_source_ref_available(db, draft.id, source_ref)
    field_key = stable_key("fld", scenario_code, _item_ref())
    field = config_store.add(
        db, ScenarioDraftField(
            draft_id=draft.id,
            field_key=field_key,
            material_key=material_key,
            name=name,
            value_type=value_type,
            extraction_strategy=extraction_strategy,
            structure_schema_json=structure_schema,
            unit=(str(payload["unit"]).strip() or None) if payload.get("unit") else None,
            currency=(str(payload["currency"]).strip() or None) if payload.get("currency") else None,
            artifact_source=(
                str(payload["artifact_source"]).strip() or None
            )
            if payload.get("artifact_source")
            else None,
            period_scope=_field_scope(payload, "period_scope"),
            statement_scope=_field_scope(payload, "statement_scope"),
            required=bool(payload.get("required", False)),
            instruction=str(payload.get("instruction") or ""),
            source_ref=source_ref,
            sort_no=_next_sort_no(db, ScenarioDraftField, draft.id),
        )
    )
    field.node_code = "RULE_EXECUTION"
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_CREATE",
        object_name=name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


def _draft_field_or_404(db: Session, draft_id: int, field_key: str) -> ScenarioDraftField:
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftField, draft_id) if (item.field_key == field_key)]), None)
    if not item:
        raise BusinessError("DRAFT_FIELD_NOT_FOUND", "字段配置不存在", status_code=404, details={"field_key": field_key})
    return item




def _rules_referencing_field(db: Session, draft_id: int, field_key: str) -> list[str]:
    rules = config_store.rows(db, ScenarioDraftRule, draft_id)
    return [
        rule.name
        for rule in rules
        if any(entry.get("field_key") == field_key for entry in (rule.rule_json or {}).get("inputs", []))
        or field_key in ((rule.rule_json or {}).get("outputs") or [])
    ]


def update_draft_field(db: Session, scenario_code: str, field_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """修改草稿中的字段（只覆盖提交的字段）。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_field_or_404(db, draft.id, field_key)
    if "material_key" in payload and payload["material_key"] is not None and payload["material_key"] != item.material_key:
        referencing = _rules_referencing_field(db, draft.id, field_key)
        if referencing:
            raise BusinessError(
                "FIELD_MATERIAL_CHANGE_REFERENCED",
                "该字段已被规则引用，先调整相关规则后再更换所属材料",
                status_code=409,
                details={"field_name": item.name, "rule_keys": referencing},
            )
        _draft_material_or_404(db, draft.id, str(payload["material_key"]))
        item.material_key = str(payload["material_key"])
    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise BusinessError("FIELD_NAME_REQUIRED", "字段名称不能为空", status_code=400)
        item.name = name
    if "value_type" in payload and payload["value_type"] is not None:
        value_type = str(payload["value_type"])
        if value_type not in DRAFT_VALUE_TYPES:
            raise BusinessError(
                "FIELD_TYPE_INVALID",
                "字段类型不受支持",
                status_code=400,
                details={"value_type": value_type, "allowed": sorted(DRAFT_VALUE_TYPES)},
            )
        item.value_type = value_type
    if "extraction_strategy" in payload or "structure_schema" in payload or "value_type" in payload:
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=item.value_type,
            extraction_strategy=(
                payload.get("extraction_strategy")
                if "extraction_strategy" in payload
                else item.extraction_strategy
            ),
            structure_schema=(
                payload.get("structure_schema")
                if "structure_schema" in payload
                else item.structure_schema_json
            ),
            field_name=item.name,
        )
        item.extraction_strategy = extraction_strategy
        item.structure_schema_json = structure_schema
    if "unit" in payload:
        unit = payload.get("unit")
        item.unit = (str(unit).strip() or None) if unit else None
    if "currency" in payload:
        currency = payload.get("currency")
        item.currency = (str(currency).strip() or None) if currency else None
    if "artifact_source" in payload:
        # 产物来源：留空表示字段由常规字段抽取提供，不表示“未声明所以随便猜”。
        artifact_source = payload.get("artifact_source")
        normalized_source = (str(artifact_source).strip() or None) if artifact_source else None
        if normalized_source is not None and normalized_source not in FIELD_ARTIFACT_SOURCES:
            raise BusinessError(
                "FIELD_ARTIFACT_SOURCE_INVALID",
                "字段的产物来源不受支持",
                status_code=400,
                details={"artifact_source": normalized_source, "allowed": sorted(FIELD_ARTIFACT_SOURCES)},
            )
        item.artifact_source = normalized_source
    if "period_scope" in payload and payload["period_scope"] is not None:
        item.period_scope = _field_scope(payload, "period_scope")
    if "statement_scope" in payload and payload["statement_scope"] is not None:
        item.statement_scope = _field_scope(
            payload, "statement_scope"
        )
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "instruction" in payload and payload["instruction"] is not None:
        item.instruction = str(payload["instruction"])
    if "source_ref" in payload:
        source_ref = _normalized_source_ref(payload.get("source_ref"))
        _assert_field_source_ref_available(db, draft.id, source_ref, field_key=field_key)
        previous_code = _normalized_source_ref(item.source_ref) or field_key
        next_code = source_ref or field_key
        if previous_code != next_code:
            draft.calculation_json = _remap_calculation_field_codes(
                draft.calculation_json, {previous_code: next_code},
            )
        item.source_ref = source_ref
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_UPDATE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


def delete_draft_field(db: Session, scenario_code: str, field_key: str) -> ScenarioDraft:
    """删除草稿中的字段；仍被规则、计算或报告引用时 409，不级联删除。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_field_or_404(db, draft.id, field_key)
    referencing = _rules_referencing_field(db, draft.id, field_key)
    calculations = _calculations_referencing_field(
        draft.calculation_json, _normalized_source_ref(item.source_ref) or field_key,
    )
    sections = config_store.rows(db, ScenarioDraftSection, draft.id)
    referencing_sections = [section.title for section in sections if field_key in (section.field_keys or [])]
    if referencing or calculations or referencing_sections:
        raise BusinessError(
            "FIELD_REFERENCED",
            "该字段仍被规则、计算或报告章节引用，请先移除引用后再删除",
            status_code=409,
            details={"field_name": item.name, "rule_keys": referencing,
                     "calculation_rules": calculations, "section_names": referencing_sections},
        )
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


# 草稿：规则


def _normalize_rule_payload(db: Session, draft: ScenarioDraft, payload: dict[str, Any], *, existing_key: str | None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """原文与引用按管理员提交保存，不检查方法、口径、业务阶段或算子映射。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("RULE_NAME_REQUIRED", "规则名称不能为空", status_code=400)
    return {**(previous or {}), **payload, "name": name, "node": "RULE_EXECUTION", "node_type": "RULE_EXECUTION",
            "operator": payload.get("operator") or (previous or {}).get("operator") or "LLM_EXECUTE",
            "source_ref": _normalized_source_ref(payload.get("source_ref")), "enabled": bool(payload.get("enabled", True))}


def _normalized_source_ref(value: Any) -> str | None:
    """业务编号：留空表示页面新建、没有来源编号，不伪造一个。"""
    text = str(value or "").strip()
    return text or None




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
            sort_no=_next_sort_no(db, ScenarioDraftRule, draft.id),
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


def _section_group_fields(db: Session, draft_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """报告章节的分组属性：报告编号、报告名称、材料范围、输出格式、结果排序、
    章节顺序、内容类型与业务配置状态。

    场景报告是「按编号分组的一组章节」；这些属性必须能
    通过页面维护并保存，否则保存一次就会退回旧的附加展示章节语义。
    缺省值为 None：旧格式章节本来就没有这些属性，不替它编造分组。
    """
    materials = config_store.rows(db, ScenarioDraftMaterial, draft_id)
    known_materials = {item.material_key for item in materials}
    result: dict[str, Any] = {}
    if "report_code" in payload:
        value = payload.get("report_code")
        result["report_code"] = str(value).strip() or None if value is not None else None
    if "report_name" in payload:
        value = payload.get("report_name")
        result["report_name"] = str(value).strip() or None if value is not None else None
    if "material_keys" in payload:
        keys: list[str] = []
        for key in payload.get("material_keys") or []:
            key = str(key)
            if key not in known_materials:
                raise BusinessError(
                    "SECTION_MATERIAL_MISSING",
                    "报告章节的材料范围引用了不存在的材料",
                    status_code=400,
                    details={"material_key": key},
                )
            if key not in keys:
                keys.append(key)
        result["material_keys"] = keys
    if "formats" in payload:
        result["formats"] = [str(item) for item in (payload.get("formats") or [])]
    if "section_order" in payload:
        value = payload.get("section_order")
        result["section_order"] = int(value) if value is not None else None
    if "content_type" in payload:
        value = payload.get("content_type")
        result["content_type"] = str(value).strip() or None if value is not None else None
    if "result_sort" in payload:
        value = payload.get("result_sort")
        result["result_sort"] = str(value).strip() or None if value is not None else None
    if "risk_level_order" in payload:
        result["risk_level_order"] = list(payload.get("risk_level_order") or [])
    if "config_status" in payload:
        value = payload.get("config_status")
        result["config_status"] = str(value).strip() or None if value is not None else None
    return result


def create_draft_section(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增报告章节。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
    field_keys = _validate_section_fields(db, draft.id, payload.get("field_keys"))
    section_key = stable_key("sec", scenario_code, _item_ref())
    config_store.add(
        db, ScenarioDraftSection(
            draft_id=draft.id,
            section_key=section_key,
            title=title,
            content_instruction=str(payload.get("content_instruction") or ""),
            field_keys=field_keys,
            sort_no=_next_sort_no(db, ScenarioDraftSection, draft.id),
            **_section_group_fields(db, draft.id, payload),
        )
    )
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_CREATE",
        object_name=title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


def _validate_section_fields(db: Session, draft_id: int, field_keys: Any) -> list[str]:
    fields = config_store.rows(db, ScenarioDraftField, draft_id)
    known = {item.field_key for item in fields}
    result: list[str] = []
    for key in field_keys or []:
        key = str(key)
        if key not in known:
            raise BusinessError("SECTION_FIELD_MISSING", "章节引用的字段不存在", status_code=400, details={"field_key": key})
        if key not in result:
            result.append(key)
    return result


def update_draft_section(db: Session, scenario_code: str, section_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """修改草稿中的报告章节。"""
    template, draft = _locked_draft(db, scenario_code)
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftSection, draft.id) if (item.section_key == section_key)]), None)
    if not item:
        raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
    if "title" in payload and payload["title"] is not None:
        title = str(payload["title"]).strip()
        if not title:
            raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
        item.title = title
    if "content_instruction" in payload and payload["content_instruction"] is not None:
        item.content_instruction = str(payload["content_instruction"])
    if "field_keys" in payload:
        item.field_keys = _validate_section_fields(db, draft.id, payload.get("field_keys"))
    # 报告分组属性：本次提交未覆盖的键保持原值，避免页面保存一次就丢掉报告编号、
    # 材料范围、输出格式或结果排序。
    for key, value in _section_group_fields(db, draft.id, payload).items():
        setattr(item, key, value)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_UPDATE",
        object_name=item.title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


def delete_draft_section(db: Session, scenario_code: str, section_key: str) -> ScenarioDraft:
    """删除草稿中的报告章节。"""
    template, draft = _locked_draft(db, scenario_code)
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftSection, draft.id) if (item.section_key == section_key)]), None)
    if not item:
        raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_DELETE",
        object_name=item.title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


# 发布与版本


def _calculation_store(raw: Any) -> list[dict[str, Any]]:
    """读取原始计算定义；不转换旧执行器的编译格式。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        if any(not isinstance(item, dict) for item in raw):
            raise BusinessError("RULE_LIBRARY_CALCULATION_FORMAT_INVALID", "计算定义必须是对象列表")
        return raw
    raise BusinessError("RULE_LIBRARY_CALCULATION_FORMAT_INVALID", "计算配置须为规则库原始定义列表，不再转换旧编译格式")


def _calculations_referencing_field(raw: Any, field_code: str) -> list[str]:
    """按原始计算定义的精确字段来源查引用。"""
    candidates = _calculation_store(raw)
    return [
        item.get("rule_name") or item["rule_code"]
        for item in candidates
        if any(parameter.get("source_type") == "FIELD" and parameter.get("source_code") == field_code
               for parameter in item.get("parameters") or [])
        or field_code in (item.get("depends_on_field_codes") or [])
    ]


def _remap_calculation_field_codes(raw: Any, code_changes: dict[str, str]) -> Any:
    """同一字段改编号时同步当前计算引用；一次映射支持批量交换编号。"""
    updated = deepcopy(raw)
    candidates = _calculation_store(updated)
    for item in candidates:
        for parameter in item.get("parameters") or []:
            if parameter.get("source_type") == "FIELD" and parameter.get("source_code") in code_changes:
                parameter["source_code"] = code_changes[parameter["source_code"]]
        if "depends_on_field_codes" in item:
            item["depends_on_field_codes"] = [
                code_changes.get(code, code) for code in item["depends_on_field_codes"]
            ]
    return updated


def compile_draft_manifest(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
) -> dict[str, Any]:
    """把草稿编译成规则库快照，供发布、草稿导出与草稿视图使用。"""
    nodes, sheets, materials, fields, rules, sections = _draft_rows(db, draft.id)
    calculation_candidates = _calculation_store(draft.calculation_json)
    # 配置行 → 编译入参的映射口径只有一份：domain.manifest.compile_inputs_of_config。
    # 方案保存走同一份，两条路径的编译产物因此必然同形。
    inputs = manifest_module.compile_inputs_of_config(
        materials=materials,
        fields=fields,
        rules=rules,
        sections=sections,
        nodes=nodes,
        sheets=sheets,
        profile_version=draft.profile_version or node_profile.NODE_PROFILE_VERSION,
        scenario_schema_config=draft.schema_json,
    )
    manifest = compile_form_manifest(
        scene_code=template.scenario_code,
        scene_name=template.scenario_name,
        namespace=template.scenario_code,
        **inputs,
        compiled_at=datetime.now(UTC),
        calculation_candidates=calculation_candidates,
        disabled_source_rows=list(draft.disabled_source_rows or []),
        import_proposals=draft.config_json.get("import_proposals"),
    )
    # 报告与版本详情需要区分「Excel 规则库导入」和页面配置；动态配置本身
    # 虽然已经编译成统一 manifest，但来源不能在编译时被改写成 FORM。
    manifest["source_kind"] = "EXCEL" if draft.source == "EXCEL" else "FORM"
    canonical = json.dumps(
        {key: value for key, value in manifest.items() if key != "manifest_hash"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest


def publish_draft(db: Session, scenario_code: str, payload: dict[str, Any]) -> tuple[ScenarioVersion, bool]:
    """把当前完整配置编译成不可变规则库版本。

    发布前的检查全部由 `assess_publish` 承担，与草稿就绪状态是同一套逻辑；任一项不通过即原样
    抛错，且在抛错前不创建 `ScenarioVersion`。

    expected_revision 必须与草稿当前修订一致，否则明确冲突（刷新后重试）；内容与最新已发布
    版本完全一致（同范围指纹、同名称）时返回该版本，不制造重复版本；版本号由 allocate_version
    的行锁与原子计数器在同一事务内分配；已发布版本不可修改，发布只新增版本。
    """
    template = template_or_404(db, scenario_code, lock=True)
    draft = ensure_draft(db, template)
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None and int(expected_revision) != draft.revision_no:
        raise BusinessError(
            "SCENARIO_DRAFT_REVISION_CONFLICT",
            f"配置在本次编辑期间已被修改（当前修订 {draft.revision_no}），请刷新后重试",
            status_code=409,
            details={"scenario_code": scenario_code, "expected": int(expected_revision), "actual": draft.revision_no},
        )
    manifest = assess_publish(
        db,
        template,
        draft,
    )
    change_summary = (str(payload.get("change_summary")).strip() or None) if payload.get("change_summary") else None
    version, reused = _publish_manifest(db, template, draft, manifest, change_summary=change_summary)
    # 发布后按当前结构与引用重新计算草稿状态。
    _refresh_publish_readiness(db, template, draft)
    if not reused:
        record_operation(
            db,
            object_type=OBJECT_SCENARIO,
            object_no=scenario_code,
            object_name=template.scenario_name,
            operation_type="PUBLISH",
            detail={
                "scenario_code": scenario_code,
                "version_no": version.version_no,
                "source": version.source,
                "material_count": len(manifest["materials"]),
                "field_count": len(manifest["fields"]),
                "rule_count": len(manifest["rules"]),
            },
        )
    db.commit()
    db.refresh(version)
    return version, reused
