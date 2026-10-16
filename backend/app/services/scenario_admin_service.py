"""审核场景的后台管理：基本资料、配置草稿与发布。

业务模型：场景身份 = scenario_template（名称/业务类型/说明/启停 + 版本计数器）；
配置草稿 = scenario_draft 及其材料/字段/规则/报告章节，每次写入递增 revision_no，发布必须
携带当前修订号；发布 = 用 compile_form_manifest 把草稿编译成与导入流程同构的 manifest，
形成不可覆盖的 scenario_version 并写入关系投影，成功后删除草稿；后续编辑从发布配置创建新
草稿并发布新快照。有有效导入作业时保存关联；缺少作业时保留 manifest 中的来源说明并记录
警告，不以追溯完整性阻止发布，也不伪造作业关联。

所有写操作都在同一事务内追加 operation_log；删除材料/字段前先返回引用清单，不自动换对象、
不级联删除规则。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain.calculation_config import (
    calculation_store,
    calculations_referencing_field,
    remap_calculation_field_codes,
)

from ..repositories import configuration_store as config_store
from ..repositories.scenario_repository import find_latest_published_version

from ..domain import (
    field_strategy,
    manifest as manifest_module,
    scenario_schema,
    node_profile,
)
from ..domain.audit_vocab import SCOPE_NOT_APPLICABLE
from ..domain.rule_library import active, is_risk_rule
from ..domain.report_data import report_data_refs
from ..domain.manifest import (
    FORM_VALUE_TYPES,
    compile_form_manifest,
    manifest_scope_hash,
)
from ..common.exceptions import BusinessError
from ..entity import (
    AuditPlan,
    ImportJob,
    ScenarioDraft,
    ScenarioDraftField,
    ScenarioDraftNode,
    ScenarioDraftMaterial,
    ScenarioDraftRule,
    ScenarioDraftSection,
    ScenarioNode,
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


def _draft_rows(
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


def _field_scope(payload: dict[str, Any], key: str) -> str:
    """口径取自规则库原文；未涉及时使用存储唯一键所需的非空标识。"""
    return str(payload.get(key) or "").strip() or SCOPE_NOT_APPLICABLE


def _item_ref() -> str:
    return public_no("ref")


def _node_type_label(node_type: str) -> str:
    """技术执行容器的展示名称。"""
    return node_profile.NODE_DISPLAY_NAME


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
    config["node_instances"] = [{"node_code": "RULE_EXECUTION", "node_type": "RULE_EXECUTION", "name": "执行生效规则", "sequence_no": 1, "depends_on": [], "config_json": {}, "enabled": True}]
    _write_config_into_draft(db, draft, config, replace_nodes=True)


def _write_config_into_draft(
    db: Session, draft: ScenarioDraft, config: dict[str, Any], *, replace_nodes: bool
) -> None:
    nodes, materials, fields, rules, sections = _draft_rows(db, draft.id)
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


def draft_config_view(db: Session, template: ScenarioTemplate) -> dict[str, Any]:
    """只读已有草稿；查看操作不创建草稿。"""
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
    if draft is None:
        raise BusinessError("SCENARIO_DRAFT_NOT_FOUND", "该场景没有未发布草稿", status_code=404)
    return draft_config_payload(db, draft)


def configuration_view(db: Session, template: ScenarioTemplate, view: str | None = None) -> dict[str, Any]:
    """编辑页读取当前配置：有草稿就读草稿，否则读发布版本；不创建草稿。"""
    if view == "DRAFT":
        return draft_config_view(db, template)
    if view is None:
        draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
        if draft is not None:
            return draft_config_payload(db, draft)
    version = find_latest_published_version(db, template.scenario_code)
    if version is None:
        if view == "PUBLISHED":
            raise BusinessError("SCENARIO_NOT_PUBLISHED", "该场景尚无已发布配置", status_code=404)
        return draft_config_view(db, template)
    config = manifest_module.editable_config_of(version.manifest_json, namespace=template.scenario_code)
    collections = []
    for section, config_key in (
        ("nodes", "node_instances"),
        ("materials", "materials"),
        ("fields", "fields"),
        ("rules", "rules"),
        ("sections", "sections"),
    ):
        items = config[config_key]
        if section == "rules":
            items = [item for item in items if not item["is_system"] and not item["rule_json"].get("calculation_ref")]
        collections.append([config_store.ConfigItem(version, section, {**item, "sort_no": index})
                            for index, item in enumerate(items, start=1)])
    return {
        "scenario_code": template.scenario_code, "configuration_kind": "PUBLISHED",
        "version_no": version.version_no, "revision_no": None,
        "source": version.source, "source_label": "已发布配置", "base_version_no": None,
        "schema_key": version.schema_key, "schema_version": version.schema_version,
        "schema_json": version.schema_json, "profile_version": version.manifest_json.get("profile_version"),
        "sync_error": None, "updated_at": version.published_at,
        **_configuration_items(*collections, compiled=version.manifest_json),
        "calculations": _calculation_view_rows(config["calculation_json"]),
    }


def draft_config_payload(db: Session, draft: ScenarioDraft) -> dict[str, Any]:
    nodes, materials, fields, rules, sections = _draft_rows(db, draft.id)
    compiled = compile_draft_manifest(db, template_or_404(db, draft.scenario_code), draft)
    return {
        "scenario_code": draft.scenario_code, "configuration_kind": "DRAFT", "version_no": None,
        "revision_no": draft.revision_no, "source": draft.source,
        "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
        "base_version_no": draft.base_version_no, "schema_key": draft.schema_key,
        "schema_version": draft.schema_version, "schema_json": draft.schema_json,
        "profile_version": draft.profile_version, "sync_error": None, "updated_at": draft.updated_at,
        **_configuration_items(nodes, materials, fields, rules, sections, compiled=compiled),
        "calculations": _calculation_view_rows(draft.calculation_json),
    }


def _configuration_items(nodes, materials, fields, rules, sections, *, compiled: dict[str, Any]) -> dict[str, Any]:
    """草稿与发布快照共用展示映射，发布内容直接使用其保存的编译结果。"""
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
    stored_keys = {item.rule_key for item in rules}
    for rule in compiled["rules"]:
        if rule["rule_key"] in stored_keys or rule.get("calculation_ref"):
            continue
        rule_items.append({**rule, "system": True,
                           "materials": [{"material_key": key, "material_name": material_names.get(key)} for key in rule.get("materials") or []],
                           "inputs": [{**entry, "field_name": field_map[entry["field_key"]].name if entry["field_key"] in field_map else None} for entry in rule.get("inputs") or []],
                           "outputs": [{"field_key": key, "field_name": field_map[key].name if key in field_map else None} for key in rule.get("outputs") or []],
                           "updated_at": None})
    data_names = {key: item.name for key, item in field_map.items()}
    data_names.update({item["rule_key"]: item["name"] for item in compiled["rules"]})
    data_names.update({item["result_code"]: item.get("rule_name") or item["rule_code"]
                       for item in compiled["calculations"]})
    section_items = [
        {
            "section_key": item.section_key,
            "title": item.title,
            "content_instruction": item.content_instruction,
            "field_keys": list(item.field_keys or []),
            "field_names": [field_map[key].name for key in (item.field_keys or []) if key in field_map],
            "data_refs": report_data_refs(item.data),
            "data_names": [data_names.get(ref, f"未明确的引用：{ref}")
                           for ref in report_data_refs(item.data)],
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
        # 规则执行记录的技术容器。
        "nodes": node_items,
        "materials": material_items,
        "fields": field_items,
        "rules": rule_items,
        "sections": section_items,
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
    if is_risk_rule({**definition, "operator": item.operator_code}):
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
        "enabled": active({**definition, "enabled": item.enabled}),
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
        "rule_role": "RISK" if is_risk_rule({**definition, "operator": item.operator_code}) else "CHECK",
        "source_rule_keys": list(definition.get("source_rule_keys") or []),
        "source_rule_codes": list(definition.get("source_rule_codes") or []),
        "config_status": definition.get("config_status"),
        "updated_at": item.updated_at,
    }


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
    draft.sync_error = None
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
            "sync_error": None,
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


def _calculation_view_rows(raw: Any) -> list[dict[str, Any]]:
    """按原始计算定义展示，不读取旧编译产物。"""
    candidates = calculation_store(raw)
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
        }
        for item in source
        if isinstance(item, dict)
    ]


def assess_publish(db: Session, template: ScenarioTemplate, draft: ScenarioDraft) -> dict[str, Any]:
    """发布只编译身份与配置结构；单条业务可执行性由运行结果表达。"""
    return compile_draft_manifest(db, template, draft)


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
    latest = find_latest_published_version(db, template.scenario_code)
    # 用不含编译时间的范围指纹比较配置内容，
    # 内容未变化时复用已发布版本。
    if (
        latest is not None
        and manifest_scope_hash(latest.manifest_json) == manifest_scope_hash(manifest)
        and latest.scenario_name == template.scenario_name
    ):
        return latest, True

    base_version = version_or_404(db, template.scenario_code, draft.base_version_no) if draft.base_version_no else None
    source = "REVISION" if base_version is not None else "FORM"
    import_job_id: int | None = None
    # 导入作业是可选来源信息；缺少来源不影响完整配置发布。
    if draft.source == "EXCEL":
        import_job_no = draft.config_json.get("import_job_no")
        import_job = db.scalar(select(ImportJob).where(ImportJob.job_no == import_job_no)) if import_job_no else None
        if import_job is None:
            logger.warning("发布配置没有可关联的导入作业 scenario_code=%s import_job_no=%s",
                           template.scenario_code, import_job_no)
        else:
            if import_job.scenario_code != template.scenario_code:
                raise BusinessError("DRAFT_IMPORT_PROVENANCE_INVALID", "导入作业不属于当前场景", status_code=409,
                                    details={"scenario_code": template.scenario_code, "import_job_no": import_job_no})
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
        manifest_json=manifest,
        published_at=datetime.now(UTC),
    )
    db.add(version)
    db.flush()
    for node in manifest["nodes"]:
        # 保存当前规则执行技术容器的投影。
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
            enabled=active(item.data),
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
        if previous_code != next_code and previous_code != field_key:
            # 内部字段键不随业务编号修改；只更新按原业务编号保存的引用。
            draft.calculation_json = remap_calculation_field_codes(
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
    calculations = calculations_referencing_field(
        draft.calculation_json, field_key,
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
    definition = {**(previous or {}), **payload, "name": name, "node": "RULE_EXECUTION", "node_type": "RULE_EXECUTION",
                  "operator": payload.get("operator") or (previous or {}).get("operator") or "LLM_EXECUTE",
                  "source_ref": _normalized_source_ref(payload.get("source_ref")), "enabled": bool(payload.get("enabled", True))}
    if payload.get("enabled") is True and definition.get("config_status") in {"PENDING", "DISABLED"}:
        definition["config_status"] = "CLEAR"
    return definition


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


def _section_group_fields(db: Session, draft_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """报告章节的分组属性：报告编号、报告名称、材料范围、输出格式、结果排序、
    章节顺序、内容类型与业务配置状态。

    只保存用户实际填写的属性，未声明的分组属性保持为空。
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
    data_refs = report_data_refs(payload)
    known_fields = {item.field_key for item in config_store.rows(db, ScenarioDraftField, draft.id)}
    field_keys = [ref for ref in data_refs if ref in known_fields]
    section_key = stable_key("sec", scenario_code, _item_ref())
    config_store.add(
        db, ScenarioDraftSection(
            draft_id=draft.id,
            section_key=section_key,
            title=title,
            content_instruction=str(payload.get("content_instruction") or ""),
            field_keys=field_keys,
            data_refs=data_refs,
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
    if "data_refs" in payload or "field_keys" in payload:
        item.data_refs = report_data_refs(payload)
        known_fields = {field.field_key for field in config_store.rows(db, ScenarioDraftField, draft.id)}
        item.field_keys = [ref for ref in item.data_refs if ref in known_fields]
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


def compile_draft_manifest(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
) -> dict[str, Any]:
    """把草稿编译成规则库快照，供发布、草稿导出与草稿视图使用。"""
    nodes, materials, fields, rules, sections = _draft_rows(db, draft.id)
    calculation_candidates = calculation_store(draft.calculation_json)
    # 配置行 → 编译入参的映射口径只有一份：domain.manifest.compile_inputs_of_config。
    # 方案保存走同一份，两条路径的编译产物因此必然同形。
    inputs = manifest_module.compile_inputs_of_config(
        materials=materials,
        fields=fields,
        rules=rules,
        sections=sections,
        nodes=nodes,
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
        import_proposals=draft.config_json.get("import_proposals"),
        source_kind="EXCEL" if draft.source == "EXCEL" else "FORM",
    )
    return manifest


def publish_draft(db: Session, scenario_code: str, payload: dict[str, Any]) -> tuple[ScenarioVersion, bool]:
    """把当前完整配置编译成不可变规则库版本。

    发布时由 `assess_publish` 编译当前配置；结构不符合执行接口则原样
    抛错，且在抛错前不创建 `ScenarioVersion`。

    expected_revision 必须与草稿当前修订一致，否则明确冲突（刷新后重试）；内容与最新已发布
    版本完全一致（同范围指纹、同名称）时返回该版本，不制造重复版本；版本号由 allocate_version
    的行锁与原子计数器在同一事务内分配；已发布版本不可修改，发布只新增版本。
    """
    template = template_or_404(db, scenario_code, lock=True)
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    if draft is None:
        raise BusinessError("SCENARIO_DRAFT_NOT_FOUND", "该场景没有未发布草稿", status_code=404)
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
    # 快照落库与草稿删除同一次提交；发布失败时草稿仍保留。
    db.delete(draft)
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
