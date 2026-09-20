"""审核场景的后台管理：基本资料、配置草稿、发布、版本与对比。

业务模型：
- 场景身份 = scenario_template（名称/业务类型/说明/启停/逻辑删除 + 版本计数器）。
- 配置草稿 = scenario_draft 及其材料/字段/规则/报告章节；每次写入递增 revision_no，
  发布必须携带当前修订号。
- 发布 = 用 compile_form_manifest 把草稿编译成与导入流程同构的 manifest，
  形成不可覆盖的 scenario_version（source=FORM/REVISION），并写入关系投影。
- 已发布版本不可修改；修改必须"基于此版创建修订"，在草稿上改完再发新版本。
- 页面来源不伪造 Excel 作业与工作表行号：import_job_id 为空，source 明确标为页面来源。

所有写操作都在同一事务内追加 operation_log；删除材料/字段前先返回引用清单，
不自动换对象、不级联删除规则。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import capabilities
from ..domain.manifest import (
    FORM_VALUE_TYPES,
    NODE_DEFINITIONS,
    OPERATOR_NODE_CONSTRAINTS,
    SOURCE_NODE_RULE_NODES,
    compile_form_manifest,
    detect_unsupported_capability,
    manifest_scope_hash,
    validate_rule_parameters,
)
from ..errors import SAFE_DETAIL_KEYS, BusinessError
from ..models import (
    AuditPlan,
    ScenarioDraft,
    ScenarioDraftField,
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
from .operation_log_service import OBJECT_SCENARIO, record_operation
from .template_registry_service import allocate_version

logger = logging.getLogger(__name__)

SYSTEM_OPERATORS = {"LLM_EXTRACT", "REPORT_RENDER"}
BUSINESS_OPERATORS = sorted(set(OPERATOR_NODE_CONSTRAINTS) - SYSTEM_OPERATORS)
# 草稿字段允许的数据类型：与 manifest 的表单契约共用同一份定义，
# 包含大写金额（03_字段抽取 的「数据类型」下拉本来就有这一项）。
DRAFT_VALUE_TYPES = FORM_VALUE_TYPES
# 字段的产物来源取值：当前只有印章识别一种。
FIELD_ARTIFACT_SOURCES = {"SEAL"}
DRAFT_SOURCE_LABELS = {"FORM": "页面编辑", "EXCEL": "规则库导入", "COPY": "复制场景", "REVISION": "版本修订"}
VERSION_SOURCE_LABELS = {
    "IMPORT": "规则库导入",
    "FORM": "页面编辑",
    "COPY": "复制场景",
    "REVISION": "版本修订",
}


def template_or_404(
    db: Session,
    scenario_code: str,
    *,
    lock: bool = False,
    include_deleted: bool = False,
) -> ScenarioTemplate:
    statement = select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code)
    if not include_deleted:
        statement = statement.where(ScenarioTemplate.deleted_at.is_(None))
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


# ---------------------------------------------------------------------------
# 草稿读写
# ---------------------------------------------------------------------------


def _latest_published_version(db: Session, scenario_code: str) -> ScenarioVersion | None:
    return db.scalar(
        select(ScenarioVersion)
        .where(ScenarioVersion.scenario_code == scenario_code, ScenarioVersion.status == "PUBLISHED")
        .order_by(ScenarioVersion.version_no.desc())
        .limit(1)
    )


def _draft_rows(db: Session, draft_id: int) -> tuple[list[ScenarioDraftMaterial], list[ScenarioDraftField], list[ScenarioDraftRule], list[ScenarioDraftSection]]:
    materials = db.scalars(
        select(ScenarioDraftMaterial).where(ScenarioDraftMaterial.draft_id == draft_id).order_by(ScenarioDraftMaterial.sort_no, ScenarioDraftMaterial.id)
    ).all()
    fields = db.scalars(
        select(ScenarioDraftField).where(ScenarioDraftField.draft_id == draft_id).order_by(ScenarioDraftField.sort_no, ScenarioDraftField.id)
    ).all()
    rules = db.scalars(
        select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft_id).order_by(ScenarioDraftRule.sort_no, ScenarioDraftRule.id)
    ).all()
    sections = db.scalars(
        select(ScenarioDraftSection).where(ScenarioDraftSection.draft_id == draft_id).order_by(ScenarioDraftSection.sort_no, ScenarioDraftSection.id)
    ).all()
    return list(materials), list(fields), list(rules), list(sections)


def ensure_draft(db: Session, template: ScenarioTemplate) -> ScenarioDraft:
    """取得场景草稿；不存在时创建。

    场景已有已发布版本时，新建草稿基于最新版本派生（source=REVISION，
    base_version_no 记录来源版本）——这是明确的“创建修订”，不会覆盖任何东西；
    没有已发布版本时创建空草稿（source=FORM）。
    """
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
    if draft is not None:
        return draft
    draft = ScenarioDraft(scenario_code=template.scenario_code, revision_no=1, source="FORM", base_version_no=None)
    db.add(draft)
    db.flush()
    latest = _latest_published_version(db, template.scenario_code)
    if latest is not None:
        draft.source = "REVISION"
        draft.base_version_no = latest.version_no
        _load_manifest_into_draft(db, draft, latest.manifest_json)
    else:
        # 没有可派生的已发布版本，这是一份空配置，必然编译不过。
        # 与 create_scenario 同一口径：配置视图里的“是否已生效”必须是真实状态。
        _try_publish_draft(db, template, draft)
    db.flush()
    return draft


def _item_ref() -> str:
    return public_no("ref")


def ensure_draft_from_published_version(db: Session, version: ScenarioVersion) -> ScenarioDraft | None:
    """导入发布后为尚无草稿的场景建立草稿（来源=EXCEL，基于本次发布的版本）。

    已有草稿（用户正在编辑的配置）保持不动：导入发布不会覆盖页面上的未发布编辑。
    """
    existing = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == version.scenario_code))
    if existing is not None:
        return None
    draft = ScenarioDraft(
        scenario_code=version.scenario_code,
        revision_no=1,
        source="EXCEL",
        base_version_no=version.version_no,
    )
    db.add(draft)
    db.flush()
    _load_manifest_into_draft(db, draft, version.manifest_json)
    db.flush()
    return draft


def _next_sort_no(db: Session, model: type, draft_id: int) -> int:
    current = db.scalar(select(func.max(model.sort_no)).where(model.draft_id == draft_id))
    return int(current or 0) + 1


def _load_manifest_into_draft(db: Session, draft: ScenarioDraft, manifest: dict[str, Any]) -> None:
    """用已发布版本的 manifest 替换草稿内容（创建修订 / 版本回填）。

    稳定键原样保留：方案对固定场景版本的规则选择依赖这些键。
    系统规则（字段抽取、报告生成）不进入草稿——它们在发布时按同一规则重新派生。

    业务配置事实必须完整带过来：产物来源、币种、报告分组属性、检查等级与风险成立条件
    都是人工维护的配置。少带一项，页面保存一次就会把它永久丢掉。
    """
    materials, fields, rules, sections = _draft_rows(db, draft.id)
    for row in [*materials, *fields, *rules, *sections]:
        db.delete(row)
    db.flush()
    # 草稿的来源格式版本随 manifest 一起带入：发布时按它导出与校验，
    # 不一律用默认版本给所有草稿打戳（那会把旧格式配置说成节点版格式）。
    profile_version = manifest.get("profile_version")
    if isinstance(profile_version, str) and profile_version:
        draft.profile_version = profile_version
    for index, material in enumerate(manifest.get("materials", []), start=1):
        db.add(
            ScenarioDraftMaterial(
                draft_id=draft.id,
                material_key=material["material_key"],
                name=material["name"],
                material_type=material.get("material_type", ""),
                required=bool(material.get("required")),
                description=str(material.get("description") or ""),
                # 来源业务编号随材料一起进入草稿：导出时要按它写回 Excel 的「材料编号」，
                # 丢掉它会让导出的文件缺少必填编号、无法重新导入。
                source_ref=material.get("source_ref"),
                sort_no=index,
            )
        )
    for index, field in enumerate(manifest.get("fields", []), start=1):
        db.add(
            ScenarioDraftField(
                draft_id=draft.id,
                field_key=field["field_key"],
                material_key=field["material_key"],
                name=field["name"],
                value_type=field["value_type"],
                unit=field.get("unit"),
                # 币种是金额比较与加总的口径之一：随字段一起进入草稿，不能只保留单位。
                currency=field.get("currency"),
                # 产物来源：主体核对据此确定字段取自哪种产物。草稿里必须保留，
                # 否则页面保存一次就会让主体核对规则不再被识别成主体核对。
                artifact_source=field.get("artifact_source"),
                required=bool(field.get("required")),
                instruction=str(field.get("instruction") or ""),
                source_ref=field.get("source_ref"),
                sort_no=index,
            )
        )
    for index, rule in enumerate(manifest.get("rules", []), start=1):
        if rule.get("operator") in SYSTEM_OPERATORS:
            continue
        applicability = rule.get("applicability") if isinstance(rule.get("applicability"), dict) else {}
        db.add(
            ScenarioDraftRule(
                draft_id=draft.id,
                rule_key=rule["rule_key"],
                name=rule["name"],
                node_code=rule["node"],
                operator_code=rule["operator"],
                enabled=bool(rule.get("enabled", True)),
                rule_json={
                    "name": rule["name"],
                    "node": rule["node"],
                    "operator": rule["operator"],
                    "enabled": bool(rule.get("enabled", True)),
                    "inputs": [
                        {"field_key": item["field_key"], "material_key": item["material_key"]}
                        for item in rule.get("inputs", [])
                    ],
                    "materials": list(rule.get("materials", [])),
                    "parameters": dict(rule.get("parameters") or {}),
                    "requirement": str(rule.get("requirement") or ""),
                    "applicability": {
                        "kind": applicability.get("kind", "ALWAYS"),
                        **(
                            {"material_keys": list(applicability.get("material_keys") or [])}
                            if applicability.get("material_keys")
                            else {}
                        ),
                    },
                    "source_node": rule.get("source_node"),
                    "source_rule_key": rule.get("source_rule_key"),
                    # 以下业务配置事实必须随草稿保留：它们是人工维护的审核口径，
                    # 页面表单不一定每一项都能编辑，整块覆盖 rule_json 时不能丢。
                    #   check_level / check_operator：检查等级与「合格要求」原文；
                    #   risk_level_value / risk_condition：风险等级原文与成立条件；
                    #   source_rule_refs / source_rule_keys：风险的多来源（任一 / 全部）；
                    #   config_status / quality_supported：业务配置状态与能力缺口标记。
                    **(
                        {"check_level": rule["check_level"]}
                        if rule.get("check_level") is not None
                        else {}
                    ),
                    **(
                        {"check_operator": rule["check_operator"]}
                        if rule.get("check_operator") is not None
                        else {}
                    ),
                    **(
                        {"risk_level_value": rule["risk_level_value"]}
                        if rule.get("risk_level_value") is not None
                        else {}
                    ),
                    **(
                        {"risk_condition": rule["risk_condition"]}
                        if rule.get("risk_condition") is not None
                        else {}
                    ),
                    **(
                        {"source_rule_keys": list(rule["source_rule_keys"])}
                        if rule.get("source_rule_keys")
                        else {}
                    ),
                    **(
                        {"config_status": rule["config_status"]}
                        if rule.get("config_status") is not None
                        else {}
                    ),
                    **(
                        {"quality_supported": rule["quality_supported"]}
                        if rule.get("quality_supported") is not None
                        else {}
                    ),
                },
                source_ref=rule.get("source_ref"),
                sort_no=index,
            )
        )
    for index, section in enumerate(manifest.get("report_template", []), start=1):
        db.add(
            ScenarioDraftSection(
                draft_id=draft.id,
                section_key=stable_key("sec", draft.scenario_code, section.get("section_name", f"section-{index}") + f"-{index}"),
                title=section.get("section_name", f"章节{index}"),
                content_instruction=str(section.get("content_instruction") or ""),
                field_keys=list(section.get("field_keys", [])),
                sort_no=index,
                # 节点版的报告分组属性：少了它们，页面保存一次就会把按编号分组的报告
                # 退回成旧的「附加展示章节」。
                report_code=section.get("report_code"),
                report_name=section.get("report_name"),
                material_keys=list(section.get("material_keys") or []) or None,
                formats=list(section.get("formats") or []) or None,
                section_order=section.get("section_order"),
                content_type=section.get("content_type"),
                result_sort=section.get("result_sort"),
                config_status=section.get("config_status"),
            )
        )
    db.flush()


def _bump_draft(draft: ScenarioDraft) -> None:
    draft.revision_no = (draft.revision_no or 0) + 1


def draft_config_view(db: Session, template: ScenarioTemplate) -> dict[str, Any]:
    draft = ensure_draft(db, template)
    # 首次访问创建草稿会落库：它是真实记录，刷新后仍然一致。
    db.commit()
    return _draft_config_payload(db, draft)


def _draft_config_payload(db: Session, draft: ScenarioDraft) -> dict[str, Any]:
    materials, fields, rules, sections = _draft_rows(db, draft.id)
    material_names = {item.material_key: item.name for item in materials}
    field_map = {item.field_key: item for item in fields}
    rule_names = {item.rule_key: item.name for item in rules}
    material_items = [
        {
            "material_key": item.material_key,
            "name": item.name,
            "material_type": item.material_type,
            "required": item.required,
            "description": item.description,
            "source_ref": item.source_ref,
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
            "unit": item.unit,
            # 币种与产物来源是金额口径与主体核对的配置事实，草稿视图必须一并返回，
            # 否则页面看不到、也改不了它们。
            "currency": item.currency,
            "artifact_source": item.artifact_source,
            "required": item.required,
            "instruction": item.instruction,
            "source_ref": item.source_ref,
            "sort_no": item.sort_no,
            "updated_at": item.updated_at,
        }
        for item in fields
    ]
    rule_items = [
        _draft_rule_view(item, material_names, field_map, rule_names) for item in rules
    ]
    # 系统规则（字段抽取、报告生成）按发布时的同一确定性规则派生，只读展示。
    for item in fields:
        material_name = material_names.get(item.material_key)
        rule_items.append(
            {
                "rule_key": stable_key("rule", draft.scenario_code, f"extract:{item.field_key}"),
                "name": f"抽取{item.name}",
                "node": "FIELD_EXTRACTION",
                "operator": "LLM_EXTRACT",
                "enabled": True,
                "system": True,
                "inputs": [],
                "materials": [{"material_key": item.material_key, "material_name": material_name}],
                "outputs": [{"field_key": item.field_key, "field_name": item.name, "material_key": item.material_key}],
                "parameters": {"instruction": item.instruction},
                "requirement": item.instruction,
                "applicability": {"kind": "ALWAYS"},
                "source_node": None,
                "source_rule_key": None,
                "source_rule_name": None,
                "risk_level": None,
                "risk_action": None,
                "requires_manual_review": False,
                "updated_at": item.updated_at,
            }
        )
    rule_items.append(
        {
            "rule_key": stable_key("rule", draft.scenario_code, "report:render"),
            "name": "生成审核报告",
            "node": "REPORT_OUTPUT",
            "operator": "REPORT_RENDER",
            "enabled": True,
            "system": True,
            "inputs": [],
            "materials": [],
            "outputs": [],
            "parameters": {},
            "requirement": "",
            "applicability": {"kind": "ALWAYS"},
            "source_node": None,
            "source_rule_key": None,
            "source_rule_name": None,
            "risk_level": None,
            "risk_action": None,
            "requires_manual_review": False,
            "updated_at": None,
        }
    )
    section_items = [
        {
            "section_key": item.section_key,
            "title": item.title,
            "content_instruction": item.content_instruction,
            "field_keys": list(item.field_keys or []),
            "field_names": [field_map[key].name for key in (item.field_keys or []) if key in field_map],
            "sort_no": item.sort_no,
            # 节点版的报告分组属性：页面据此按报告展示与维护，而不是把章节当成
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
            "config_status": item.config_status,
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
        # 草稿的来源格式版本：页面据此说明这份配置按哪种格式导出。
        "profile_version": draft.profile_version,
        # 当前配置是否已生效：NULL 表示已生效；非空时页面显示“尚未生效 + 原因”。
        # 编译未通过不阻断保存（建配置过程中必然不完整），但绝不能被当成已生效。
        "sync_error": draft.sync_error,
        "updated_at": draft.updated_at,
        "materials": material_items,
        "fields": field_items,
        "rules": rule_items,
        "sections": section_items,
        # 发布可用性提示：材料、字段、业务规则都齐备时才算“准备就绪”，
        # 但真正的发布校验仍以发布时的编译结果为准。
        "business_rule_count": len(rules),
        "system_rule_count": len(field_items) + 1,
    }


def _draft_rule_view(
    item: ScenarioDraftRule,
    material_names: dict[str, str],
    field_map: dict[str, ScenarioDraftField],
    rule_names: dict[str, str],
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
            for key in definition.get("materials", [])
        ],
        "outputs": [],
        "parameters": parameters,
        "requirement": str(definition.get("requirement") or ""),
        "applicability": {"kind": applicability.get("kind", "ALWAYS")},
        "source_node": definition.get("source_node"),
        "source_rule_key": definition.get("source_rule_key"),
        "source_rule_name": rule_names.get(definition.get("source_rule_key")) if definition.get("source_rule_key") else None,
        "risk_level": parameters.get("risk_level"),
        "risk_action": parameters.get("action"),
        "requires_manual_review": parameters.get("requires_manual_review") is True,
        "source_ref": item.source_ref,
        # 业务配置事实：页面要能显示检查等级、合格要求原文、风险等级原文、
        # 成立条件、多来源与配置状态，不能只给执行参数。
        "check_level": definition.get("check_level"),
        "check_operator": definition.get("check_operator"),
        "risk_level_value": definition.get("risk_level_value"),
        "risk_condition": definition.get("risk_condition"),
        "source_rule_keys": list(definition.get("source_rule_keys") or []),
        "config_status": definition.get("config_status"),
        "quality_supported": definition.get("quality_supported"),
        "updated_at": item.updated_at,
    }


def version_config_view(db: Session, scenario_code: str, version_no: int) -> dict[str, Any]:
    version = version_or_404(db, scenario_code, version_no)
    manifest = version.manifest_json
    material_names = {item["material_key"]: item["name"] for item in manifest.get("materials", [])}
    field_map = {item["field_key"]: item for item in manifest.get("fields", [])}
    rule_names = {item["rule_key"]: item["name"] for item in manifest.get("rules", [])}
    materials = [
        {
            "material_key": item["material_key"],
            "name": item["name"],
            "material_type": item.get("material_type", ""),
            "required": bool(item.get("required")),
            "description": str(item.get("description") or ""),
        }
        for item in manifest.get("materials", [])
    ]
    fields = [
        {
            "field_key": item["field_key"],
            "material_key": item["material_key"],
            "material_name": material_names.get(item["material_key"]),
            "name": item["name"],
            "value_type": item["value_type"],
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "required": bool(item.get("required")),
            "instruction": str(item.get("instruction") or ""),
        }
        for item in manifest.get("fields", [])
    ]
    rules = []
    for rule in manifest.get("rules", []):
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        applicability = rule.get("applicability") if isinstance(rule.get("applicability"), dict) else {}
        inputs = [
            {
                "field_key": entry.get("field_key"),
                "material_key": entry.get("material_key"),
                "field_name": field_map.get(entry.get("field_key"), {}).get("name"),
                "material_name": material_names.get(entry.get("material_key")),
                "value_type": field_map.get(entry.get("field_key"), {}).get("value_type"),
                "unit": field_map.get(entry.get("field_key"), {}).get("unit"),
                "currency": field_map.get(entry.get("field_key"), {}).get("currency"),
            }
            for entry in rule.get("inputs", [])
        ]
        rules.append(
            {
                "rule_key": rule["rule_key"],
                "name": rule["name"],
                "node": rule["node"],
                "operator": rule["operator"],
                "enabled": bool(rule.get("enabled", True)),
                "system": rule["operator"] in SYSTEM_OPERATORS,
                "inputs": inputs,
                "materials": [
                    {"material_key": key, "material_name": material_names.get(key)}
                    for key in rule.get("materials", [])
                ],
                "outputs": [
                    {
                        "field_key": key,
                        "field_name": field_map.get(key, {}).get("name"),
                        "material_key": field_map.get(key, {}).get("material_key"),
                    }
                    for key in rule.get("outputs", [])
                ],
                "parameters": parameters,
                "requirement": str(rule.get("requirement") or ""),
                "applicability": {"kind": applicability.get("kind", "ALWAYS"), "material_keys": list(applicability.get("material_keys", []))},
                "source_node": rule.get("source_node"),
                "source_rule_key": rule.get("source_rule_key"),
                "source_rule_name": rule_names.get(rule.get("source_rule_key")) if rule.get("source_rule_key") else None,
                "risk_level": parameters.get("risk_level"),
                "risk_action": parameters.get("action"),
                "requires_manual_review": parameters.get("requires_manual_review") is True,
        "source_ref": item.source_ref,
                "updated_at": None,
            }
        )
    sections = [
        {
            "section_key": f"v{version_no}-s{index}",
            "title": section.get("section_name", ""),
            "content_instruction": str(section.get("content_instruction") or ""),
            "field_keys": list(section.get("field_keys", [])),
            "field_names": [field_map[key]["name"] for key in section.get("field_keys", []) if key in field_map],
            "sort_no": index,
            "updated_at": None,
        }
        for index, section in enumerate(manifest.get("report_template", []), start=1)
    ]
    return {
        "scenario_code": version.scenario_code,
        "scenario_name": version.scenario_name,
        "version_no": version.version_no,
        "source": version.source,
        "source_label": VERSION_SOURCE_LABELS.get(version.source, version.source),
        "change_summary": version.change_summary,
        "published_at": version.published_at,
        "manifest_hash": version.manifest_hash,
        "unconfigured_check_nodes": list(manifest.get("unconfigured_check_nodes", [])),
        "capability_gaps": list(manifest.get("capability_gaps", [])),
        "materials": materials,
        "fields": fields,
        "rules": rules,
        "sections": sections,
        "business_rule_count": len([item for item in rules if item["operator"] not in SYSTEM_OPERATORS]),
        "system_rule_count": len([item for item in rules if item["operator"] in SYSTEM_OPERATORS]),
    }


# ---------------------------------------------------------------------------
# 场景基本资料
# ---------------------------------------------------------------------------


def list_scenarios_page(
    db: Session,
    *,
    name: str | None,
    category: str | None,
    enabled: bool | None,
    published_from: datetime | None,
    published_to: datetime | None,
    deleted: bool,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    conditions = [
        ScenarioTemplate.deleted_at.is_not(None) if deleted else ScenarioTemplate.deleted_at.is_(None)
    ]
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
    latest_versions: dict[str, ScenarioVersion] = {}
    draft_by_code: dict[str, ScenarioDraft] = {}
    draft_counts: dict[str, dict[str, int]] = {}
    if codes:
        for version in db.scalars(
            select(ScenarioVersion)
            .where(ScenarioVersion.scenario_code.in_(codes), ScenarioVersion.status == "PUBLISHED")
            .order_by(ScenarioVersion.version_no)
        ).all():
            latest_versions[version.scenario_code] = version
        for draft in db.scalars(select(ScenarioDraft).where(ScenarioDraft.scenario_code.in_(codes))).all():
            draft_by_code[draft.scenario_code] = draft
        draft_ids = [draft.id for draft in draft_by_code.values()]
        if draft_ids:
            material_counts = dict(
                db.execute(
                    select(ScenarioDraftMaterial.draft_id, func.count())
                    .where(ScenarioDraftMaterial.draft_id.in_(draft_ids))
                    .group_by(ScenarioDraftMaterial.draft_id)
                ).all()
            )
            field_counts = dict(
                db.execute(
                    select(ScenarioDraftField.draft_id, func.count())
                    .where(ScenarioDraftField.draft_id.in_(draft_ids))
                    .group_by(ScenarioDraftField.draft_id)
                ).all()
            )
            rule_counts = dict(
                db.execute(
                    select(ScenarioDraftRule.draft_id, func.count())
                    .where(ScenarioDraftRule.draft_id.in_(draft_ids))
                    .group_by(ScenarioDraftRule.draft_id)
                ).all()
            )
            for scenario_code, draft in draft_by_code.items():
                draft_counts[scenario_code] = {
                    "material": int(material_counts.get(draft.id, 0)),
                    "field": int(field_counts.get(draft.id, 0)),
                    "rule": int(rule_counts.get(draft.id, 0)),
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
                "deleted": template.deleted_at is not None,
                "published_version_no": latest.version_no if latest else None,
                "published_at": latest.published_at if latest else None,
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


def list_scenario_options(db: Session) -> list[dict[str, Any]]:
    """下拉选项：未删除的场景及其实时状态（用于方案、任务等选择器）。"""
    templates = db.scalars(
        select(ScenarioTemplate).where(ScenarioTemplate.deleted_at.is_(None)).order_by(ScenarioTemplate.scenario_name)
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
        }
        for item in templates
    ]


def create_scenario(db: Session, payload: dict[str, Any]) -> ScenarioTemplate:
    name = str(payload.get("scenario_name") or "").strip()
    if not name:
        raise BusinessError("SCENARIO_NAME_REQUIRED", "场景名称不能为空", status_code=400)
    scenario_code = public_no("SCN")
    template = ScenarioTemplate(
        scenario_code=scenario_code,
        scenario_name=name,
        category=(str(payload.get("category")).strip() or None) if payload.get("category") else None,
        description=(str(payload.get("description")).strip() or None) if payload.get("description") else None,
        enabled=bool(payload.get("enabled", True)),
        latest_version_no=0,
    )
    db.add(template)
    db.flush()
    draft = ScenarioDraft(scenario_code=scenario_code, revision_no=0, source="FORM", base_version_no=None)
    db.add(draft)
    db.flush()
    # 新建场景是空配置，必然编译不过（没有材料位）。这里就写下真实的未生效原因，
    # 否则页面会把一个空场景谎报成“配置已生效”。
    _try_publish_draft(db, template, draft)
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
    template = template_or_404(db, scenario_code, lock=True)
    if "scenario_name" in payload and payload["scenario_name"] is not None:
        name = str(payload["scenario_name"]).strip()
        if not name:
            raise BusinessError("SCENARIO_NAME_REQUIRED", "场景名称不能为空", status_code=400)
        template.scenario_name = name
    if "category" in payload:
        category = payload.get("category")
        template.category = (str(category).strip() or None) if category is not None else None
    if "description" in payload:
        description = payload.get("description")
        template.description = (str(description).strip() or None) if description is not None else None
    if "enabled" in payload and payload["enabled"] is not None:
        template.enabled = bool(payload["enabled"])
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=template.scenario_name,
        operation_type="UPDATE",
        detail={"scenario_code": scenario_code},
    )
    db.commit()
    db.refresh(template)
    return template


def set_scenario_enabled(db: Session, scenario_code: str, enabled: bool) -> ScenarioTemplate:
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


def delete_scenario(db: Session, scenario_code: str) -> ScenarioTemplate:
    template = template_or_404(db, scenario_code, lock=True)
    if template.deleted_at is not None:
        raise BusinessError("SCENARIO_ALREADY_DELETED", "该场景已在已删除列表中", status_code=409)
    template.deleted_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=template.scenario_name,
        operation_type="DELETE",
        detail={"scenario_code": scenario_code},
    )
    db.commit()
    db.refresh(template)
    return template


def restore_scenario(db: Session, scenario_code: str) -> ScenarioTemplate:
    template = template_or_404(db, scenario_code, lock=True, include_deleted=True)
    if template.deleted_at is None:
        raise BusinessError("SCENARIO_NOT_DELETED", "该场景未被删除，无需恢复", status_code=409)
    template.deleted_at = None
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=scenario_code,
        object_name=template.scenario_name,
        operation_type="RESTORE",
        detail={"scenario_code": scenario_code},
    )
    db.commit()
    db.refresh(template)
    return template


def copy_scenario(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioTemplate:
    """复制场景：基本资料 + 当前草稿配置。

    新场景使用全新的稳定键（不是复制键），所有内部引用（字段→材料、规则输入、
    风险来源、报告章节字段）同步重映射；已发布版本不复制。
    """
    source = template_or_404(db, scenario_code)
    name = str(payload.get("scenario_name") or "").strip() or f"{source.scenario_name} 副本"
    new_code = public_no("SCN")
    template = ScenarioTemplate(
        scenario_code=new_code,
        scenario_name=name,
        category=source.category,
        description=source.description,
        enabled=True,
        latest_version_no=0,
    )
    db.add(template)
    db.flush()
    source_draft = ensure_draft(db, source)
    materials, fields, rules, sections = _draft_rows(db, source_draft.id)
    draft = ScenarioDraft(
        scenario_code=new_code,
        revision_no=1,
        source="COPY",
        base_version_no=None,
        # 复制保留来源格式版本：复制出来的是同一份业务配置，不因复制换一种导出格式。
        profile_version=source_draft.profile_version,
    )
    db.add(draft)
    db.flush()
    material_key_map: dict[str, str] = {}
    field_key_map: dict[str, str] = {}
    rule_key_map: dict[str, str] = {}
    for item in materials:
        new_key = stable_key("mat", new_code, _item_ref())
        material_key_map[item.material_key] = new_key
        db.add(
            ScenarioDraftMaterial(
                draft_id=draft.id,
                material_key=new_key,
                name=item.name,
                material_type=item.material_type,
                required=item.required,
                description=item.description,
                source_ref=item.source_ref,
                sort_no=item.sort_no,
            )
        )
    for item in fields:
        new_key = stable_key("fld", new_code, _item_ref())
        field_key_map[item.field_key] = new_key
        db.add(
            ScenarioDraftField(
                draft_id=draft.id,
                field_key=new_key,
                material_key=material_key_map[item.material_key],
                name=item.name,
                value_type=item.value_type,
                unit=item.unit,
                currency=item.currency,
                artifact_source=item.artifact_source,
                required=item.required,
                instruction=item.instruction,
                source_ref=item.source_ref,
                sort_no=item.sort_no,
            )
        )
    for item in rules:
        new_key = stable_key("rule", new_code, _item_ref())
        rule_key_map[item.rule_key] = new_key
    for item in rules:
        definition = dict(item.rule_json or {})
        new_definition = {
            **definition,
            "inputs": [
                {
                    "field_key": field_key_map.get(entry.get("field_key"), entry.get("field_key")),
                    "material_key": material_key_map.get(entry.get("material_key"), entry.get("material_key")),
                }
                for entry in definition.get("inputs", [])
            ],
            "materials": [material_key_map.get(key, key) for key in definition.get("materials", [])],
            "source_rule_key": rule_key_map.get(definition.get("source_rule_key")) if definition.get("source_rule_key") else None,
        }
        db.add(
            ScenarioDraftRule(
                draft_id=draft.id,
                rule_key=rule_key_map[item.rule_key],
                name=item.name,
                node_code=item.node_code,
                operator_code=item.operator_code,
                enabled=item.enabled,
                rule_json=new_definition,
                source_ref=item.source_ref,
                sort_no=item.sort_no,
            )
        )
    for item in sections:
        db.add(
            ScenarioDraftSection(
                draft_id=draft.id,
                section_key=stable_key("sec", new_code, _item_ref()),
                title=item.title,
                content_instruction=item.content_instruction,
                field_keys=[field_key_map.get(key, key) for key in (item.field_keys or [])],
                sort_no=item.sort_no,
                # 报告分组属性随复制一起保留，材料范围按新材料键重映射。
                report_code=item.report_code,
                report_name=item.report_name,
                material_keys=[material_key_map.get(key, key) for key in (item.material_keys or [])] or None,
                formats=list(item.formats or []) or None,
                section_order=item.section_order,
                content_type=item.content_type,
                result_sort=item.result_sort,
                config_status=item.config_status,
            )
        )
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=new_code,
        object_name=name,
        operation_type="COPY",
        detail={"scenario_code": new_code, "source_scenario_code": scenario_code},
    )
    # 复制出来的是一份完整可用的配置（材料/字段/规则/章节都按新键重映射过），
    # 因此直接同步成新场景的已发布版本，新场景立即可用于建方案。
    # 若源草稿本身未生效（编译不过），复制结果同样不生效并带上同一原因。
    _try_publish_draft(db, template, draft)
    db.commit()
    db.refresh(template)
    return template


def scenario_detail(db: Session, scenario_code: str) -> dict[str, Any]:
    template = template_or_404(db, scenario_code, include_deleted=True)
    versions = db.scalars(
        select(ScenarioVersion)
        .where(ScenarioVersion.scenario_code == scenario_code)
        .order_by(ScenarioVersion.version_no.desc())
    ).all()
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    plan_count = db.scalar(
        select(func.count()).select_from(AuditPlan).where(AuditPlan.scenario_code == scenario_code, AuditPlan.deleted_at.is_(None))
    ) or 0
    return {
        "scenario_code": template.scenario_code,
        "scenario_name": template.scenario_name,
        "category": template.category,
        "description": template.description,
        "enabled": template.enabled,
        "deleted": template.deleted_at is not None,
        "latest_version_no": template.latest_version_no,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
        "plan_count": int(plan_count),
        "draft": {
            "revision_no": draft.revision_no,
            "source": draft.source,
            "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
            "base_version_no": draft.base_version_no,
            # 当前配置是否已生效：页面据此显示“尚未生效 + 原因”。
            "sync_error": draft.sync_error,
            "updated_at": draft.updated_at,
        }
        if draft
        else None,
        "versions": [
            {
                "version_no": item.version_no,
                "status": item.status,
                "source": item.source,
                "source_label": VERSION_SOURCE_LABELS.get(item.source, item.source),
                "change_summary": item.change_summary,
                "published_at": item.published_at,
                "manifest_hash": item.manifest_hash,
                "material_count": len(item.manifest_json.get("materials", [])),
                "field_count": len(item.manifest_json.get("fields", [])),
                "rule_count": len([rule for rule in item.manifest_json.get("rules", []) if rule.get("operator") not in SYSTEM_OPERATORS]),
            }
            for item in versions
        ],
    }


# ---------------------------------------------------------------------------
# 草稿：材料 / 字段 / 规则 / 报告章节
# ---------------------------------------------------------------------------


def _locked_draft(db: Session, scenario_code: str) -> tuple[ScenarioTemplate, ScenarioDraft]:
    template = template_or_404(db, scenario_code, lock=True)
    draft = ensure_draft(db, template)
    return template, draft


def _sync_published_version(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    *,
    operation_type: str,
    object_name: str,
    detail: dict[str, Any],
) -> ScenarioDraft:
    """草稿写入后的统一收口：记录操作、递增修订，并把配置同步成新的已发布版本。

    场景配置只有一份（页面上没有“草稿 / 版本”的概念）：每次保存都要让已发布版本
    追上草稿，方案与任务才能引用到最新配置。

    保存必须永远成功，因此编译失败不在这里抛错：建场景过程中配置必然不完整
    （加了第一个材料时还没有规则），抛错会让用户根本建不起来。此时草稿照常落库，
    但不推进已发布版本——上一个可用版本继续供方案与任务引用，页面据
    draft_config_view 里的 sync_error 显示「当前配置尚未生效 + 具体原因」。
    这是显式状态，不是静默降级：原因与缺失项都会原样返回给页面。
    """
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
    if not _try_publish_draft(db, template, draft):
        # 编译未通过：草稿已落库，但已发布版本保持不变。
        db.commit()
        return draft
    db.commit()
    return draft


def _try_publish_draft(db: Session, template: ScenarioTemplate, draft: ScenarioDraft) -> bool:
    """尝试把草稿编译并同步成新的已发布版本。

    返回 True 表示配置已生效（编译通过并写出版本，或内容未变而复用现有版本）；
    返回 False 表示配置不完整、尚未生效，具体原因记在当前配置视图的 sync_error 里。
    编译失败不抛给调用方：保存必须成功，未生效是一个可展示的状态。
    """
    try:
        manifest = compile_draft_manifest(db, template, draft)
    except BusinessError as failure:
        logger.info(
            "场景配置尚未生效：scenario_code=%s code=%s message=%s",
            template.scenario_code,
            failure.code,
            failure.message,
        )
        draft.sync_error = _sync_error_detail(failure)
        return False
    draft.sync_error = None
    _publish_manifest(db, template, draft, manifest, change_summary=None)
    return True


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
    version_no = allocate_version(db, template.scenario_code, template.scenario_name)
    version = ScenarioVersion(
        import_job_id=None,
        scenario_code=template.scenario_code,
        scenario_name=template.scenario_name,
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
                material_type=material["material_type"],
                required=material["required"],
                source_json=material["source"],
            )
        )
    for node in manifest["nodes"]:
        db.add(ScenarioNode(scenario_version_id=version.id, **node))
    for rule in manifest["rules"]:
        db.add(
            ScenarioRule(
                scenario_version_id=version.id,
                rule_key=rule["rule_key"],
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
    template, draft = _locked_draft(db, scenario_code)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
    material_key = stable_key("mat", scenario_code, _item_ref())
    db.add(
        ScenarioDraftMaterial(
            draft_id=draft.id,
            material_key=material_key,
            name=name,
            material_type=str(payload.get("material_type") or "").strip(),
            required=bool(payload.get("required", True)),
            description=str(payload.get("description") or ""),
            source_ref=_normalized_source_ref(payload.get("source_ref")),
            sort_no=_next_sort_no(db, ScenarioDraftMaterial, draft.id),
        )
    )
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_CREATE",
        object_name=name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def _draft_material_or_404(db: Session, draft_id: int, material_key: str) -> ScenarioDraftMaterial:
    item = db.scalar(
        select(ScenarioDraftMaterial).where(
            ScenarioDraftMaterial.draft_id == draft_id,
            ScenarioDraftMaterial.material_key == material_key,
        )
    )
    if not item:
        raise BusinessError("DRAFT_MATERIAL_NOT_FOUND", "材料配置不存在", status_code=404, details={"material_key": material_key})
    return item


def update_draft_material(db: Session, scenario_code: str, material_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_material_or_404(db, draft.id, material_key)
    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
        item.name = name
    if "material_type" in payload and payload["material_type"] is not None:
        item.material_type = str(payload["material_type"]).strip()
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "description" in payload and payload["description"] is not None:
        item.description = str(payload["description"])
    if "source_ref" in payload:
        item.source_ref = _normalized_source_ref(payload.get("source_ref"))
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_UPDATE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def delete_draft_material(db: Session, scenario_code: str, material_key: str) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_material_or_404(db, draft.id, material_key)
    fields = db.scalars(
        select(ScenarioDraftField).where(ScenarioDraftField.draft_id == draft.id, ScenarioDraftField.material_key == material_key)
    ).all()
    rules = db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all()
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
    db.delete(item)
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def create_draft_field(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
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
    field_key = stable_key("fld", scenario_code, _item_ref())
    db.add(
        ScenarioDraftField(
            draft_id=draft.id,
            field_key=field_key,
            material_key=material_key,
            name=name,
            value_type=value_type,
            unit=(str(payload["unit"]).strip() or None) if payload.get("unit") else None,
            currency=(str(payload["currency"]).strip() or None) if payload.get("currency") else None,
            artifact_source=(
                str(payload["artifact_source"]).strip() or None
            )
            if payload.get("artifact_source")
            else None,
            required=bool(payload.get("required", False)),
            instruction=str(payload.get("instruction") or ""),
            source_ref=_normalized_source_ref(payload.get("source_ref")),
            sort_no=_next_sort_no(db, ScenarioDraftField, draft.id),
        )
    )
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_CREATE",
        object_name=name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


def _draft_field_or_404(db: Session, draft_id: int, field_key: str) -> ScenarioDraftField:
    item = db.scalar(
        select(ScenarioDraftField).where(ScenarioDraftField.draft_id == draft_id, ScenarioDraftField.field_key == field_key)
    )
    if not item:
        raise BusinessError("DRAFT_FIELD_NOT_FOUND", "字段配置不存在", status_code=404, details={"field_key": field_key})
    return item


def _rules_referencing_field(db: Session, draft_id: int, field_key: str) -> list[str]:
    rules = db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft_id)).all()
    return [
        rule.name
        for rule in rules
        if any(entry.get("field_key") == field_key for entry in (rule.rule_json or {}).get("inputs", []))
    ]


def update_draft_field(db: Session, scenario_code: str, field_key: str, payload: dict[str, Any]) -> ScenarioDraft:
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
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "instruction" in payload and payload["instruction"] is not None:
        item.instruction = str(payload["instruction"])
    if "source_ref" in payload:
        item.source_ref = _normalized_source_ref(payload.get("source_ref"))
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_UPDATE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


def delete_draft_field(db: Session, scenario_code: str, field_key: str) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_field_or_404(db, draft.id, field_key)
    referencing = _rules_referencing_field(db, draft.id, field_key)
    sections = db.scalars(select(ScenarioDraftSection).where(ScenarioDraftSection.draft_id == draft.id)).all()
    referencing_sections = [section.title for section in sections if field_key in (section.field_keys or [])]
    if referencing or referencing_sections:
        raise BusinessError(
            "FIELD_REFERENCED",
            "该字段仍被以下规则或报告章节引用，请先移除引用后再删除",
            status_code=409,
            details={"field_name": item.name, "rule_keys": referencing, "section_names": referencing_sections},
        )
    db.delete(item)
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


# ---------------------------------------------------------------------------
# 草稿：规则
# ---------------------------------------------------------------------------


def _normalize_rule_payload(
    db: Session,
    draft: ScenarioDraft,
    payload: dict[str, Any],
    *,
    existing_key: str | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把页面提交的规则定义规范化为草稿存储结构，并做逐项校验。

    这里校验的是“这条规则自身”的引用完整性与参数合法性；发布时还会用
    compile_form_manifest 做一次全量编译校验（同一套参数契约、能力判定与类型检查）。

    `previous` 是该规则草稿里已有的定义。页面表单只呈现它支持的字段，因此保存时
    必须把表单不呈现、但属于业务配置事实的键原样保留（检查等级、合格要求原文、
    风险等级原文与成立条件、风险多来源、配置状态）：整块替换 rule_json 会把它们
    永久丢掉，打开既有规则再保存就改变了审核条件。
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("RULE_NAME_REQUIRED", "规则名称不能为空", status_code=400)
    node = str(payload.get("node") or "")
    operator = str(payload.get("operator") or "")
    if operator in SYSTEM_OPERATORS:
        raise BusinessError("RULE_SYSTEM_READONLY", "字段抽取与报告生成规则由系统自动派生，不能手工创建", status_code=409)
    if operator not in OPERATOR_NODE_CONSTRAINTS or node not in OPERATOR_NODE_CONSTRAINTS[operator]:
        raise BusinessError(
            "RULE_OPERATOR_NODE_INVALID",
            "检查方式不能在所选阶段执行",
            status_code=400,
            details={"operator": operator, "node": node},
        )
    materials = db.scalars(select(ScenarioDraftMaterial).where(ScenarioDraftMaterial.draft_id == draft.id)).all()
    material_keys = {item.material_key for item in materials}
    fields = db.scalars(select(ScenarioDraftField).where(ScenarioDraftField.draft_id == draft.id)).all()
    field_map = {item.field_key: item for item in fields}

    inputs: list[dict[str, str]] = []
    for entry in payload.get("inputs") or []:
        field_key = str(entry.get("field_key") or "")
        material_key = str(entry.get("material_key") or "")
        field = field_map.get(field_key)
        if field is None:
            raise BusinessError("RULE_INPUT_FIELD_MISSING", "规则引用的字段不存在", status_code=400, details={"field_key": field_key})
        if field.material_key != material_key:
            raise BusinessError(
                "RULE_INPUT_MATERIAL_MISMATCH",
                "规则输入的所属材料与字段的实际材料不一致",
                status_code=400,
                details={"field_name": field.name, "material_key": material_key},
            )
        inputs.append({"field_key": field_key, "material_key": material_key})

    scope: list[str] = []
    for material_key in payload.get("materials") or []:
        if material_key not in material_keys:
            raise BusinessError("RULE_MATERIAL_MISSING", "规则材料范围中的材料不存在", status_code=400, details={"material_key": material_key})
        if material_key not in scope:
            scope.append(material_key)

    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    parameters = dict(parameters)
    validate_rule_parameters(name, operator, parameters)
    unsupported = detect_unsupported_capability(payload.get("requirement"), parameters.get("instruction"))
    if unsupported:
        raise BusinessError(
            "RULE_CAPABILITY_UNSUPPORTED",
            f"当前系统不支持：{unsupported[2]}（命中“{unsupported[1]}”），请改为可支持的检查方式",
            status_code=400,
            details={"rule": name, "capability": unsupported[0], "matched": unsupported[1]},
        )

    if operator in {"MATERIAL_EXISTS", "FILE_QUALITY_CHECK"} and not scope:
        raise BusinessError("RULE_MATERIAL_REQUIRED", "材料规则必须选择适用材料", status_code=400, details={"rule": name})
    comparison_operators = {"FIELD_EQUAL", "FIELD_NOT_EQUAL", "DATE_COMPARE", "DECIMAL_COMPARE"}
    if operator in comparison_operators:
        if len(inputs) != 2:
            raise BusinessError("RULE_INPUT_COUNT_INVALID", "比较规则必须恰好选择两个输入字段", status_code=400, details={"rule": name})
        if node == "INTERNAL_VALIDATE" and inputs[0]["material_key"] != inputs[1]["material_key"]:
            raise BusinessError("RULE_INTERNAL_MATERIAL_MISMATCH", "文件内部校验的左右字段必须属于同一份材料", status_code=400, details={"rule": name})
        input_types = [field_map[item["field_key"]].value_type for item in inputs]
        if operator == "DATE_COMPARE" and any(value_type != "DATE" for value_type in input_types):
            raise BusinessError("RULE_TYPE_INVALID", "日期比较只能选择日期类型字段", status_code=400, details={"rule": name})
        if operator == "DECIMAL_COMPARE" and any(value_type != "DECIMAL" for value_type in input_types):
            raise BusinessError("RULE_TYPE_INVALID", "金额比较只能选择金额类型字段", status_code=400, details={"rule": name})
        if operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"} and input_types[0] != input_types[1]:
            raise BusinessError("RULE_TYPE_INVALID", "字段比较的两个字段类型必须一致", status_code=400, details={"rule": name})
        left_unit = (field_map[inputs[0]["field_key"]].unit or "").strip()
        right_unit = (field_map[inputs[1]["field_key"]].unit or "").strip()
        if left_unit and right_unit and left_unit != right_unit:
            raise BusinessError(
                "RULE_UNIT_MISMATCH",
                f"比较双方单位不一致（{left_unit} / {right_unit}），系统不做单位换算，请先统一单位或改用智能判断",
                status_code=400,
                details={"rule": name},
            )
    if operator == "LLM_VALIDATE":
        if not inputs:
            raise BusinessError("RULE_INPUT_COUNT_INVALID", "智能判断必须选择至少一个输入字段", status_code=400, details={"rule": name})
        if node == "INTERNAL_VALIDATE" and len(inputs) > 1:
            raise BusinessError("RULE_INPUT_COUNT_INVALID", "文件内部校验的智能判断最多只能引用一个字段", status_code=400, details={"rule": name})
    if node == "CROSS_VALIDATE" and len(inputs) != 2:
        raise BusinessError("RULE_INPUT_COUNT_INVALID", "文件交叉校验必须左右各引用一个字段", status_code=400, details={"rule": name})

    applicability_raw = payload.get("applicability") if isinstance(payload.get("applicability"), dict) else {}
    applicability_kind = str(applicability_raw.get("kind") or "ALWAYS")
    if applicability_kind not in {"ALWAYS", "MATERIALS_UPLOADED"}:
        raise BusinessError("RULE_APPLICABILITY_INVALID", "适用条件不受支持", status_code=400, details={"kind": applicability_kind})

    source_rule_key: str | None = None
    source_node: str | None = None
    if operator == "RISK_TRIGGER":
        source_rule_key = str(payload.get("source_rule_key") or "")
        source_node = str(payload.get("source_node") or "")
        if not source_rule_key or source_rule_key == existing_key:
            raise BusinessError("RULE_RISK_SOURCE_REQUIRED", "风险规则必须选择来源规则", status_code=400, details={"rule": name})
        if source_node not in SOURCE_NODE_RULE_NODES:
            raise BusinessError("RULE_RISK_SOURCE_NODE_REQUIRED", "风险规则必须选择来源节点", status_code=400, details={"rule": name})
        sibling = db.scalar(
            select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id, ScenarioDraftRule.rule_key == source_rule_key)
        )
        if sibling is None:
            raise BusinessError("RULE_RISK_SOURCE_MISSING", "风险规则的来源规则不存在", status_code=400, details={"rule": name})
        if sibling.operator_code == "RISK_TRIGGER":
            raise BusinessError("RULE_RISK_SOURCE_INVALID", "风险规则的来源规则不能是另一条风险规则", status_code=400, details={"rule": name})
        node_positions = {code: index for index, (code, _) in enumerate(NODE_DEFINITIONS)}
        if node_positions.get(node, 0) <= node_positions.get(sibling.node_code, 0):
            raise BusinessError("RULE_NODE_ORDER_INVALID", "风险规则必须位于来源规则之后", status_code=400, details={"rule": name})
        if sibling.node_code not in SOURCE_NODE_RULE_NODES[source_node]:
            raise BusinessError(
                "RULE_RISK_SOURCE_NODE_MISMATCH",
                "来源规则不在所选的来源节点中，请核对来源节点",
                status_code=400,
                details={"rule": name, "source_node": source_node},
            )
    definition = {
        "name": name,
        "node": node,
        "operator": operator,
        "enabled": bool(payload.get("enabled", True)),
        "inputs": inputs,
        "materials": scope,
        "parameters": parameters,
        "requirement": str(payload.get("requirement") or ""),
        "applicability": {"kind": applicability_kind},
        "source_node": source_node,
        "source_rule_key": source_rule_key,
    }
    definition = _preserve_untouched_rule_keys(definition, previous, operator=operator)
    source_ref = _normalized_source_ref(payload.get("source_ref"))
    if source_ref is not None:
        definition["source_ref"] = source_ref
    elif isinstance(previous, dict) and previous.get("source_ref"):
        # 页面表单不呈现编号；本次没提交时保留既有编号，不让它被清空。
        definition["source_ref"] = previous["source_ref"]
    return definition


def _normalized_source_ref(value: Any) -> str | None:
    """业务编号：留空表示页面新建、没有来源编号，不伪造一个。"""
    text = str(value or "").strip()
    return text or None


# 页面表单不呈现、但属于业务配置事实的键：它们只能由导入、人工裁决或发布编译产生。
# 保存草稿时原样保留，不因为它们不在本次提交里就当作“用户清空了该配置”。
PRESERVED_RULE_KEYS = (
    "check_level",
    "check_operator",
    "risk_level_value",
    "risk_condition",
    "source_rule_keys",
    "config_status",
    "quality_supported",
)


def _preserve_untouched_rule_keys(
    definition: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    operator: str | None = None,
) -> dict[str, Any]:
    """把本次提交未覆盖的业务配置事实从既有草稿定义里带过来。

    参数合并按「谁拥有这个键」区分，不能一律 merged.update：

      - **表单拥有的键**（方向、允许差值、差值单位、抽取要求等）：本次提交就是权威取值。
        提交里没有这个键，表示用户把它清空了——必须真的删掉。
        若在这里用旧值兜底，「清空允许差值」会变成界面显示已清空、执行仍用旧容差。
      - **表单不呈现的键**（文件检查的检查项目、合格要求、标准值、单位、检查口径）：
        它们只能由导入或人工裁决产生，页面保存时原样保留。
    """
    if not isinstance(previous, dict):
        return definition
    for key in PRESERVED_RULE_KEYS:
        if key in definition:
            continue
        if key in previous:
            definition[key] = previous[key]
    previous_parameters = previous.get("parameters")
    submitted_parameters = definition.get("parameters") or {}
    if isinstance(previous_parameters, dict):
        owned = _form_owned_parameter_keys(operator or str(definition.get("operator") or ""))
        merged = {
            key: value
            for key, value in previous_parameters.items()
            # 表单拥有的键一律以本次提交为准：没提交就等于清空，不保留旧值。
            if key not in owned
        }
        merged.update(submitted_parameters)
        definition["parameters"] = merged
    return definition


# 页面表单**直接呈现并可清空**的参数键。它们的权威取值来自本次提交：
# 提交里缺失即表示清空，后端不得用旧值兜底。
FORM_OWNED_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    "DATE_COMPARE": frozenset({"operator", "threshold", "threshold_unit"}),
    "DECIMAL_COMPARE": frozenset({"operator", "threshold", "threshold_unit"}),
    "DECIMAL_SUM_COMPARE": frozenset({"operator", "threshold", "threshold_unit"}),
    "LLM_VALIDATE": frozenset({"instruction"}),
    "FILE_QUALITY_CHECK": frozenset({"artifact_type"}),
    "RISK_TRIGGER": frozenset({"risk_level", "action"}),
}
# 所有比较类算子的表单都含「人工复核」开关。
FORM_OWNED_COMMON_PARAMETER_KEYS = frozenset({"requires_manual_review"})


def _form_owned_parameter_keys(operator: str) -> frozenset[str]:
    return FORM_OWNED_PARAMETER_KEYS.get(operator, frozenset()) | FORM_OWNED_COMMON_PARAMETER_KEYS
    return definition


def create_draft_rule(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    definition = _normalize_rule_payload(db, draft, payload, existing_key=None)
    rule_key = stable_key("rule", scenario_code, _item_ref())
    db.add(
        ScenarioDraftRule(
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
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_CREATE",
        object_name=definition["name"],
        detail={"scenario_code": scenario_code, "rule_key": rule_key},
    )
    return draft


def _draft_rule_or_404(db: Session, draft_id: int, rule_key: str) -> ScenarioDraftRule:
    item = db.scalar(
        select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft_id, ScenarioDraftRule.rule_key == rule_key)
    )
    if not item:
        raise BusinessError("DRAFT_RULE_NOT_FOUND", "规则配置不存在", status_code=404, details={"rule_key": rule_key})
    return item


def update_draft_rule(db: Session, scenario_code: str, rule_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_rule_or_404(db, draft.id, rule_key)
    definition = _normalize_rule_payload(
        db,
        draft,
        payload,
        existing_key=rule_key,
        previous=item.rule_json if isinstance(item.rule_json, dict) else None,
    )
    dependents = db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all()
    downstream = [
        rule.name
        for rule in dependents
        if rule.rule_key != rule_key and (rule.rule_json or {}).get("source_rule_key") == rule_key
    ]
    item.name = definition["name"]
    item.node_code = definition["node"]
    item.operator_code = definition["operator"]
    item.enabled = definition["enabled"]
    item.rule_json = definition
    item.source_ref = definition.get("source_ref")
    detail: dict[str, Any] = {"scenario_code": scenario_code, "rule_key": rule_key}
    if downstream:
        detail["downstream_rules"] = downstream
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_UPDATE",
        object_name=definition["name"],
        detail=detail,
    )
    return draft


def set_draft_rule_enabled(db: Session, scenario_code: str, rule_key: str, enabled: bool) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_rule_or_404(db, draft.id, rule_key)
    item.enabled = enabled
    definition = dict(item.rule_json or {})
    definition["enabled"] = enabled
    item.rule_json = definition
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_RULE_ENABLE" if enabled else "DRAFT_RULE_DISABLE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "rule_key": rule_key},
    )
    return draft


def delete_draft_rule(db: Session, scenario_code: str, rule_key: str) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_rule_or_404(db, draft.id, rule_key)
    dependents = db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all()
    downstream = [
        rule.name
        for rule in dependents
        if rule.rule_key != rule_key and (rule.rule_json or {}).get("source_rule_key") == rule_key
    ]
    if downstream:
        raise BusinessError(
            "RULE_REFERENCED_BY_RISK",
            "该规则仍是以下风险规则的来源规则，请先处理后再删除",
            status_code=409,
            details={"rule_name": item.name, "rule_keys": downstream},
        )
    db.delete(item)
    _sync_published_version(
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

    节点版（contract_review_v3）的报告是「按编号分组的一组章节」；这些属性必须能
    通过页面维护并保存，否则保存一次就会退回旧的附加展示章节语义。
    缺省值为 None：旧格式章节本来就没有这些属性，不替它编造分组。
    """
    materials = db.scalars(
        select(ScenarioDraftMaterial).where(ScenarioDraftMaterial.draft_id == draft_id)
    ).all()
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
    if "config_status" in payload:
        value = payload.get("config_status")
        result["config_status"] = str(value).strip() or None if value is not None else None
    return result


def create_draft_section(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
    field_keys = _validate_section_fields(db, draft.id, payload.get("field_keys"))
    section_key = stable_key("sec", scenario_code, _item_ref())
    db.add(
        ScenarioDraftSection(
            draft_id=draft.id,
            section_key=section_key,
            title=title,
            content_instruction=str(payload.get("content_instruction") or ""),
            field_keys=field_keys,
            sort_no=_next_sort_no(db, ScenarioDraftSection, draft.id),
            **_section_group_fields(db, draft.id, payload),
        )
    )
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_CREATE",
        object_name=title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


def _validate_section_fields(db: Session, draft_id: int, field_keys: Any) -> list[str]:
    fields = db.scalars(select(ScenarioDraftField).where(ScenarioDraftField.draft_id == draft_id)).all()
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
    template, draft = _locked_draft(db, scenario_code)
    item = db.scalar(
        select(ScenarioDraftSection).where(ScenarioDraftSection.draft_id == draft.id, ScenarioDraftSection.section_key == section_key)
    )
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
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_UPDATE",
        object_name=item.title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


def delete_draft_section(db: Session, scenario_code: str, section_key: str) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    item = db.scalar(
        select(ScenarioDraftSection).where(ScenarioDraftSection.draft_id == draft.id, ScenarioDraftSection.section_key == section_key)
    )
    if not item:
        raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
    db.delete(item)
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_DELETE",
        object_name=item.title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


# ---------------------------------------------------------------------------
# 发布与版本
# ---------------------------------------------------------------------------


def compile_draft_manifest(db: Session, template: ScenarioTemplate, draft: ScenarioDraft) -> dict[str, Any]:
    materials, fields, rules, sections = _draft_rows(db, draft.id)
    settings = get_settings()
    return compile_form_manifest(
        scene_code=template.scenario_code,
        scene_name=template.scenario_name,
        namespace=template.scenario_code,
        materials=[
            {
                "material_key": item.material_key,
                "name": item.name,
                "material_type": item.material_type,
                "required": item.required,
                "description": item.description,
                # 来源业务编号进入编译结果，导出时按它写回 Excel 对应列。
                "source_ref": item.source_ref,
            }
            for item in materials
        ],
        fields=[
            {
                "field_key": item.field_key,
                "material_key": item.material_key,
                "name": item.name,
                "value_type": item.value_type,
                "unit": item.unit,
                "currency": item.currency,
                # 产物来源：主体核对据此确定字段取自哪种产物。草稿里保留、发布时带上，
                # 否则主体核对规则在页面保存一次后就不再被识别成主体核对。
                "artifact_source": item.artifact_source,
                "required": item.required,
                "instruction": item.instruction,
                "source_ref": item.source_ref,
            }
            for item in fields
        ],
        rules=[
            {
                **(item.rule_json or {}),
                "rule_key": item.rule_key,
                "name": item.name,
                "node": item.node_code,
                "operator": item.operator_code,
                "enabled": item.enabled,
                "source_ref": item.source_ref,
            }
            for item in rules
        ],
        sections=[
            {
                "section_name": item.title,
                "content_instruction": item.content_instruction,
                "field_keys": list(item.field_keys or []),
                "report_code": item.report_code,
                "report_name": item.report_name,
                "material_keys": list(item.material_keys or []),
                "formats": list(item.formats or []),
                "section_order": item.section_order,
                "content_type": item.content_type,
                "result_sort": item.result_sort,
                "config_status": item.config_status,
            }
            for item in sections
        ],
        # 来源格式版本：由导入发布 / 版本修订 / 复制继承；页面新建的草稿没有历史来源，
        # 才用配置里的默认版本。一律用默认版本会把旧格式配置说成节点版格式。
        profile_version=draft.profile_version or settings.default_rulebook_profile_version,
        compiled_at=datetime.now(UTC),
    )


def publish_draft(db: Session, scenario_code: str, payload: dict[str, Any]) -> tuple[ScenarioVersion, bool]:
    """把当前配置编译成场景版本。

    页面上已不再暴露“发布”动作（场景配置只有一份，保存即生效）；保留本函数
    供导入发布与内部同步调用，语义与 _publish_manifest 一致。

    - expected_revision 必须与草稿当前修订一致，否则明确冲突（刷新后重试）。
    - 内容与最新已发布版本完全一致（同频率指纹、同名称）时返回该版本，不制造重复版本。
    - 版本号由 allocate_version 的行锁与原子计数器分配，同一事务内完成。
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
    manifest = compile_draft_manifest(db, template, draft)
    change_summary = (str(payload.get("change_summary")).strip() or None) if payload.get("change_summary") else None
    version, reused = _publish_manifest(db, template, draft, manifest, change_summary=change_summary)
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


def revise_from_version(db: Session, scenario_code: str, version_no: int, payload: dict[str, Any]) -> ScenarioDraft:
    """基于某个已发布版本创建修订：用该版本配置整体替换当前草稿。

    这是显式的覆盖动作（页面必须二次确认）；已发布版本本身不可修改。
    """
    template = template_or_404(db, scenario_code, lock=True)
    version = version_or_404(db, scenario_code, version_no)
    draft = ensure_draft(db, template)
    _load_manifest_into_draft(db, draft, version.manifest_json)
    draft.source = "REVISION"
    draft.base_version_no = version_no
    _sync_published_version(
        db,
        template,
        draft,
        operation_type="REVISE_FROM_VERSION",
        object_name=template.scenario_name,
        detail={"scenario_code": scenario_code, "version_no": version_no},
    )
    db.refresh(draft)
    return draft


def list_versions_page(
    db: Session,
    *,
    scenario_code: str | None,
    source: str | None,
    published_from: datetime | None,
    published_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    conditions = []
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if source:
        conditions.append(ScenarioVersion.source == source)
    if published_from:
        conditions.append(ScenarioVersion.published_at >= published_from)
    if published_to:
        conditions.append(ScenarioVersion.published_at <= published_to)
    count_statement = select(func.count()).select_from(ScenarioVersion)
    rows_statement = select(ScenarioVersion)
    if conditions:
        count_statement = count_statement.where(*conditions)
        rows_statement = rows_statement.where(*conditions)
    total = db.scalar(count_statement) or 0
    versions = db.scalars(
        rows_statement.order_by(ScenarioVersion.published_at.desc(), ScenarioVersion.id.desc()).limit(page_size).offset((page - 1) * page_size)
    ).all()
    items = []
    for item in versions:
        manifest = item.manifest_json
        items.append(
            {
                "scenario_code": item.scenario_code,
                "scenario_name": item.scenario_name,
                "version_no": item.version_no,
                "status": item.status,
                "source": item.source,
                "source_label": VERSION_SOURCE_LABELS.get(item.source, item.source),
                "change_summary": item.change_summary,
                "published_at": item.published_at,
                "manifest_hash": item.manifest_hash,
                "material_count": len(manifest.get("materials", [])),
                "field_count": len(manifest.get("fields", [])),
                "rule_count": len([rule for rule in manifest.get("rules", []) if rule.get("operator") not in SYSTEM_OPERATORS]),
                "system_rule_count": len([rule for rule in manifest.get("rules", []) if rule.get("operator") in SYSTEM_OPERATORS]),
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def compare_versions(db: Session, scenario_code: str, from_no: int, to_no: int) -> dict[str, Any]:
    """按稳定键比较两个版本（不按名称猜对象）。"""
    from_version = version_or_404(db, scenario_code, from_no)
    to_version = version_or_404(db, scenario_code, to_no)

    def diff(kind: str, key_field: str, from_items: list[dict[str, Any]], to_items: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
        from_map = {item[key_field]: item for item in from_items}
        to_map = {item[key_field]: item for item in to_items}
        result = []
        for key in sorted(set(from_map) | set(to_map)):
            before = from_map.get(key)
            after = to_map.get(key)
            if before is None:
                result.append({"kind": kind, "key": key, "name": after.get("name", key), "change": "ADDED"})
                continue
            if after is None:
                result.append({"kind": kind, "key": key, "name": before.get("name", key), "change": "REMOVED"})
                continue
            changed_fields = [field for field in fields if before.get(field) != after.get(field)]
            if changed_fields:
                result.append(
                    {
                        "kind": kind,
                        "key": key,
                        "name": after.get("name", key),
                        "change": "CHANGED",
                        "changed_fields": changed_fields,
                        "before": {field: before.get(field) for field in changed_fields},
                        "after": {field: after.get(field) for field in changed_fields},
                    }
                )
        return result

    from_manifest = from_version.manifest_json
    to_manifest = to_version.manifest_json
    entries = []
    entries.extend(diff("MATERIAL", "material_key", from_manifest.get("materials", []), to_manifest.get("materials", []), ["name", "material_type", "required"]))
    entries.extend(
        diff(
            "FIELD",
            "field_key",
            from_manifest.get("fields", []),
            to_manifest.get("fields", []),
            ["name", "value_type", "unit", "currency", "material_key", "required", "instruction"],
        )
    )
    entries.extend(
        diff(
            "RULE",
            "rule_key",
            from_manifest.get("rules", []),
            to_manifest.get("rules", []),
            ["name", "node", "operator", "enabled", "parameters", "requirement", "materials", "source_rule_key"],
        )
    )
    return {
        "scenario_code": scenario_code,
        "from_version_no": from_no,
        "to_version_no": to_no,
        "entries": entries,
        "summary": {
            "added": len([item for item in entries if item["change"] == "ADDED"]),
            "removed": len([item for item in entries if item["change"] == "REMOVED"]),
            "changed": len([item for item in entries if item["change"] == "CHANGED"]),
        },
    }
