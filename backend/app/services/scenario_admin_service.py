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

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import capabilities, currency, field_strategy, financial_report, manifest as manifest_module, node_rulebook, scenario_schema, unified_profile
from ..domain.audit_vocab import SCOPE_NOT_APPLICABLE
from ..domain.node_registry import NODE_TYPE_LABELS, legacy_node_type_of
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
from .operation_log_service import OBJECT_SCENARIO, record_operation
from .template_registry_service import allocate_version

logger = logging.getLogger(__name__)

SYSTEM_OPERATORS = manifest_module.SYSTEM_OPERATORS
BUSINESS_OPERATORS = sorted(set(OPERATOR_NODE_CONSTRAINTS) - SYSTEM_OPERATORS)
# 草稿字段允许的数据类型：与 manifest 的表单契约共用同一份定义，
# 包含大写金额（03_字段 的「数据类型」下拉本来就有这一项）。
DRAFT_VALUE_TYPES = FORM_VALUE_TYPES
# 字段的产物来源取值：当前只有印章识别一种。
FIELD_ARTIFACT_SOURCES = {"SEAL"}
# 字段的取数期间与报表口径：**由字段自己声明**，计算参数按这两项定位字段事实
# （参数表不再声明它们）。合同场景没有这些概念，因此**允许声明「不适用」**，
# 页面留空即落成它；但被计算参数引用的字段不能用「不适用」（发布期会拦）。
FIELD_PERIOD_SCOPES = ("CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", SCOPE_NOT_APPLICABLE)
FIELD_STATEMENT_SCOPES = ("CONSOLIDATED", "PARENT", SCOPE_NOT_APPLICABLE)
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
    """按场景编码取模板；取不到即 404。lock 供写路径先锁行，include_deleted 供恢复与历史查看。"""
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

    节点实例排在最前：规则的节点归属与 Sheet 绑定都要按它校验，
    缺了它连「这条规则挂在哪个节点上」都无法回答。
    """
    sheets = db.scalars(
        select(ScenarioDraftSheet)
        .where(ScenarioDraftSheet.draft_id == draft_id)
        .order_by(ScenarioDraftSheet.sort_no, ScenarioDraftSheet.id)
    ).all()
    nodes = db.scalars(
        select(ScenarioDraftNode)
        .where(ScenarioDraftNode.draft_id == draft_id)
        .order_by(ScenarioDraftNode.sequence_no, ScenarioDraftNode.id)
    ).all()
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
    return list(nodes), list(sheets), list(materials), list(fields), list(rules), list(sections)


def _node_instance_payloads(nodes: list[ScenarioDraftNode]) -> list[dict[str, Any]]:
    """草稿节点实例 → 编译输入。

    只带编译真正需要的字段：实例码、能力类型、名称、顺序、依赖、配置与启停。
    依赖是**实例码**列表，编译期据此校验缺失依赖、自依赖、循环与顺序冲突。
    """
    return [
        {
            "node_code": item.node_code,
            "node_type": item.node_type,
            "name": item.name,
            "sequence_no": item.sequence_no,
            "depends_on": list(item.depends_on or []),
            "config_json": item.config_json,
            "enabled": item.enabled,
        }
        for item in nodes
    ]


def ensure_draft(db: Session, template: ScenarioTemplate) -> ScenarioDraft:
    """取得场景草稿；不存在时创建。

    场景已有已发布版本时，新建草稿基于最新版本派生（source=REVISION，
    base_version_no 记录来源版本）——这是明确的“创建修订”，不会覆盖任何东西；
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
        # 这里**不产生版本**，只刷新阻断原因。
        _refresh_publish_readiness(db, template, draft)
    db.flush()
    return draft


def _field_scope_of(field: dict[str, Any], key: str, label: str) -> str:
    """已发布配置里字段的取数期间 / 报表口径。

    历史 manifest 没有这两项：那时口径由**计算参数**声明，同一个字段可能被不同期间的
    参数共用（本期与上期共用一个「营业收入」）。新协议下口径由字段自己声明、一个字段
    只有一个口径，那个字段必须拆成两行——这是**业务决定**，系统无法从旧数据推断。
    因此这里明确拒绝把这份配置带进新草稿，而不是替业务挑一个口径：
    挑错会让计算静默取到另一期的数，而结果看起来完全正常。
    """
    value = field.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(
            "LEGACY_FIELD_SCOPE_INCOMPLETE",
            f"已发布版本里的字段“{field.get('name')}”没有声明{label}："
            "新协议要求字段自己声明取数期间与报表口径；旧版本的口径记在计算参数上，"
            "同一个字段可能被不同期间的参数共用，系统无法推断该保留哪一个。"
            "请重新导入规则库或重新配置这些字段后再发布新版本",
            status_code=409,
            details={"field_key": field.get("field_key"), "column": key},
        )
    return value.strip()


def _field_scope(payload: dict[str, Any], key: str, label: str, allowed: tuple[str, ...]) -> str:
    """表单提交的取数期间 / 报表口径。

    **留空 = 「不适用」**：合同场景本来就没有期间与合并/母公司的概念，这两列对它没有
    可填的值。留空落成显式取值而不是 NULL——统一事实表的当前版本唯一键由这几个口径
    拼成，NULL 会让唯一键整体失效（见 `domain.audit_vocab`）。

    填了不在允许取值内的值仍然明确报错：那不叫「没填」，叫填错了，不静默改成「不适用」。
    """
    value = str(payload.get(key) or "").strip()
    if not value:
        return SCOPE_NOT_APPLICABLE
    if value not in allowed:
        raise BusinessError(
            "FIELD_SCOPE_INVALID",
            f"字段的{label}取值无效（允许：{'、'.join(allowed)}，或留空表示「不适用」）",
            status_code=400,
            details={"column": key, "value": value, "allowed": list(allowed)},
        )
    return value


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
        schema_key=version.schema_key,
        schema_version=version.schema_version,
        schema_json=version.schema_json,
    )
    db.add(draft)
    db.flush()
    _load_manifest_into_draft(db, draft, version.manifest_json)
    db.flush()
    return draft


def _node_type_label(node_type: str) -> str:
    """节点能力类型的中文名。取不到时如实返回类型码，不猜一个名字。"""
    for node in unified_profile.load_unified_profile()["node_types"]:
        if node["node_type"] == node_type:
            return str(node.get("display_name") or node_type)
    return node_type


def _next_sort_no(db: Session, model: type, draft_id: int) -> int:
    current = db.scalar(select(func.max(model.sort_no)).where(model.draft_id == draft_id))
    return int(current or 0) + 1


def _origin_of_source_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **({"import_job_no": row["import_job_no"]} if row.get("import_job_no") else {}),
        "sheet_name": row["sheet_name"],
        **row["source"],
    }


def _load_manifest_into_draft(db: Session, draft: ScenarioDraft, manifest: dict[str, Any]) -> None:
    """用已发布版本的 manifest 替换草稿内容（创建修订 / 版本回填）。

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


def load_import_config_into_draft(
    db: Session, draft: ScenarioDraft, config: dict[str, Any], *, profile_version: str
) -> None:
    """导入替换 Excel 业务配置，并保留同一字段在场景内维护的抽取策略。"""
    existing_fields = db.scalars(
        select(ScenarioDraftField).where(ScenarioDraftField.draft_id == draft.id)
    ).all()
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
        if preserved is not None:
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
    _write_config_into_draft(db, draft, config, replace_nodes=False)


def _write_config_into_draft(
    db: Session, draft: ScenarioDraft, config: dict[str, Any], *, replace_nodes: bool
) -> None:
    nodes, sheets, materials, fields, rules, sections = _draft_rows(db, draft.id)
    old_rows = [*sheets, *materials, *fields, *rules, *sections]
    if replace_nodes:
        old_rows.extend(nodes)
    for row in old_rows:
        db.delete(row)
    db.flush()
    # 以下每一段都带顺序：原样恢复用户配置的顺序，不重排。
    if replace_nodes:
        for index, node in enumerate(config["nodes"], start=1):
            db.add(ScenarioDraftNode(draft_id=draft.id, sort_no=index, **node))
    for index, sheet in enumerate(config["sheets"], start=1):
        db.add(ScenarioDraftSheet(draft_id=draft.id, sort_no=index, **sheet))
    for index, material in enumerate(config["materials"], start=1):
        db.add(ScenarioDraftMaterial(draft_id=draft.id, sort_no=index, **material))
    for index, field in enumerate(config["fields"], start=1):
        db.add(ScenarioDraftField(draft_id=draft.id, sort_no=index, **field))
    # 系统规则（字段抽取、报告生成）不进草稿——它们在发布时按同一确定性规则重新派生。
    draft_rules = [item for item in config["rules"] if not item["is_system"]]
    for index, rule in enumerate(draft_rules, start=1):
        db.add(ScenarioDraftRule(draft_id=draft.id, sort_no=index, **{k: v for k, v in rule.items() if k != "is_system"}))
    for index, section in enumerate(config["sections"], start=1):
        db.add(ScenarioDraftSection(draft_id=draft.id, sort_no=index, **section))
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
    # 节点实例：用户配置的流程。页面据此渲染流程编排，并作为 Sheet 绑定的可选节点来源。
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
    explicit_extraction_outputs = {
        key
        for rule in rules
        if rule.enabled and rule.operator_code == "LLM_EXTRACT"
        for key in (rule.rule_json or {}).get("outputs", [])
        if key in field_map
    }
    # 系统规则（字段抽取、报告生成）按发布时的同一确定性规则派生，只读展示。
    # 归属必须是**用户配置的实例码**，与 compile_form_manifest 的派生口径一致：
    # 这里写能力类型会让预览与真实发布结果不一致。
    nodes, _, _, _, _, _ = _draft_rows(db, draft.id)
    # 同一能力类型可以有多个节点实例，不能用普通字典让后一个实例覆盖前一个。
    # 字段抽取优先沿用唯一的 Sheet 绑定；没有唯一绑定时，只有场景本身恰好
    # 只有一个同类节点才有确定归属。多个实例且没有唯一绑定时保持 None，
    # 让发布校验明确报告“无法确定”，不在草稿视图里编造一个归属。
    node_codes_by_type: dict[str, list[str]] = {}
    for node in nodes:
        node_codes_by_type.setdefault(node.node_type, []).append(node.node_code)
    instance_by_type = {
        node_type: codes[0]
        for node_type, codes in node_codes_by_type.items()
        if len(codes) == 1
    }
    extraction_sheet_nodes = {
        sheet.node_code
        for sheet in sheets
        if sheet.sheet_role == "FIELD_EXTRACTION" and sheet.node_code
    }
    if len(extraction_sheet_nodes) == 1:
        extraction_node_code = next(iter(extraction_sheet_nodes))
    else:
        extraction_node_code = instance_by_type.get("FIELD_EXTRACTION")
    for item in fields:
        if item.field_key in explicit_extraction_outputs:
            continue
        material_name = material_names.get(item.material_key)
        rule_items.append(
            {
                "rule_key": stable_key("rule", draft.scenario_code, f"extract:{item.field_key}"),
                "name": f"抽取{item.name}",
                "node": extraction_node_code,
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
            "node": instance_by_type.get("REPORT_OUTPUT"),
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
            "origin": item.origin_json,
            "updated_at": item.updated_at,
        }
        for item in sections
    ]
    profile = scenario_schema.effective_profile(draft.schema_json)
    return {
        "scenario_code": draft.scenario_code,
        "revision_no": draft.revision_no,
        "source": draft.source,
        "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
        "base_version_no": draft.base_version_no,
        "schema_key": draft.schema_key,
        "schema_version": draft.schema_version,
        "schema_json": draft.schema_json,
        # 草稿的来源格式版本：页面据此说明这份配置按哪种格式导出。
        "profile_version": draft.profile_version,
        # 当前配置是否已生效：NULL 表示已生效；非空时页面显示“尚未生效 + 原因”。
        # 编译未通过不阻断保存（建配置过程中必然不完整），但绝不能被当成已生效。
        "sync_error": draft.sync_error,
        "updated_at": draft.updated_at,
        # 节点实例：用户配置的流程（存在、顺序、依赖、启停）。
        # 页面的流程编排读它。Sheet 绑定的真源是导入成功写入后的 sheets，不在这里另改。
        "nodes": node_items,
        # 场景**选用**的 Sheet：这个场景用统一模板协议里的哪几张表、每张归哪个节点。
        # 页面据此渲染「Sheet 选择与绑定」，模板下载也按它取值——
        # 不再从草稿里已有材料/字段/规则反推（反推区分不了合同版与财报版的同名 Sheet）。
        "sheets": [
            {
                "sheet_role": item.sheet_role,
                "sheet_role_label": unified_profile.sheet_role_label(profile, item.sheet_role),
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
        # 计算三表写入草稿后的核对视图。没有计算规则时为空列表，不编一份。
        "calculations": _calculation_view_rows(draft.calculation_json),
        "business_rule_count": len(rules),
        "system_rule_count": len(field_items) - len(explicit_extraction_outputs) + 1,
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
        "source_node": definition.get("source_node"),
        "source_rule_key": definition.get("source_rule_key"),
        "source_rule_name": rule_names.get(definition.get("source_rule_key")) if definition.get("source_rule_key") else None,
        "risk_level": parameters.get("risk_level"),
        "risk_action": parameters.get("action"),
        "requires_manual_review": parameters.get("requires_manual_review") is True,
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


def version_config_view(db: Session, scenario_code: str, version_no: int) -> dict[str, Any]:
    """某个已发布版本的配置视图（材料、字段、规则、报告章节）。"""
    version = version_or_404(db, scenario_code, version_no)
    manifest = version.manifest_json
    material_names = {item["material_key"]: item["name"] for item in manifest.get("materials", [])}
    field_map = {item["field_key"]: item for item in manifest.get("fields", [])}
    rule_names = {item["rule_key"]: item["name"] for item in manifest.get("rules", [])}
    materials = [
        {
            "material_key": item["material_key"],
            "name": item["name"],
            "required": bool(item.get("required")),
            "description": str(item.get("description") or ""),
            "origin": item.get("origin"),
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
            "origin": item.get("origin"),
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
                "system": manifest_module.is_system_rule(rule),
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
                "source_ref": rule.get("source_ref"),
                "origin": rule.get("origin"),
                "updated_at": None,
            }
        )
    sections = [
        {
            "section_key": section["section_key"] if "section_key" in section else f"v{version_no}-s{index}",
            "title": section.get("section_name", ""),
            "content_instruction": str(section.get("content_instruction") or ""),
            "field_keys": list(section.get("field_keys", [])),
            "field_names": [field_map[key]["name"] for key in section.get("field_keys", []) if key in field_map],
            "sort_no": index,
            "origin": section.get("origin"),
            "updated_at": None,
        }
        for index, section in enumerate(manifest.get("report_template", []), start=1)
    ]
    return {
        "scenario_code": version.scenario_code,
        "scenario_name": version.scenario_name,
        "version_no": version.version_no,
        "schema_key": version.schema_key,
        "schema_version": version.schema_version,
        "schema_json": version.schema_json,
        "source": version.source,
        "source_label": VERSION_SOURCE_LABELS.get(version.source, version.source),
        "change_summary": version.change_summary,
        "published_at": version.published_at,
        "manifest_hash": version.manifest_hash,
        "unconfigured_check_node_types": list(manifest.get("unconfigured_check_node_types", [])),
        "unconfigured_check_node_codes": list(manifest.get("unconfigured_check_node_codes", [])),
        "capability_gaps": list(manifest.get("capability_gaps", [])),
        "materials": materials,
        "fields": fields,
        "rules": rules,
        "sections": sections,
        "calculations": _calculation_view_rows({"frozen": manifest.get("calculations") or []}),
        "disabled_source_rows": list(manifest.get("disabled_source_rows") or []),
        "business_rule_count": len([item for item in rules if not manifest_module.is_system_rule(item)]),
        "system_rule_count": len([item for item in rules if manifest_module.is_system_rule(item)]),
    }


# 场景基本资料


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
    """场景分页列表（真实后端筛选与 total）。"""
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
    """**管理员**用的场景下拉：未删除的全部场景及其实时状态。

    导入页与场景管理页需要在场景**发布之前**就选中它（先建场景、再上传规则库），
    因此这里不能按「已发布」过滤——那会让管理员无法在新建场景下上传。
    """
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
            "available": item.enabled and item.scenario_code in latest,
        }
        for item in templates
    ]


def list_scenario_options(db: Session) -> list[dict[str, Any]]:
    """**业务用户可用的场景**下拉选项（方案、任务等选择器）。

    只列出「未删除 + 已启用 + 已有已发布版本」的场景：草稿、未发布、停用或已删除的
    场景不能用于创建方案（需求 41）。把不可用的场景也列出来、再在创建时报错，
    会让业务用户在一堆选不动的选项里试错。

    `available` 恒为 true；保留该字段是为了让页面无需自己推断，
    并让接口在将来放宽条件时不必改契约。
    """
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
            "latest_version_no": latest[item.scenario_code],
            "available": True,
        }
        for item in templates
        if item.enabled and item.scenario_code in latest
    ]


def _default_draft_nodes(draft_id: int, schema: dict[str, Any]) -> list[ScenarioDraftNode]:
    """统一模板协议声明的标准节点集：每种能力类型一个实例。

    节点实例码直接用能力类型名（`MATERIAL_CHECK`、`DOCUMENT_PREPARE`……），顺序取协议
    声明顺序，依赖取协议声明的 `depends_on_types`。新建场景因此一开始就带着一条完整流程，
    导入时的「绑定节点」是纯下拉选择，不必先手工把节点建一遍。

    这只是**预置的起点**，不是固定结构：预置后用户仍可增删节点、改实例码、改顺序与依赖
    （同一能力类型配多个实例也照旧支持）。本函数只在新建场景时调用一次，不覆盖已有配置。
    """
    profile = scenario_schema.effective_profile(schema)
    rows: list[ScenarioDraftNode] = []
    for sequence_no, node in enumerate(profile["node_types"], start=1):
        node_type = node["node_type"]
        rows.append(
            ScenarioDraftNode(
                draft_id=draft_id,
                # 实例码 = 能力类型名：与旧场景（node_code 就是能力类型）同一口径，
                # 也让「这张 Sheet 该绑哪个节点」在预置后只有一个显而易见的答案。
                node_code=node_type,
                node_type=node_type,
                name=str(node.get("display_name") or node_type),
                sequence_no=sequence_no,
                depends_on=[str(code) for code in (node.get("depends_on_types") or [])],
                enabled=True,
                sort_no=sequence_no,
            )
        )
    return rows


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
    for row in _default_draft_nodes(draft.id, schema):
        db.add(row)
    db.flush()
    # 新建场景是空配置，必然不能发布（没有材料位）。这里就写下真实的阻断原因，
    # 否则页面会把一个空场景谎报成「可以发布」。**不产生任何版本**。
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
            if (schema["schema_key"], schema["schema_version"]) == (
                current["schema_key"], current["schema_version"]
            ):
                raise BusinessError(
                    "SCENARIO_SCHEMA_VERSION_UNCHANGED",
                    "场景字段映射已变化，必须修改 schema_key 或 schema_version",
                    status_code=409,
                )
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


def delete_scenario(db: Session, scenario_code: str) -> ScenarioTemplate:
    """逻辑删除场景；历史版本与任务保留。"""
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
    """恢复已逻辑删除的场景。"""
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
    """复制场景：基本资料 + 当前草稿的**完整配置**。

    复制的是一份完整可编辑的草稿，不是残件：节点实例（含顺序、依赖、启停与节点级配置）、
    Sheet 绑定、材料、字段、规则、报告章节、计算候选与停用来源行一起复制。
    内部引用全部重映射到新稳定键：

    - 字段 → 所属材料；
    - 规则 → 输入字段与材料、材料范围、`source_rule_key` 与 `source_rule_keys`
      （**风险的多来源必须逐条重映射**）、来源节点实例码；
    - 报告章节 → 字段与材料范围；
    - 计算候选里的字段编号与节点实例码。

    **节点先复制**：规则的 `node` 是节点实例码，节点实例码在复制后保持不变
    （它由用户配置，不是系统派生的稳定键），但规则的归属必须落在真实存在的节点上，
    因此先建节点再写规则。浅复制含旧稳定键的 JSON 会让新场景的规则指向原场景的
    字段与规则，发布时要么报「引用的字段不存在」，要么指向另一份配置。

    已发布版本不复制：复制的是一份草稿，仍要管理员显式发布才能被业务使用。
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
        schema_key=source.schema_key,
        schema_version=source.schema_version,
        schema_json=source.schema_json,
    )
    db.add(template)
    db.flush()
    source_draft = ensure_draft(db, source)
    nodes, sheets, materials, fields, rules, sections = _draft_rows(db, source_draft.id)
    draft = ScenarioDraft(
        scenario_code=new_code,
        revision_no=1,
        source="COPY",
        base_version_no=None,
        # 复制保留来源格式版本：复制出来的是同一份业务配置，不因复制换一种导出格式。
        profile_version=source_draft.profile_version,
        sync_error=source_draft.sync_error,
        schema_key=source_draft.schema_key,
        schema_version=source_draft.schema_version,
        schema_json=source_draft.schema_json,
    )
    db.add(draft)
    db.flush()
    # 节点实例先复制：规则的节点归属与 Sheet 绑定都要按它校验。
    for item in nodes:
        db.add(
            ScenarioDraftNode(
                draft_id=draft.id,
                node_code=item.node_code,
                node_type=item.node_type,
                name=item.name,
                sequence_no=item.sequence_no,
                depends_on=list(item.depends_on or []),
                config_json=dict(item.config_json) if isinstance(item.config_json, dict) else item.config_json,
                enabled=item.enabled,
                sort_no=item.sort_no,
            )
        )
    for item in sheets:
        db.add(
            ScenarioDraftSheet(
                draft_id=draft.id,
                sheet_role=item.sheet_role,
                node_code=item.node_code,
                sort_no=item.sort_no,
            )
        )

    def copied_ref(mapping: dict[str, str], key: str, *, owner: str, target: str) -> str:
        copied = mapping.get(key)
        if copied is None:
            raise BusinessError(
                "SCENARIO_COPY_REFERENCE_MISSING",
                "复制场景时配置引用的对象不存在，不能保留原场景的稳定键",
                details={"owner": owner, "target": target, "key": key},
            )
        return copied

    def copied_key(prefix: str, source_ref: str | None) -> str:
        code = (source_ref or "").strip()
        return stable_key(prefix, new_code, code if code else _item_ref())

    material_key_map: dict[str, str] = {}
    field_key_map: dict[str, str] = {}
    rule_key_map: dict[str, str] = {}
    for item in materials:
        new_key = copied_key("mat", item.source_ref)
        material_key_map[item.material_key] = new_key
        db.add(
            ScenarioDraftMaterial(
                draft_id=draft.id,
                material_key=new_key,
                name=item.name,
                required=item.required,
                description=item.description,
                source_ref=item.source_ref,
                origin_json=item.origin_json,
                sort_no=item.sort_no,
            )
        )
    for item in fields:
        new_key = copied_key("fld", item.source_ref)
        field_key_map[item.field_key] = new_key
        db.add(
            ScenarioDraftField(
                draft_id=draft.id,
                field_key=new_key,
                material_key=copied_ref(
                    material_key_map, item.material_key, owner=item.field_key, target="MATERIAL"
                ),
                name=item.name,
                value_type=item.value_type,
                extraction_strategy=item.extraction_strategy,
                structure_schema_json=item.structure_schema_json,
                unit=item.unit,
                currency=item.currency,
                artifact_source=item.artifact_source,
                period_scope=item.period_scope,
                statement_scope=item.statement_scope,
                required=item.required,
                instruction=item.instruction,
                source_ref=item.source_ref,
                origin_json=item.origin_json,
                sort_no=item.sort_no,
            )
        )
    material_by_key = {item.material_key: item for item in materials}
    node_type_by_code = {item.node_code: item.node_type for item in nodes}
    for item in rules:
        code = (item.source_ref or "").strip()
        if not code:
            new_key = copied_key("rule", None)
        else:
            node_type = node_type_by_code.get(item.node_code)
            if node_type is None:
                raise BusinessError(
                    "SCENARIO_COPY_RULE_NODE_MISSING",
                    "复制场景时规则所属节点不存在，无法保留业务身份",
                    details={"rule_key": item.rule_key, "node_code": item.node_code},
                )
            material_code: str | None = None
            if node_type == "INTERNAL_VALIDATE":
                branch_keys = {
                    entry.get("material_key") for entry in (item.rule_json or {}).get("inputs", [])
                }
                declared_materials = set((item.rule_json or {}).get("materials") or [])
                if len(branch_keys) != 1 or None in branch_keys or (
                    declared_materials and declared_materials != branch_keys
                ):
                    raise BusinessError(
                        "SCENARIO_COPY_RULE_BRANCH_INVALID",
                        "内部校验规则的输入材料与适用材料不一致，无法保留业务身份",
                        details={"rule_key": item.rule_key, "source_ref": code},
                    )
                material = material_by_key.get(next(iter(branch_keys)))
                material_code = (material.source_ref or "").strip() if material else ""
                if not material_code:
                    raise BusinessError(
                        "SCENARIO_COPY_RULE_BRANCH_INVALID",
                        "内部校验规则的输入材料缺少业务编号，无法保留业务身份",
                        details={"rule_key": item.rule_key, "source_ref": code},
                    )
            new_key = manifest_module.business_rule_key(
                new_code, node_type, code, material_code=material_code
            )
        rule_key_map[item.rule_key] = new_key
    for object_type, mapping in (
        ("MATERIAL", material_key_map),
        ("FIELD", field_key_map),
        ("RULE", rule_key_map),
    ):
        if len(set(mapping.values())) != len(mapping):
            raise BusinessError(
                "SCENARIO_COPY_BUSINESS_CODE_DUPLICATED",
                "来源业务编号在副本中无法唯一确定配置对象",
                details={"object_type": object_type},
            )
    for item in rules:
        definition = dict(item.rule_json or {})
        applicability = definition.get("applicability") if isinstance(definition.get("applicability"), dict) else {}
        mapped_rule_keys = list(dict.fromkeys(
            copied_ref(rule_key_map, declared, owner=item.rule_key, target="SOURCE_RULE")
            for declared in (definition.get("source_rule_keys") or [])
        ))
        declared_source = definition.get("source_rule_key")
        mapped_source = (
            copied_ref(rule_key_map, declared_source, owner=item.rule_key, target="SOURCE_RULE")
            if declared_source else None
        )
        if mapped_source and mapped_source not in mapped_rule_keys:
            mapped_rule_keys.insert(0, mapped_source)
        new_definition = {
            **definition,
            "inputs": [
                {
                    "field_key": copied_ref(
                        field_key_map, entry["field_key"], owner=item.rule_key, target="FIELD"
                    ),
                    "material_key": copied_ref(
                        material_key_map, entry["material_key"], owner=item.rule_key, target="MATERIAL"
                    ),
                }
                for entry in definition.get("inputs", [])
            ],
            "outputs": [
                copied_ref(field_key_map, key, owner=item.rule_key, target="FIELD")
                for key in definition.get("outputs", [])
            ],
            "materials": [
                copied_ref(material_key_map, key, owner=item.rule_key, target="MATERIAL")
                for key in definition.get("materials", [])
            ],
            "source_rule_key": mapped_source,
            # 风险的多来源（任一 / 全部关联检查）必须逐条重映射：只改第一条会让
            # 「全部关联检查不合格」在副本里指向原场景的规则。
            "source_rule_keys": mapped_rule_keys,
            "applicability": {
                **applicability,
                **(
                    {"material_keys": [
                        copied_ref(material_key_map, key, owner=item.rule_key, target="MATERIAL")
                        for key in applicability.get("material_keys") or []
                    ]}
                    if applicability.get("material_keys")
                    else {}
                ),
            },
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
                origin_json=item.origin_json,
                sort_no=item.sort_no,
            )
        )
    section_keys: set[str] = set()
    for item in sections:
        if item.report_code and item.report_code.strip() and item.section_order is not None and item.section_order > 0:
            section_identity = json.dumps(
                [item.report_code.strip(), item.section_order], ensure_ascii=False, separators=(",", ":")
            )
            section_key = stable_key("sec", new_code, section_identity)
        else:
            section_key = copied_key("sec", None)
        if section_key in section_keys:
            raise BusinessError(
                "SCENARIO_COPY_SECTION_DUPLICATED",
                "报告编号与章节顺序在副本中重复，无法保留章节身份",
                details={"report_code": item.report_code, "section_order": item.section_order},
            )
        section_keys.add(section_key)
        db.add(
            ScenarioDraftSection(
                draft_id=draft.id,
                section_key=section_key,
                title=item.title,
                content_instruction=item.content_instruction,
                field_keys=[
                    copied_ref(field_key_map, key, owner=section_key, target="FIELD")
                    for key in (item.field_keys or [])
                ],
                sort_no=item.sort_no,
                # 报告分组属性随复制一起保留，材料范围按新材料键重映射。
                report_code=item.report_code,
                report_name=item.report_name,
                material_keys=[
                    copied_ref(material_key_map, key, owner=section_key, target="MATERIAL")
                    for key in (item.material_keys or [])
                ] or None,
                formats=list(item.formats or []) or None,
                section_order=item.section_order,
                content_type=item.content_type,
                result_sort=item.result_sort,
                config_status=item.config_status,
                origin_json=item.origin_json,
            )
        )
    # 计算配置随复制一起带走。它内部引用的是**业务编号**（参数的 `source_code` 是字段
    # 抽取的业务编号或计算结果编号）与**节点实例码**，三者都随复制原样保留，
    # 因此这里只需要深拷贝一份，不重映射也不共享原对象——共享会让副本页面的保存
    # 直接改写原场景的草稿 JSON。
    draft.calculation_json = json.loads(json.dumps(source_draft.calculation_json or []))
    draft.disabled_source_rows = [
        {
            **row,
            "row_key": stable_key(
                "disabled",
                new_code,
                json.dumps(
                    [row["sheet_role"], row["source"]["sheet_key"], row["source"]["row_no"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        }
        for row in json.loads(json.dumps(source_draft.disabled_source_rows or []))
    ]
    record_operation(
        db,
        object_type=OBJECT_SCENARIO,
        object_no=new_code,
        object_name=name,
        operation_type="COPY",
        detail={"scenario_code": new_code, "source_scenario_code": scenario_code},
    )
    # 复制出来的是一份完整可用的配置（节点、Sheet、材料/字段/规则/章节都按新键重映射过），
    # 但**复制不等于发布**：新场景仍需要管理员显式发布才能被业务用户使用。
    # 这里只刷新「能不能发布」的状态；源草稿本身不完整时带上同一原因。
    _refresh_publish_readiness(db, template, draft)
    db.commit()
    db.refresh(template)
    return template


def scenario_detail(db: Session, scenario_code: str) -> dict[str, Any]:
    """场景详情：基本资料、版本列表与配置统计。"""
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
        "schema_key": template.schema_key,
        "schema_version": template.schema_version,
        "schema_json": template.schema_json,
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
                "rule_count": len([rule for rule in item.manifest_json.get("rules", []) if not manifest_module.is_system_rule(rule)]),
            }
            for item in versions
        ],
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
    if template.deleted_at is not None:
        raise BusinessError("SCENARIO_DELETED", "场景已删除，不能修改草稿", status_code=409)
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
    profile = scenario_schema.effective_profile(draft.schema_json)
    definition = unified_profile.sheet_definition(profile, role)
    profile_columns = {column["key"]: column for column in definition["columns"]}
    stored_columns = {column["key"]: column for column in row["columns"]}
    if set(stored_columns) != set(profile_columns) or len(stored_columns) != len(row["columns"]):
        raise BusinessError(
            "DISABLED_SOURCE_ROW_PROFILE_CHANGED",
            "停用来源行的列结构与当前模板协议不同，不能按旧列定义编辑",
            status_code=409,
            details={"row_key": row_key, "sheet_role": role},
        )
    values = payload["values"]
    unknown = sorted(set(values) - set(stored_columns))
    if unknown:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_COLUMN_UNKNOWN",
            "修改内容包含当前工作表不存在的列",
            details={"row_key": row_key, "columns": unknown},
        )
    if "enabled" in values and values["enabled"].strip() != "否":
        raise BusinessError(
            "DISABLED_SOURCE_ROW_ENABLE_REQUIRED",
            "编辑停用来源行不能直接改变是否生效；启用时须转换为业务配置并校验引用",
            status_code=409,
        )
    if role in {"CALC_PARAMETER", "CALC_JUDGEMENT"} and "rule_code" in values:
        if values["rule_code"].strip() != stored_columns["rule_code"]["value"]:
            raise BusinessError(
                "DISABLED_CALC_REFERENCE_LOCKED",
                "计算参数和判定的规则编号由停用计算定义维护，不能单独改写",
                status_code=409,
            )
    updated_columns = [
        {**column, "value": values[column["key"]].strip()} if column["key"] in values else dict(column)
        for column in row["columns"]
    ]
    updated_values = {column["key"]: column["value"] for column in updated_columns}
    if role == "CALC_DEFINITION" and updated_values["rule_code"] != stored_columns["rule_code"]["value"]:
        old_code = stored_columns["rule_code"]["value"]
        new_code = updated_values["rule_code"]
        children = [
            (child_index, child)
            for child_index, child in enumerate(rows)
            if child["sheet_role"] in {"CALC_PARAMETER", "CALC_JUDGEMENT"}
            and any(column["key"] == "rule_code" and column["value"] == old_code for column in child["columns"])
        ]
        if children and not new_code:
            raise BusinessError(
                "DISABLED_CALC_CODE_REQUIRED",
                "该计算定义仍有关联参数或判定，不能清空规则编号",
                status_code=409,
            )
        calculation_candidates, frozen_calculations = _calculation_store(draft.calculation_json)
        if new_code and any(
            item.get("rule_code") == new_code
            for item in (frozen_calculations if frozen_calculations is not None else calculation_candidates)
        ):
            raise BusinessError("DISABLED_CALC_CODE_DUPLICATE", "规则编号与现有计算规则重复", status_code=409)
        if any(
            item["sheet_role"] == "CALC_DEFINITION" and item["row_key"] != row_key
            and any(column["key"] == "rule_code" and column["value"] == new_code for column in item["columns"])
            for item in rows
        ):
            raise BusinessError("DISABLED_CALC_CODE_DUPLICATE", "停用计算定义的规则编号重复", status_code=409)
        for child_index, child in children:
            rows[child_index] = {
                **child,
                "source_id": new_code or None,
                "columns": [
                    {**column, "value": new_code} if column["key"] == "rule_code" else column
                    for column in child["columns"]
                ],
            }
    row["columns"] = updated_columns
    identity_key = next((key for key in ("source_id", "report_code", "rule_code") if key in updated_values), None)
    if identity_key is not None:
        row["source_id"] = updated_values[identity_key] or None
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


def activate_disabled_source_row(
    db: Session, scenario_code: str, row_key: str, *, expected_revision: int
) -> ScenarioDraft:
    """按角色解析停用来源行，核对引用后写入同一份场景草稿。"""
    template, draft = _locked_draft(db, scenario_code)
    if template.deleted_at is not None:
        raise BusinessError("SCENARIO_DELETED", "场景已删除，不能修改草稿", status_code=409)
    if expected_revision != draft.revision_no:
        raise BusinessError(
            "SCENARIO_DRAFT_REVISION_CONFLICT",
            f"配置在本次编辑期间已被修改（当前修订 {draft.revision_no}），请刷新后重试",
            status_code=409,
            details={"scenario_code": scenario_code, "expected": expected_revision, "actual": draft.revision_no},
        )
    rows = list(draft.disabled_source_rows or [])
    source_row = next((row for row in rows if row["row_key"] == row_key), None)
    if source_row is None:
        raise BusinessError("DISABLED_SOURCE_ROW_NOT_FOUND", "停用来源行不存在", status_code=404)
    if source_row["sheet_role"] == "CALC_DEFINITION":
        return _activate_disabled_calculation_locked(db, template, draft, source_row, rows)
    if source_row["sheet_role"] in {"MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK"}:
        return _activate_disabled_rule_group_locked(db, template, draft, source_row, rows)
    role = source_row["sheet_role"]
    if role not in {"MATERIAL_LIST", "FIELD_EXTRACTION", "REPORT_TEMPLATE"}:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_ROLE_UNSUPPORTED",
            "该工作表的停用行尚无直接启用转换路径",
            status_code=409,
            details={"sheet_role": role},
        )
    nodes, sheets, materials, fields, _rules, sections = _draft_rows(db, draft.id)
    binding = next((sheet for sheet in sheets if sheet.sheet_role == role), None)
    node = next((item for item in nodes if binding is not None and item.node_code == binding.node_code), None)
    profile = scenario_schema.effective_profile(draft.schema_json)
    definition = unified_profile.sheet_definition(profile, role)
    if node is None or not node.enabled or node.node_type not in definition["node_types"]:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_NODE_INVALID",
            "停用来源行所属工作表未绑定可用的业务节点，不能启用",
            status_code=409,
            details={"sheet_role": role, "node_code": binding.node_code if binding else None},
        )
    values = {column["key"]: column["value"] for column in source_row["columns"]}
    if values.get("enabled") != "否":
        raise BusinessError("DISABLED_SOURCE_ROW_STATE_INVALID", "来源行不是停用状态，不能重复启用", status_code=409)
    effective_row = json.loads(json.dumps(source_row))
    effective_row["columns"] = [
        {**column, "value": "是"} if column["key"] == "enabled" else column
        for column in effective_row["columns"]
    ]
    material_items: list[dict[str, Any]] = []
    for material in (materials if role in {"FIELD_EXTRACTION", "REPORT_TEMPLATE"} else []):
        if not material.source_ref:
            continue
        material_items.append({
            "candidate_ref": material.material_key,
            "source_id": material.source_ref,
            "name": material.name,
        })
    material_by_code = node_rulebook._reference_index(material_items, kind="材料")
    field_by_code: dict[str, dict[str, Any]] = {}
    if role == "REPORT_TEMPLATE":
        for field in fields:
            if not field.source_ref:
                continue
            if field.source_ref in field_by_code:
                raise BusinessError(
                    "DISABLED_FIELD_CODE_DUPLICATE",
                    "现有字段编号重复，报告章节无法唯一解析展示字段",
                    status_code=409,
                    details={"source_ref": field.source_ref},
                )
            field_by_code[field.source_ref] = {
                "candidate_ref": field.field_key,
                "source_id": field.source_ref,
                "name": field.name,
                "material_ref": field.material_key,
            }
    conversion = node_rulebook.convert_disabled_config_row(
        effective_row,
        profile_version=draft.profile_version or "",
        material_by_code=material_by_code,
        field_by_code=field_by_code,
        profile=profile,
    )
    if conversion.notes:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_ISSUES_UNRESOLVED",
            "来源行仍有未确认的业务口径，请先补齐后再启用",
            status_code=409,
            details={"row_key": row_key, "issues": [note.to_payload() for note in conversion.notes]},
        )
    candidate_type = {
        "MATERIAL_LIST": "materials",
        "FIELD_EXTRACTION": "fields",
        "REPORT_TEMPLATE": "report_sections",
    }[role]
    candidates = conversion.proposals[candidate_type]
    if len(candidates) != 1:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_CONVERSION_INVALID",
            "来源行未能转换成唯一业务配置，草稿未修改",
            status_code=409,
            details={"row_key": row_key, "sheet_role": role},
        )
    candidate = candidates[0]
    code = candidate["report_code"] if role == "REPORT_TEMPLATE" else candidate["source_id"]
    if role == "MATERIAL_LIST":
        key = stable_key("mat", scenario_code, code)
        if any(item.material_key == key or item.source_ref == code or item.name == candidate["name"] for item in materials):
            raise BusinessError(
                "DISABLED_MATERIAL_DUPLICATE", "材料编号或名称已存在，不能重复启用", status_code=409,
                details={"source_ref": code, "name": candidate["name"]},
            )
        db.add(ScenarioDraftMaterial(
            draft_id=draft.id,
            material_key=key,
            name=candidate["name"],
            required=candidate["required"],
            description=candidate["description"],
            source_ref=code,
            origin_json=_origin_of_source_row(source_row),
            sort_no=_next_sort_no(db, ScenarioDraftMaterial, draft.id),
        ))
    elif role == "FIELD_EXTRACTION":
        key = stable_key("fld", scenario_code, code)
        material_key = candidate["material_ref"]
        if any(
            item.field_key == key or item.source_ref == code
            or (item.material_key == material_key and item.name == candidate["name"])
            for item in fields
        ):
            raise BusinessError(
                "DISABLED_FIELD_DUPLICATE", "字段编号或同材料下的字段名称已存在，不能重复启用", status_code=409,
                details={"source_ref": code, "name": candidate["name"], "material_key": material_key},
            )
        db.add(ScenarioDraftField(
            draft_id=draft.id,
            field_key=key,
            material_key=material_key,
            name=candidate["name"],
            value_type=candidate["value_type"],
            extraction_strategy=field_strategy.default_extraction_strategy(candidate["value_type"]),
            structure_schema_json=None,
            unit=candidate["unit"],
            currency=candidate["currency"],
            artifact_source=candidate["artifact_source"],
            period_scope=candidate["period_scope"],
            statement_scope=candidate["statement_scope"],
            required=candidate["required"],
            instruction=candidate["instruction"],
            source_ref=code,
            origin_json=_origin_of_source_row(source_row),
            sort_no=_next_sort_no(db, ScenarioDraftField, draft.id),
        ))
    else:
        section_identity = json.dumps(
            [code, candidate["section_order"]], ensure_ascii=False, separators=(",", ":")
        )
        key = stable_key("sec", scenario_code, section_identity)
        if any(item.section_key == key or (
            item.report_code == code and item.section_order == candidate["section_order"]
        ) for item in sections):
            raise BusinessError(
                "DISABLED_REPORT_SECTION_DUPLICATE",
                "同一报告编号下的章节顺序已存在，不能重复启用",
                status_code=409,
                details={"report_code": code, "section_order": candidate["section_order"]},
            )
        for item in sections:
            if item.report_code != code and item.report_name == candidate["report_name"]:
                raise BusinessError(
                    "DISABLED_REPORT_NAME_DUPLICATE",
                    "不同报告编号使用了相同报告名称，不能启用该章节",
                    status_code=409,
                    details={"report_code": code, "existing_report_code": item.report_code},
                )
            if item.report_code != code:
                continue
            if (
                item.report_name != candidate["report_name"]
                or list(item.material_keys or []) != candidate["material_refs"]
                or list(item.formats or []) != candidate["formats"]
                or item.result_sort != candidate["result_sort"]
            ):
                raise BusinessError(
                    "DISABLED_REPORT_GROUP_CONFLICT",
                    "同一报告编号的报告名称、材料范围、输出格式或结果排序与现有章节不一致",
                    status_code=409,
                    details={"report_code": code, "existing_section_key": item.section_key},
                )
        db.add(ScenarioDraftSection(
            draft_id=draft.id,
            section_key=key,
            title=candidate["section_name"],
            content_instruction=candidate["content_instruction"],
            field_keys=candidate["field_refs"],
            report_code=code,
            report_name=candidate["report_name"],
            material_keys=candidate["material_refs"],
            formats=candidate["formats"],
            section_order=candidate["section_order"],
            content_type=candidate["content_type"],
            result_sort=candidate["result_sort"],
            config_status=candidate["config_status"],
            origin_json=_origin_of_source_row(source_row),
            sort_no=_next_sort_no(db, ScenarioDraftSection, draft.id),
        ))
    draft.disabled_source_rows = [row for row in rows if row["row_key"] != row_key]
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SOURCE_ROW_ACTIVATE",
        object_name=template.scenario_name,
        detail={
            "row_key": row_key,
            "sheet_role": role,
            "target_key": key,
            "original_source": source_row["source"],
            "effective_columns": effective_row["columns"],
        },
    )
    db.refresh(draft)
    return draft


def _draft_risks_by_code(stored_rules: list[ScenarioDraftRule]) -> dict[str, ScenarioDraftRule]:
    risks: dict[str, ScenarioDraftRule] = {}
    for item in stored_rules:
        if item.operator_code != "RISK_TRIGGER" or not item.source_ref:
            continue
        if item.source_ref in risks:
            raise BusinessError("DISABLED_RISK_CODE_DUPLICATE", "现有风险编号重复，无法确定检查规则的关联风险", status_code=409)
        risks[item.source_ref] = item
    return risks


def _disabled_rule_group_rows(
    source_row: dict[str, Any], rows: list[dict[str, Any]], existing_risks: dict[str, ScenarioDraftRule]
) -> tuple[list[dict[str, Any]], set[str]]:
    """按 02/04/05 的风险引用求检查与风险的完整停用行闭包。"""
    check_roles = {"MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE"}
    disabled_checks = [item for item in rows if item["sheet_role"] in check_roles]
    disabled_risks: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        if item["sheet_role"] == "RISK" and item.get("source_id"):
            disabled_risks.setdefault(item["source_id"], []).append(item)
    risk_code_by_reference: dict[str, str] = {}
    def add_reference(value: str, code: str) -> None:
        if not value:
            return
        previous = risk_code_by_reference.get(value)
        if previous is not None and previous != code:
            raise BusinessError(
                "RISK_REFERENCE_AMBIGUOUS", f"风险引用“{value}”同时对应多个风险，不能确定关联关系",
                status_code=409, details={"value": value, "risk_codes": [previous, code]},
            )
        risk_code_by_reference[value] = code

    for code, risk in existing_risks.items():
        add_reference(code, code)
        add_reference(risk.name, code)
    for code, risk_rows in disabled_risks.items():
        add_reference(code, code)
        for risk_row in risk_rows:
            name = next((column["value"] for column in risk_row["columns"] if column["key"] == "name"), "")
            add_reference(name, code)
    check_by_key = {item["row_key"]: item for item in disabled_checks}
    check_risks: dict[str, list[str]] = {}
    # 只用原文定位可能相关的行；进入本组的行再按正式清单规则校验。
    possible_risks = {
        item["row_key"]: [
            part.strip()
            for column in item["columns"] if column["key"] == "risk_refs"
            for part in column["value"].split(node_rulebook.LIST_SEPARATOR)
            if part.strip()
        ]
        for item in disabled_checks
    }

    def risks_of(row_key: str) -> list[str]:
        if row_key not in check_risks:
            references = node_rulebook.disabled_check_risk_codes(check_by_key[row_key])
            check_risks[row_key] = [risk_code_by_reference.get(value, value) for value in references]
        return check_risks[row_key]
    selected_checks: set[str] = set()
    selected_risks: set[str] = set()
    if source_row["sheet_role"] == "RISK":
        code = source_row.get("source_id")
        if not code:
            raise BusinessError("DISABLED_RISK_CODE_REQUIRED", "请先填写风险编号，再启用关联检查", status_code=409)
        if code in existing_risks:
            raise BusinessError("DISABLED_RISK_CODE_DUPLICATE", "停用风险编号与现有风险重复", status_code=409, details={"risk_code": code})
        selected_risks.add(code)
    else:
        selected_checks.add(source_row["row_key"])
    while True:
        before = (len(selected_checks), len(selected_risks))
        for row_key in tuple(selected_checks):
            for code in risks_of(row_key):
                matches = disabled_risks.get(code, [])
                if len(matches) > 1:
                    raise BusinessError("DISABLED_RISK_CODE_DUPLICATE", "停用风险编号重复，无法确定关联风险", status_code=409, details={"risk_code": code})
                if matches and code in existing_risks:
                    raise BusinessError("DISABLED_RISK_CODE_DUPLICATE", "停用风险编号与现有风险重复", status_code=409, details={"risk_code": code})
                if not matches and code not in existing_risks:
                    raise BusinessError("RISK_REF_UNKNOWN", "检查规则引用的风险名称或编号不存在", status_code=409, details={"risk_code": code, "row_key": row_key})
                if not matches and not existing_risks[code].enabled:
                    raise BusinessError("DISABLED_RISK_NOT_ENABLED", "关联风险未启用，不能写入来源检查", status_code=409, details={"risk_code": code})
                selected_risks.add(code)
        for code in tuple(selected_risks):
            for row_key, references in possible_risks.items():
                if any(risk_code_by_reference.get(value, value) == code for value in references) and code in risks_of(row_key):
                    selected_checks.add(row_key)
        if before == (len(selected_checks), len(selected_risks)):
            break
    if source_row["sheet_role"] == "RISK" and not selected_checks:
        raise BusinessError("RISK_SOURCE_MISSING", "风险没有关联的检查规则，不能启用", status_code=409)

    selected_rows = [
        item for item in rows
        if item["row_key"] in selected_checks
        or (item["sheet_role"] == "RISK" and item.get("source_id") in selected_risks)
    ]
    return selected_rows, selected_risks


def preview_disabled_rule_group(db: Session, scenario_code: str, row_key: str) -> dict[str, Any]:
    """只读预览一次成组启用会触及哪些原始行及已有风险。"""
    template = template_or_404(db, scenario_code)
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == template.scenario_code))
    if draft is None:
        raise BusinessError("DISABLED_SOURCE_ROW_NOT_FOUND", "停用来源行不存在", status_code=404)
    rows = list(draft.disabled_source_rows or [])
    source_row = next((item for item in rows if item["row_key"] == row_key), None)
    if source_row is None:
        raise BusinessError("DISABLED_SOURCE_ROW_NOT_FOUND", "停用来源行不存在", status_code=404)
    if source_row["sheet_role"] not in {"MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK"}:
        raise BusinessError("DISABLED_SOURCE_ROW_ROLE_UNSUPPORTED", "该来源行不属于检查与风险组", status_code=409)
    stored_rules = list(db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all())
    existing_risks = _draft_risks_by_code(stored_rules)
    selected_rows, risk_codes = _disabled_rule_group_rows(source_row, rows, existing_risks)
    return {
        "revision_no": draft.revision_no,
        "rows": [
            {
                "row_key": item["row_key"],
                "sheet_role": item["sheet_role"],
                "sheet_name": item["sheet_name"],
                "row_no": item["source"]["row_no"],
                "source_id": item.get("source_id"),
            }
            for item in selected_rows
        ],
        "updated_risk_codes": sorted(risk_codes.intersection(existing_risks)),
    }


def _activate_disabled_rule_group_locked(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    source_row: dict[str, Any],
    rows: list[dict[str, Any]],
) -> ScenarioDraft:
    """启用检查与风险的关联闭包，并在同一修订中更新风险来源。"""
    check_roles = {"MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE"}
    nodes, sheets, materials, fields, stored_rules, _sections = _draft_rows(db, draft.id)
    node_by_code = {item.node_code: item for item in nodes}
    binding_by_role = {item.sheet_role: item for item in sheets}
    profile = scenario_schema.effective_profile(draft.schema_json)
    profile_version = draft.profile_version or ""
    existing_risks = _draft_risks_by_code(stored_rules)
    selected_rows, selected_risks = _disabled_rule_group_rows(source_row, rows, existing_risks)
    risk_codes_by_reference = {code: code for code in selected_risks}
    risk_codes_by_reference.update({item.name: code for code, item in existing_risks.items() if code in selected_risks})
    for item in selected_rows:
        if item["sheet_role"] != "RISK" or not item.get("source_id"):
            continue
        name = next((column["value"] for column in item["columns"] if column["key"] == "name"), "")
        if name:
            risk_codes_by_reference[name] = item["source_id"]

    material_items: list[dict[str, Any]] = []
    for item in materials:
        if not item.source_ref:
            continue
        material_items.append({
            "candidate_ref": item.material_key, "source_id": item.source_ref, "name": item.name,
        })
    material_by_code = node_rulebook._reference_index(material_items, kind="材料")
    field_by_code: dict[str, dict[str, Any]] = {}
    field_names: set[tuple[str, str]] = set()
    field_catalog: dict[tuple[str, str], dict[str, Any]] = {}
    material_codes_by_key = {item["candidate_ref"]: item["source_id"] for item in material_items}
    for item in fields:
        material_code = material_codes_by_key.get(item.material_key)
        if material_code is None:
            continue
        identity = (material_code, item.name)
        if identity in field_names:
            raise BusinessError("DISABLED_FIELD_NAME_DUPLICATE", "同一材料下的字段名称重复，无法解析内部校验引用", status_code=409)
        field_names.add(identity)
        field_catalog[identity] = {
            "candidate_ref": item.field_key,
            "name": item.name,
            "material_ref": item.material_key,
            "value_type": item.value_type,
            "unit": item.unit,
            "currency": item.currency,
            "artifact_source": item.artifact_source,
        }
        if item.source_ref:
            if item.source_ref in field_by_code:
                raise BusinessError("DISABLED_FIELD_CODE_DUPLICATE", "现有字段编号重复，无法解析交叉校验引用", status_code=409)
            field_by_code[item.source_ref] = {
                "candidate_ref": item.field_key,
                "source_id": item.source_ref,
                "name": item.name,
                "material_ref": item.material_key,
                "value_type": item.value_type,
                "unit": item.unit,
                "currency": item.currency,
                "artifact_source": item.artifact_source,
            }

    converted: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    effective_rows: list[dict[str, Any]] = []
    for item in selected_rows:
        role = item["sheet_role"]
        binding = binding_by_role.get(role)
        node = node_by_code.get(binding.node_code) if binding is not None else None
        definition = unified_profile.sheet_definition(profile, role)
        if node is None or not node.enabled or node.node_type not in definition["node_types"]:
            raise BusinessError(
                "DISABLED_SOURCE_ROW_NODE_INVALID", "停用来源行所属工作表未绑定可用的业务节点",
                status_code=409, details={"sheet_role": role, "row_key": item["row_key"]},
            )
        values = {column["key"]: column["value"] for column in item["columns"]}
        if values.get("enabled") != "否":
            raise BusinessError("DISABLED_SOURCE_ROW_STATE_INVALID", "关联来源行不是停用状态，无法整组启用", status_code=409)
        effective = {
            **item,
            "columns": [
                {**column, "value": "是"} if column["key"] == "enabled" else dict(column)
                for column in item["columns"]
            ],
        }
        conversion = node_rulebook.convert_disabled_config_row(
            effective,
            profile_version=profile_version,
            material_by_code=material_by_code,
            field_by_code=field_by_code,
            field_catalog=field_catalog,
            profile=profile,
        )
        if conversion.notes:
            raise BusinessError(
                "DISABLED_SOURCE_ROW_ISSUES_UNRESOLVED", "关联来源行仍有未确认的业务口径，请先补齐后再启用",
                status_code=409,
                details={"row_key": item["row_key"], "issues": [note.to_payload() for note in conversion.notes]},
            )
        candidates = conversion.proposals["rules"]
        if not candidates or any(outcome.disposition != "CANDIDATE" for outcome in conversion.outcomes):
            raise BusinessError("DISABLED_SOURCE_ROW_CONVERSION_INVALID", "关联来源行未形成业务规则", status_code=409)
        converted.extend((candidate, item, node.node_code) for candidate in candidates)
        effective_rows.append(effective)

    new_rules: list[dict[str, Any]] = []
    source_keys_by_risk: dict[str, list[str]] = {code: [] for code in selected_risks}
    for candidate, item, node_code in converted:
        role = item["sheet_role"]
        code = candidate["source_id"]
        material_code: str | None = None
        if role == "INTERNAL_VALIDATE":
            branch_refs = {entry["material_ref"] for entry in candidate["inputs"]}
            if len(branch_refs) != 1:
                raise BusinessError("IMPORT_RULE_BRANCH_INVALID", "内部校验分支必须且只能对应一个材料", status_code=409)
            material_code = material_codes_by_key.get(next(iter(branch_refs)))
        rule_key = manifest_module.business_rule_key(
            template.scenario_code, node_by_code[node_code].node_type, code, material_code=material_code
        )
        inputs = [
            {"field_key": entry["field_ref"], "material_key": entry["material_ref"]}
            for entry in candidate.get("inputs", [])
        ]
        material_scope = list(candidate.get("material_refs") or [])
        new_rules.append({
            "rule_key": rule_key,
            "rule_code": code,
            "name": candidate["name"],
            "node": node_code,
            "node_type": node_by_code[node_code].node_type,
            "operator": candidate["operator"],
            "enabled": True,
            "inputs": inputs,
            "outputs": list(candidate.get("output_field_refs") or []),
            "materials": material_scope,
            "parameters": dict(candidate.get("parameters") or {}),
            "requirement": candidate.get("requirement") or "",
            "description": (candidate.get("requirement") or "") if role == "RISK" else "",
            "applicability": {"kind": "ALWAYS", "material_keys": list(dict.fromkeys([
                *(entry["material_key"] for entry in inputs), *material_scope,
            ]))},
            "source_node": candidate.get("source_node"),
            "source_rule_key": None,
            "source_rule_keys": [],
            "source_rule_codes": list(candidate.get("source_rule_codes") or []),
            "check_level": candidate.get("check_level"),
            "check_operator": candidate.get("check_operator"),
            "risk_level_value": candidate.get("risk_level_value"),
            "risk_condition": candidate.get("risk_condition"),
            "config_status": candidate.get("config_status"),
            "quality_supported": candidate.get("quality_supported"),
            "source_ref": code,
            "origin": _origin_of_source_row(item),
        })
        if role in check_roles:
            for reference in candidate.get("risk_names") or []:
                risk_code = risk_codes_by_reference.get(reference)
                if risk_code is None:
                    raise BusinessError(
                        "RISK_REF_UNKNOWN", "检查规则引用的风险名称或编号不存在", status_code=409,
                        details={"risk_reference": reference, "row_key": item["row_key"]},
                    )
                source_keys_by_risk[risk_code].append(rule_key)

    existing_keys = {item.rule_key for item in stored_rules}
    new_keys = [item["rule_key"] for item in new_rules]
    if len(set(new_keys)) != len(new_keys) or existing_keys.intersection(new_keys):
        raise BusinessError("DISABLED_RULE_IDENTITY_DUPLICATE", "关联规则的业务身份与现有草稿或本组其他规则冲突", status_code=409)
    missing_nodes = sorted({item.node_code for item in stored_rules if item.node_code not in node_by_code})
    if missing_nodes:
        raise BusinessError("DISABLED_RULE_NODE_MISSING", "现有规则所属节点不存在，无法核对业务编号", status_code=409, details={"node_codes": missing_nodes})
    manifest_module.ensure_unique_rule_codes([
        {
            "rule_key": item.rule_key,
            "rule_code": item.source_ref or (item.rule_json or {}).get("rule_code"),
            "node": item.node_code,
            "node_type": node_by_code[item.node_code].node_type,
            "operator": item.operator_code,
            "inputs": (item.rule_json or {}).get("inputs") or [],
            "materials": (item.rule_json or {}).get("materials") or [],
        }
        for item in stored_rules
    ] + new_rules)

    rule_by_key = {item["rule_key"]: item for item in new_rules}
    node_positions = {item.node_code: item.sequence_no for item in nodes}
    for code in selected_risks:
        new_risk = next((item for item in new_rules if item["operator"] == "RISK_TRIGGER" and item["rule_code"] == code), None)
        existing_risk = existing_risks.get(code)
        if new_risk is not None and existing_risk is not None:
            raise BusinessError("DISABLED_RISK_CODE_DUPLICATE", "风险编号与现有草稿重复", status_code=409, details={"risk_code": code})
        if new_risk is None and (existing_risk is None or not existing_risk.enabled):
            raise BusinessError("DISABLED_RISK_NOT_ENABLED", "关联风险未启用，不能写入来源检查", status_code=409, details={"risk_code": code})
        source_keys = list(dict.fromkeys(source_keys_by_risk[code]))
        if new_risk is not None and not source_keys:
            raise BusinessError("RISK_SOURCE_MISSING", "风险没有关联的检查规则，不能启用", status_code=409, details={"risk_code": code})
        risk_node = new_risk["node"] if new_risk is not None else existing_risk.node_code
        source_node = new_risk.get("source_node") if new_risk is not None else (existing_risk.rule_json or {}).get("source_node")
        if risk_node not in node_positions or (source_node and source_node not in SOURCE_NODE_RULE_NODES):
            raise BusinessError("DISABLED_RISK_NODE_INVALID", "关联风险的节点配置无效", status_code=409, details={"risk_code": code, "node_code": risk_node, "source_node": source_node})
        for key in source_keys:
            check = rule_by_key[key]
            if node_positions[risk_node] <= node_positions[check["node"]]:
                raise BusinessError("MANIFEST_NODE_ORDER_INVALID", "风险节点必须位于关联检查节点之后", status_code=409, details={"risk_code": code, "rule_key": key})
            if source_node and check["node_type"] not in SOURCE_NODE_RULE_NODES[source_node]:
                raise BusinessError("MANIFEST_RISK_SOURCE_NODE_MISMATCH", "关联检查不属于风险声明的来源节点类型", status_code=409, details={"risk_code": code, "rule_key": key})
        if new_risk is not None:
            new_risk["source_rule_key"] = source_keys[0]
            new_risk["source_rule_keys"] = source_keys
        else:
            definition = dict(existing_risk.rule_json or {})
            merged = list(dict.fromkeys([
                *(definition.get("source_rule_keys") or []),
                *([definition["source_rule_key"]] if definition.get("source_rule_key") else []),
                *source_keys,
            ]))
            definition["source_rule_key"] = merged[0] if merged else None
            definition["source_rule_keys"] = merged
            existing_risk.rule_json = definition

    editable = manifest_module.editable_config_of({"rules": new_rules}, namespace=template.scenario_code)
    for index, item in enumerate(editable["rules"], start=_next_sort_no(db, ScenarioDraftRule, draft.id)):
        db.add(ScenarioDraftRule(
            draft_id=draft.id,
            sort_no=index,
            **{key: value for key, value in item.items() if key != "is_system"},
        ))
    activated_keys = {item["row_key"] for item in selected_rows}
    draft.disabled_source_rows = [item for item in rows if item["row_key"] not in activated_keys]
    _save_draft(
        db, template, draft,
        operation_type="DRAFT_SOURCE_ROW_ACTIVATE",
        object_name=template.scenario_name,
        detail={
            "row_keys": sorted(activated_keys),
            "rule_keys": new_keys,
            "risk_codes": sorted(selected_risks),
            "source_rows": [
                {"row_key": item["row_key"], "original_source": item["source"], "effective_columns": effective["columns"]}
                for item, effective in zip(selected_rows, effective_rows, strict=True)
            ],
        },
    )
    db.refresh(draft)
    return draft


def _activate_disabled_calculation_locked(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    definition: dict[str, Any],
    rows: list[dict[str, Any]],
) -> ScenarioDraft:
    """将停用的 06 定义与同编号的 07/08 行一起转换。"""
    row_key = definition["row_key"]
    values = {column["key"]: column["value"] for column in definition["columns"]}
    rule_code = values.get("rule_code", "").strip()
    if not rule_code:
        raise BusinessError("DISABLED_CALC_CODE_REQUIRED", "先填写计算定义的规则编号，再启用", status_code=409)
    if values.get("enabled") != "否":
        raise BusinessError("DISABLED_CALC_STATE_INVALID", "来源行不是停用状态，不能重复启用", status_code=409)
    if any(
        row["sheet_role"] == "CALC_DEFINITION" and row["row_key"] != row_key
        and any(column["key"] == "rule_code" and column["value"] == rule_code for column in row["columns"])
        for row in rows
    ):
        raise BusinessError(
            "DISABLED_CALC_CODE_DUPLICATE",
            "停用计算定义的规则编号重复，无法确定参数和判定归属",
            status_code=409,
            details={"rule_code": rule_code},
        )
    children = [
        row for row in rows
        if row["sheet_role"] in {"CALC_PARAMETER", "CALC_JUDGEMENT"}
        and any(column["key"] == "rule_code" and column["value"] == rule_code for column in row["columns"])
    ]
    selected_rows = [json.loads(json.dumps(definition)), *json.loads(json.dumps(children))]
    selected_rows[0]["columns"] = [
        {**column, "value": "是"} if column["key"] == "enabled" else column
        for column in selected_rows[0]["columns"]
    ]
    nodes, sheets, _materials, fields, _rules, _sections = _draft_rows(db, draft.id)
    node_by_code = {node.node_code: node for node in nodes}
    node_codes = {sheet.sheet_role: sheet.node_code for sheet in sheets if sheet.node_code}
    node_code = node_codes.get("CALC_DEFINITION")
    node = node_by_code.get(node_code)
    if node is None or node.node_type != "CALCULATION" or not node.enabled:
        raise BusinessError(
            "DISABLED_CALC_NODE_INVALID",
            "计算定义绑定的计算节点不存在或未启用，不能启用这组来源行",
            status_code=409,
            details={"node_code": node_code},
        )
    converted = financial_report.convert_disabled_calculation_rows(
        selected_rows,
        scenario_code=template.scenario_code,
        node_codes=node_codes,
        profile_version=draft.profile_version or "",
        profile=scenario_schema.effective_profile(draft.schema_json),
    )
    activated = converted["calculations"]
    if len(activated) != 1 or activated[0]["rule_code"] != rule_code:
        raise BusinessError(
            "DISABLED_CALC_CONVERSION_INVALID",
            "停用计算定义未能转换成唯一计算规则，草稿未修改",
            status_code=409,
            details={"row_key": row_key, "rule_code": rule_code},
        )
    source_rows = {
        (row["sheet_role"], row["source"]["row_no"]): row for row in selected_rows
    }
    activated[0]["origin"] = _origin_of_source_row(definition)
    for role, key in (("CALC_PARAMETER", "parameters"), ("CALC_JUDGEMENT", "bands")):
        for item in activated[0].get(key) or []:
            source_row = source_rows[(role, item["source"]["row_no"])]
            item["origin"] = _origin_of_source_row(source_row)
    candidates, frozen = _calculation_store(draft.calculation_json)
    if frozen:
        raise BusinessError(
            "DISABLED_CALC_FROZEN_DRAFT",
            "当前草稿只有历史计算编译产物，不能与新计算候选混合",
            status_code=409,
        )
    if any(item.get("rule_code") == rule_code for item in candidates):
        raise BusinessError(
            "DISABLED_CALC_CODE_DUPLICATE", "规则编号与现有计算规则重复", status_code=409,
            details={"rule_code": rule_code},
        )
    field_codes = [field.source_ref for field in fields if field.source_ref]
    if len(field_codes) != len(set(field_codes)):
        raise BusinessError(
            "DISABLED_CALC_FIELD_CODE_DUPLICATE",
            "字段业务编号重复，计算参数无法唯一定位字段",
            status_code=409,
        )
    activated[0]["activated_source_rows"] = [
        {"row_key": row["row_key"], "sheet_role": row["sheet_role"], "source": row["source"], "columns": row["columns"]}
        for row in selected_rows
    ]
    merged = [*candidates, activated[0]]
    manifest_module.compile_calculations(
        merged,
        field_code_index={
            field.source_ref: {
                "field_key": field.field_key,
                "name": field.name,
                "value_type": field.value_type,
                "unit": field.unit,
                "currency": field.currency,
                "material_key": field.material_key,
                "period_scope": field.period_scope,
                "statement_scope": field.statement_scope,
            }
            for field in fields if field.source_ref
        },
        node_instances=[
            {"node_code": node.node_code, "node_type": node.node_type, "enabled": node.enabled}
            for node in nodes
        ],
    )
    activated_keys = {row["row_key"] for row in selected_rows}
    draft.disabled_source_rows = [row for row in rows if row["row_key"] not in activated_keys]
    draft.calculation_json = merged
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_CALC_ACTIVATE",
        object_name=template.scenario_name,
        detail={"rule_code": rule_code, "source_row_keys": sorted(activated_keys)},
    )
    db.refresh(draft)
    return draft


# 节点实例（用户配置的流程）


def _draft_node_or_404(db: Session, draft_id: int, node_code: str) -> ScenarioDraftNode:
    node = db.scalar(
        select(ScenarioDraftNode).where(
            ScenarioDraftNode.draft_id == draft_id,
            ScenarioDraftNode.node_code == node_code,
        )
    )
    if node is None:
        raise BusinessError(
            "SCENARIO_NODE_NOT_FOUND",
            "该业务节点不在当前场景配置里",
            status_code=404,
            details={"node_code": node_code},
        )
    return node


def _node_type_of_draft_node(db: Session, draft_id: int, node_code: str) -> str:
    """草稿里某个节点实例的**能力类型**。

    规则表单提交的 `node` 是实例码，算子约束与节点专属校验比的是能力类型。
    实例不存在时明确报错：允许一条规则挂到并不存在的节点上，发布期才会以
    「规则指向不存在的节点」报出，那时已经看不出真实原因。
    """
    node = db.scalar(
        select(ScenarioDraftNode).where(
            ScenarioDraftNode.draft_id == draft_id,
            ScenarioDraftNode.node_code == node_code,
        )
    )
    if node is None:
        raise BusinessError(
            "RULE_NODE_UNKNOWN",
            f"规则归属的节点“{node_code}”不在当前场景配置里：请先在场景配置里建立该节点",
            status_code=400,
            details={"node_code": node_code},
        )
    return node.node_type


def select_draft_sheets(db: Session, scenario_code: str, selections: list[dict[str, Any]]) -> ScenarioDraft:
    """拒绝在流程节点页单独改写 Sheet 绑定。

    `scenario_draft_sheet` 只在「写入场景草稿」整体成功时，与材料、字段、规则、
    计算和报告一起替换。单独保存一份 Sheet 选择会和 `import_sheet` 形成两套真源。
    """
    raise BusinessError(
        "SHEET_BINDING_VIA_IMPORT",
        "场景的 Sheet 绑定只能在导入作业确认后，由「写入所属场景草稿」整体替换，不能在流程节点页单独改选",
        status_code=409,
        details={"scenario_code": scenario_code, "submitted_count": len(selections)},
    )


def _assert_node_type_known(node_type: str) -> str:
    """节点能力类型必须来自统一模板协议。"""
    known = {node["node_type"] for node in unified_profile.load_unified_profile()["node_types"]}
    if node_type not in known:
        raise BusinessError(
            "SCENARIO_NODE_TYPE_UNKNOWN",
            f"节点类型“{node_type}”不在统一模板协议里",
            details={"node_type": node_type, "known": sorted(known)},
        )
    return node_type


def _assert_depends_on(draft_id: int, nodes: list[ScenarioDraftNode], node_code: str, depends_on: list[str]) -> None:
    """依赖必须指向本场景真实存在的节点实例，且不得自依赖。

    缺失依赖与自依赖在这里报出；循环依赖由发布期的 DAG 校验兜底（那时能看到完整图）。
    """
    if node_code in depends_on:
        raise BusinessError(
            "SCENARIO_NODE_SELF_DEPENDENCY",
            f"节点“{node_code}”不能依赖自己",
            details={"node_code": node_code},
        )
    known = {item.node_code for item in nodes}
    unknown = sorted(set(depends_on) - known)
    if unknown:
        raise BusinessError(
            "SCENARIO_NODE_DEPENDENCY_MISSING",
            "节点依赖的上游节点不在本场景的节点清单里："
            + "、".join(f"“{item}”" for item in unknown),
            details={"node_code": node_code, "missing": unknown, "configured_nodes": sorted(known)},
        )


def _assert_node_removable(db: Session, draft_id: int, node_code: str) -> None:
    """被规则或下游节点依赖的节点不能删除。

    删除它会让规则指向不存在的节点、或让下游节点的依赖悬空，而两者在页面上
    都只表现为「配置看起来还在」。因此这里明确报出引用清单，由用户先处理引用。
    """
    referencing_rules = [
        item.rule_key
        for item in db.scalars(
            select(ScenarioDraftRule).where(
                ScenarioDraftRule.draft_id == draft_id, ScenarioDraftRule.node_code == node_code
            )
        ).all()
    ]
    downstream = [
        item.node_code
        for item in db.scalars(select(ScenarioDraftNode).where(ScenarioDraftNode.draft_id == draft_id)).all()
        if node_code in (item.depends_on or [])
    ]
    if referencing_rules or downstream:
        raise BusinessError(
            "SCENARIO_NODE_IN_USE",
            "该节点仍被引用，不能删除：请先把引用它的规则与下游节点改到别的节点上",
            details={
                "node_code": node_code,
                "rule_keys": sorted(referencing_rules),
                "downstream_nodes": sorted(downstream),
            },
        )


def create_draft_node(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    nodes = list(db.scalars(select(ScenarioDraftNode).where(ScenarioDraftNode.draft_id == draft.id)).all())
    node_code = str(payload["node_code"]).strip()
    if any(item.node_code == node_code for item in nodes):
        raise BusinessError(
            "SCENARIO_NODE_DUPLICATED",
            f"节点实例码“{node_code}”在当前场景里已存在；同一场景内实例码必须唯一",
            details={"node_code": node_code},
        )
    node_type = _assert_node_type_known(str(payload["node_type"]))
    depends_on = [str(item) for item in (payload.get("depends_on") or [])]
    _assert_depends_on(draft.id, nodes, node_code, depends_on)
    sequence_no = int(payload.get("sequence_no") or (max((item.sequence_no for item in nodes), default=0) + 1))
    db.add(
        ScenarioDraftNode(
            draft_id=draft.id,
            node_code=node_code,
            node_type=node_type,
            name=str(payload.get("name") or node_code),
            sequence_no=sequence_no,
            depends_on=depends_on,
            config_json=payload.get("config_json"),
            enabled=bool(payload.get("enabled", True)),
            sort_no=sequence_no,
        )
    )
    _save_draft(
        db,
        template,
        draft,
        operation_type="UPDATE",
        object_name=template.scenario_name,
        detail={"action": "CREATE_NODE", "node_code": node_code, "node_type": node_type},
    )
    db.refresh(draft)
    return draft


def update_draft_node(db: Session, scenario_code: str, node_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    node = _draft_node_or_404(db, draft.id, node_code)
    nodes = list(db.scalars(select(ScenarioDraftNode).where(ScenarioDraftNode.draft_id == draft.id)).all())
    if "node_type" in payload and payload["node_type"] is not None:
        node.node_type = _assert_node_type_known(str(payload["node_type"]))
    if "name" in payload and payload["name"] is not None:
        node.name = str(payload["name"])
    if "sequence_no" in payload and payload["sequence_no"] is not None:
        node.sequence_no = int(payload["sequence_no"])
        node.sort_no = node.sequence_no
    if "enabled" in payload and payload["enabled"] is not None:
        node.enabled = bool(payload["enabled"])
    if "config_json" in payload:
        node.config_json = payload["config_json"]
    if "depends_on" in payload and payload["depends_on"] is not None:
        depends_on = [str(item) for item in payload["depends_on"]]
        _assert_depends_on(draft.id, nodes, node_code, depends_on)
        node.depends_on = depends_on
    _save_draft(
        db,
        template,
        draft,
        operation_type="UPDATE",
        object_name=template.scenario_name,
        detail={"action": "UPDATE_NODE", "node_code": node_code},
    )
    db.refresh(draft)
    return draft


def delete_draft_node(db: Session, scenario_code: str, node_code: str) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    node = _draft_node_or_404(db, draft.id, node_code)
    _assert_node_removable(db, draft.id, node_code)
    db.delete(node)
    _save_draft(
        db,
        template,
        draft,
        operation_type="UPDATE",
        object_name=template.scenario_name,
        detail={"action": "DELETE_NODE", "node_code": node_code},
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
    """草稿计算配置的核对行。冻结产物与导入候选都按同一组字段展示。"""
    candidates, frozen = _calculation_store(raw)
    source = frozen if frozen is not None else candidates
    return [
        {
            "rule_code": item.get("rule_code"),
            "name": item.get("rule_name") or item.get("name"),
            "node_code": item.get("node_code") or item.get("node_instance_code"),
            "formula": item.get("formula") or item.get("expression"),
            "result_code": item.get("result_code"),
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "status": item.get("status"),
            "frozen": frozen is not None,
            "origin": item.get("origin"),
            "parameter_sources": [
                {
                    "code": parameter.get("parameter_code"),
                    "origin": (
                        (item.get("parameter_origins") or {}).get(parameter.get("parameter_code"))
                        if frozen is not None else parameter.get("origin")
                    ),
                }
                for parameter in item.get("parameters") or []
            ],
            "band_sources": [
                {
                    "code": band.get("condition_code"),
                    "origin": (
                        (item.get("band_origins") or {}).get(band.get("condition_code"))
                        if frozen is not None else band.get("origin")
                    ),
                }
                for band in item.get("bands") or []
            ],
        }
        for item in source
        if isinstance(item, dict)
    ]


def assess_publish(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    *,
    acknowledged_capability_gaps: list[str] | None = None,
) -> dict[str, Any]:
    """场景发布的唯一预检。草稿就绪状态与 `publish_draft` 必须调用这里。

    失败抛出 BusinessError，不创建版本。保存草稿的调用方捕获该错误并写入
    `sync_error`；真正发布时原样抛出。
    """
    from .import_service import _capability_blockers

    acknowledged = list(acknowledged_capability_gaps or [])
    nodes, sheets, _materials, _fields, rules, _sections = _draft_rows(db, draft.id)
    profile = scenario_schema.effective_profile(draft.schema_json)
    known_types = {str(item["node_type"]) for item in profile["node_types"]}
    node_by_code = {item.node_code: item for item in nodes}
    if not sheets:
        raise BusinessError(
            "SCENARIO_SHEET_REQUIRED",
            "场景尚未确认任何 Sheet 绑定，配置未生效：请先导入规则库，确认 Sheet 与节点绑定",
            status_code=409,
            details={"scenario_code": template.scenario_code},
        )
    for sheet in sheets:
        label = unified_profile.sheet_role_label(profile, sheet.sheet_role)
        if not sheet.node_code:
            raise BusinessError(
                "SCENARIO_SHEET_NODE_REQUIRED",
                f"Sheet“{label}”尚未绑定节点，不能发布",
                status_code=409,
                details={"scenario_code": template.scenario_code, "sheet_role": sheet.sheet_role},
            )
        node = node_by_code.get(sheet.node_code)
        if node is None:
            raise BusinessError(
                "SCENARIO_SHEET_NODE_UNKNOWN",
                f"Sheet“{label}”绑定的节点“{sheet.node_code}”不在当前场景节点清单里",
                status_code=409,
                details={
                    "scenario_code": template.scenario_code,
                    "sheet_role": sheet.sheet_role,
                    "node_code": sheet.node_code,
                },
            )
        if not node.enabled:
            raise BusinessError(
                "SCENARIO_SHEET_NODE_DISABLED",
                f"Sheet“{label}”绑定的节点“{node.node_code}”已停用，不能发布",
                status_code=409,
                details={
                    "scenario_code": template.scenario_code,
                    "sheet_role": sheet.sheet_role,
                    "node_code": node.node_code,
                },
            )
        allowed = unified_profile.node_types_of_sheet_role(profile, sheet.sheet_role)
        if node.node_type not in allowed:
            raise BusinessError(
                "SCENARIO_SHEET_NODE_TYPE_MISMATCH",
                f"Sheet“{label}”不能绑定到节点类型“{node.node_type}”上",
                status_code=409,
                details={
                    "sheet_role": sheet.sheet_role,
                    "node_code": node.node_code,
                    "node_type": node.node_type,
                },
            )
    # 每个节点的能力类型都必须在统一协议里：只校验「被 Sheet 或规则引用到的节点」
    # 会漏掉用户建好但暂时没挂规则的节点，而它的类型一旦是自由文本，
    # 节点注册表在运行期找不到 handler，发布却已经放行。
    for node in nodes:
        if str(node.node_type) not in known_types:
            raise BusinessError(
                "MANIFEST_NODE_TYPE_INVALID",
                f"节点“{node.node_code}”的能力类型“{node.node_type}”不在统一模板协议里",
                status_code=409,
                details={"node_code": node.node_code, "node_type": node.node_type},
            )
    for rule in rules:
        if not rule.enabled:
            continue
        node = node_by_code.get(rule.node_code)
        if node is None:
            raise BusinessError(
                "MANIFEST_RULE_NODE_MISSING",
                f"启用规则“{rule.name}”指向的节点“{rule.node_code}”不在本场景节点清单里",
                status_code=409,
                details={"node_code": rule.node_code, "rule_key": rule.rule_key, "name": rule.name},
            )
        if not node.enabled:
            raise BusinessError(
                "SCENARIO_RULE_NODE_DISABLED",
                f"启用规则“{rule.name}”挂在已停用的节点“{node.node_code}”上，不能发布",
                status_code=409,
                details={"node_code": node.node_code, "rule_key": rule.rule_key, "name": rule.name},
            )
    manifest = compile_draft_manifest(
        db, template, draft, acknowledged_capability_gaps=acknowledged
    )
    instance_type = {item.node_code: item.node_type for item in nodes}
    enabled_nodes = {item.node_code for item in nodes if item.enabled}
    for rule in manifest.get("rules", []):
        if not rule.get("enabled", True):
            continue
        node_code = str(rule.get("node") or "")
        node_type = rule.get("node_type")
        if node_code not in instance_type:
            raise BusinessError(
                "MANIFEST_RULE_NODE_MISSING",
                f"启用规则“{rule.get('name')}”的节点“{node_code}”不是当前场景的节点实例码",
                status_code=409,
                details={"node_code": node_code, "rule_key": rule.get("rule_key"), "name": rule.get("name")},
            )
        if node_code not in enabled_nodes:
            raise BusinessError(
                "SCENARIO_RULE_NODE_DISABLED",
                f"启用规则“{rule.get('name')}”挂在已停用的节点“{node_code}”上，不能发布",
                status_code=409,
                details={"node_code": node_code, "rule_key": rule.get("rule_key"), "name": rule.get("name")},
            )
        if not isinstance(node_type, str) or node_type not in known_types or instance_type[node_code] != node_type:
            raise BusinessError(
                "MANIFEST_NODE_IDENTITY_MIXED",
                f"规则“{rule.get('name')}”的节点实例码与能力类型不一致",
                status_code=409,
                details={
                    "node_code": node_code,
                    "node_type": node_type if isinstance(node_type, str) else None,
                    "rule_key": rule.get("rule_key"),
                },
            )
    for item in manifest.get("calculations") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("node_instance_code") or item.get("node_code") or "")
        node = node_by_code.get(code)
        # 计算规则必须落在**存在、启用、且能力类型为 CALCULATION** 的节点上：
        # 只看「存在且启用」会让一条计算规则挂到字段抽取节点上，运行期才暴露。
        if node is None or not node.enabled or node.node_type != "CALCULATION":
            raise BusinessError(
                "SCENARIO_RULE_NODE_DISABLED",
                f"计算规则“{item.get('rule_code')}”挂在不存在、已停用或不是计算节点的节点“{code}”上，不能发布",
                status_code=409,
                details={"node_code": code, "rule": item.get("rule_code")},
            )
    coverage = manifest.get("capability_coverage") if isinstance(manifest.get("capability_coverage"), dict) else {}
    blockers = _capability_blockers(coverage, acknowledged)
    if blockers:
        first = blockers[0]
        rule_keys = [
            entry.get("details", {}).get("rule_key")
            for entry in blockers
            if isinstance(entry.get("details"), dict) and entry.get("details", {}).get("rule_key")
        ]
        raise BusinessError(
            str(first.get("code") or "CAPABILITY_UNIMPLEMENTED"),
            "；".join(str(entry.get("message") or "") for entry in blockers),
            status_code=409,
            details={"rule_keys": rule_keys, "blockers": blockers},
        )
    return manifest


def _refresh_publish_readiness(
    db: Session, template: ScenarioTemplate, draft: ScenarioDraft
) -> bool:
    """重新计算「这份草稿现在能不能发布」，把阻断原因写进 `draft.sync_error`。

    与 `publish_draft` 调用同一个 `assess_publish`。确认列表为空：未逐条确认的
    能力缺口也是阻断原因。本函数不产生任何版本；保存草稿必须成功，因此只捕获
    BusinessError。其它异常继续抛出。
    """
    try:
        assess_publish(db, template, draft, acknowledged_capability_gaps=[])
    except BusinessError as failure:
        logger.info(
            "场景配置尚不能发布：scenario_code=%s code=%s message=%s",
            template.scenario_code,
            failure.code,
            failure.message,
        )
        draft.sync_error = _sync_error_detail(failure)
        return False
    draft.sync_error = None
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
        projected_type = node.get("node_type") or legacy_node_type_of(str(node["node_code"]))
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


def _assert_material_source_ref_available(
    db: Session, draft_id: int, source_ref: str | None, *, material_key: str | None = None
) -> None:
    if source_ref is None:
        return
    statement = select(ScenarioDraftMaterial).where(
        ScenarioDraftMaterial.draft_id == draft_id,
        ScenarioDraftMaterial.source_ref == source_ref,
    )
    if material_key is not None:
        statement = statement.where(ScenarioDraftMaterial.material_key != material_key)
    existing = db.scalar(statement.limit(1))
    if existing is not None:
        raise BusinessError(
            "MATERIAL_CODE_DUPLICATE",
            f"材料编号“{source_ref}”已由材料“{existing.name}”使用",
            status_code=409,
            details={"source_ref": source_ref, "material_key": existing.material_key},
        )


def create_draft_material(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增材料位。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
    source_ref = _normalized_source_ref(payload.get("source_ref"))
    _assert_material_source_ref_available(db, draft.id, source_ref)
    material_key = stable_key("mat", scenario_code, _item_ref())
    db.add(
        ScenarioDraftMaterial(
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
        _assert_material_source_ref_available(db, draft.id, source_ref, material_key=material_key)
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
    if source_ref is None:
        return
    statement = select(ScenarioDraftField).where(
        ScenarioDraftField.draft_id == draft_id,
        ScenarioDraftField.source_ref == source_ref,
    )
    if field_key is not None:
        statement = statement.where(ScenarioDraftField.field_key != field_key)
    existing = db.scalar(statement.limit(1))
    if existing is not None:
        raise BusinessError(
            "FIELD_CODE_DUPLICATE",
            f"字段编号“{source_ref}”已由字段“{existing.name}”使用",
            status_code=409,
            details={"source_ref": source_ref, "field_key": existing.field_key},
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
    db.add(
        ScenarioDraftField(
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
            period_scope=_field_scope(payload, "period_scope", "取数期间", FIELD_PERIOD_SCOPES),
            statement_scope=_field_scope(payload, "statement_scope", "报表口径", FIELD_STATEMENT_SCOPES),
            required=bool(payload.get("required", False)),
            instruction=str(payload.get("instruction") or ""),
            source_ref=source_ref,
            sort_no=_next_sort_no(db, ScenarioDraftField, draft.id),
        )
    )
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
        item.period_scope = _field_scope(payload, "period_scope", "取数期间", FIELD_PERIOD_SCOPES)
    if "statement_scope" in payload and payload["statement_scope"] is not None:
        item.statement_scope = _field_scope(
            payload, "statement_scope", "报表口径", FIELD_STATEMENT_SCOPES
        )
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "instruction" in payload and payload["instruction"] is not None:
        item.instruction = str(payload["instruction"])
    if "source_ref" in payload:
        source_ref = _normalized_source_ref(payload.get("source_ref"))
        _assert_field_source_ref_available(db, draft.id, source_ref, field_key=field_key)
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
    """删除草稿中的字段；仍被规则或报告章节引用时 409，不级联删除。"""
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
    previous_code = str((previous or {}).get("rule_code") or (previous or {}).get("source_ref") or "")
    existing_extraction = (
        operator == "LLM_EXTRACT"
        and existing_key is not None
        and isinstance(previous, dict)
        and previous.get("operator") == "LLM_EXTRACT"
        and previous_code
        and not manifest_module.is_system_rule(previous)
    )
    if operator in SYSTEM_OPERATORS and not existing_extraction:
        raise BusinessError("RULE_SYSTEM_READONLY", "字段抽取与报告生成规则由系统自动派生，不能手工创建", status_code=409)
    # `node` 是用户配置的**节点实例码**，算子约束比的是**能力类型**：
    # 实例码是用户自己起的名字，拿它去比能力类型约束会让所有规则都存不下来。
    node_type = _node_type_of_draft_node(db, draft.id, node)
    if operator not in OPERATOR_NODE_CONSTRAINTS or node_type not in OPERATOR_NODE_CONSTRAINTS[operator]:
        raise BusinessError(
            "RULE_OPERATOR_NODE_INVALID",
            "检查方式不能在所选阶段执行",
            status_code=400,
            details={"operator": operator, "node": node, "node_type": node_type},
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

    outputs: list[str] = []
    for field_key in payload.get("outputs") or []:
        field = field_map.get(field_key)
        if field is None:
            raise BusinessError("RULE_OUTPUT_FIELD_MISSING", "规则输出字段不存在", status_code=400, details={"field_key": field_key})
        if field_key not in outputs:
            outputs.append(field_key)
    if operator == "LLM_EXTRACT":
        if not str(payload.get("source_ref") or "").strip():
            raise BusinessError("RULE_CODE_REQUIRED", "显式字段抽取规则必须保留业务编号", status_code=400)
        if not outputs:
            raise BusinessError("RULE_EXTRACTION_OUTPUT_REQUIRED", "字段抽取规则必须选择输出字段", status_code=400)
        if any(field_map[key].material_key not in scope for key in outputs):
            raise BusinessError("RULE_EXTRACTION_MATERIAL_INVALID", "输出字段不属于规则适用材料", status_code=400)
    elif outputs:
        raise BusinessError("RULE_OUTPUT_UNEXPECTED", "当前检查方式不接受输出字段", status_code=400)

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
    comparison_operators = {"FIELD_EQUAL", "FIELD_NOT_EQUAL", "DATE_COMPARE", "DECIMAL_COMPARE", "DECIMAL_SUM_COMPARE"}
    if operator in comparison_operators:
        if len(inputs) < 2 or (operator != "DECIMAL_SUM_COMPARE" and len(inputs) != 2):
            raise BusinessError("RULE_INPUT_COUNT_INVALID", "加总比较至少需要左侧字段与一个加数" if operator == "DECIMAL_SUM_COMPARE" else "比较规则必须恰好选择两个输入字段", status_code=400, details={"rule": name})
        if node_type == "INTERNAL_VALIDATE" and len({item["material_key"] for item in inputs}) != 1:
            raise BusinessError("RULE_INTERNAL_MATERIAL_MISMATCH", "文件内部校验的左右字段必须属于同一份材料", status_code=400, details={"rule": name})
        input_types = [field_map[item["field_key"]].value_type for item in inputs]
        if operator == "DATE_COMPARE" and any(value_type != "DATE" for value_type in input_types):
            raise BusinessError("RULE_TYPE_INVALID", "日期比较只能选择日期类型字段", status_code=400, details={"rule": name})
        if operator in {"DECIMAL_COMPARE", "DECIMAL_SUM_COMPARE"} and any(value_type != "DECIMAL" for value_type in input_types):
            raise BusinessError("RULE_TYPE_INVALID", "金额比较只能选择金额类型字段", status_code=400, details={"rule": name})
        if operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"} and input_types[0] != input_types[1]:
            raise BusinessError("RULE_TYPE_INVALID", "字段比较的两个字段类型必须一致", status_code=400, details={"rule": name})
        units = {(field_map[item["field_key"]].unit or "").strip() for item in inputs} - {""}
        if len(units) > 1:
            raise BusinessError(
                "RULE_UNIT_MISMATCH",
                f"比较字段的单位不一致（{' / '.join(sorted(units))}），系统不做单位换算，请先统一单位或改用智能判断",
                status_code=400,
                details={"rule": name},
            )
        if operator in {"DECIMAL_COMPARE", "DECIMAL_SUM_COMPARE"} or "DECIMAL" in input_types or "DECIMAL_CN" in input_types:
            currencies = [
                currency.canonical_currency(field_map[item["field_key"]].currency)
                for item in inputs
            ]
            if any(not value for value in currencies):
                raise BusinessError(
                    "RULE_CURRENCY_MISSING",
                    "金额比较字段必须全部声明币种，系统不默认人民币",
                    status_code=400,
                    details={"rule": name},
                )
            if len(set(currencies)) > 1:
                raise BusinessError(
                    "RULE_CURRENCY_MISMATCH",
                    f"比较字段的币种不一致（{' / '.join(sorted(set(currencies)))}），系统不做汇率换算",
                    status_code=400,
                    details={"rule": name},
                )
    if operator == "LLM_VALIDATE":
        if not inputs:
            raise BusinessError("RULE_INPUT_COUNT_INVALID", "智能判断必须选择至少一个输入字段", status_code=400, details={"rule": name})
        if node_type == "INTERNAL_VALIDATE" and len(inputs) > 1:
            raise BusinessError("RULE_INPUT_COUNT_INVALID", "文件内部校验的智能判断最多只能引用一个字段", status_code=400, details={"rule": name})
    if node_type == "CROSS_VALIDATE" and (len(inputs) < 2 or (operator != "DECIMAL_SUM_COMPARE" and len(inputs) != 2)):
        raise BusinessError("RULE_INPUT_COUNT_INVALID", "文件交叉校验必须左右各引用一个字段", status_code=400, details={"rule": name})

    applicability_raw = payload.get("applicability") if isinstance(payload.get("applicability"), dict) else {}
    applicability_kind = str(applicability_raw.get("kind") or "ALWAYS")
    if applicability_kind not in {"ALWAYS", "MATERIALS_UPLOADED"}:
        raise BusinessError("RULE_APPLICABILITY_INVALID", "适用条件不受支持", status_code=400, details={"kind": applicability_kind})

    same_operator = isinstance(previous, dict) and previous.get("operator") == operator
    source_rule_key: str | None = None
    source_rule_keys: list[str] = []
    source_rule_codes: list[str] = []
    source_node: str | None = None
    if operator == "RISK_TRIGGER":
        declared_keys = payload.get("source_rule_keys") if "source_rule_keys" in payload else (
            (previous.get("source_rule_keys") or []) if same_operator else []
        )
        source_rule_keys = list(dict.fromkeys(str(key) for key in declared_keys if str(key)))
        single_key = str(payload.get("source_rule_key") or "")
        if "source_rule_keys" not in payload and single_key:
            source_rule_keys = [single_key]
        elif single_key and single_key not in source_rule_keys:
            source_rule_keys.insert(0, single_key)
        elif not source_rule_keys and "source_rule_key" not in payload and same_operator and previous.get("source_rule_key"):
            source_rule_keys = [str(previous["source_rule_key"])]
        source_rule_key = source_rule_keys[0] if source_rule_keys else None
        declared_codes = payload.get("source_rule_codes") if "source_rule_codes" in payload else (
            (previous.get("source_rule_codes") or []) if same_operator else []
        )
        source_rule_codes = list(dict.fromkeys(str(code) for code in declared_codes if str(code)))
        if not source_rule_keys and not source_rule_codes:
            raise BusinessError("RULE_RISK_SOURCE_REQUIRED", "风险规则必须选择来源检查或计算结果", status_code=400, details={"rule": name})
        declared_node = payload.get("source_node") if "source_node" in payload else (
            previous.get("source_node") if same_operator else None
        )
        source_node = str(declared_node or "") or None
        if source_node is not None and source_node not in SOURCE_NODE_RULE_NODES:
            raise BusinessError("RULE_RISK_SOURCE_NODE_INVALID", "风险规则的来源节点类型无效", status_code=400, details={"rule": name, "source_node": source_node})
        draft_nodes = list(
            db.scalars(select(ScenarioDraftNode).where(ScenarioDraftNode.draft_id == draft.id)).all()
        )
        node_positions = {item.node_code: item.sequence_no for item in draft_nodes}
        node_type_by_code = {item.node_code: item.node_type for item in draft_nodes}
        siblings = {
            item.rule_key: item
            for item in db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all()
        }
        for key in source_rule_keys:
            sibling = siblings.get(key)
            if sibling is None:
                raise BusinessError("RULE_RISK_SOURCE_MISSING", "风险规则的来源规则不存在", status_code=400, details={"rule": name, "source_rule_key": key})
            if key == existing_key or sibling.operator_code == "RISK_TRIGGER":
                raise BusinessError("RULE_RISK_SOURCE_INVALID", "风险规则不能引用自身或另一条风险规则", status_code=400, details={"rule": name, "source_rule_key": key})
            if node_positions.get(node, 0) <= node_positions.get(sibling.node_code, 0):
                raise BusinessError("RULE_NODE_ORDER_INVALID", "风险规则必须位于全部来源规则之后", status_code=400, details={"rule": name, "source_rule_key": key})
            if source_node is not None and node_type_by_code.get(sibling.node_code) not in SOURCE_NODE_RULE_NODES[source_node]:
                raise BusinessError("RULE_RISK_SOURCE_NODE_MISMATCH", "来源规则不在指定的来源节点类型中", status_code=400, details={"rule": name, "source_rule_key": key, "source_node": source_node})
    definition = {
        "name": name,
        "node": node,
        "operator": operator,
        "enabled": bool(payload.get("enabled", True)),
        "inputs": inputs,
        "outputs": outputs,
        "materials": scope,
        "parameters": parameters,
        "requirement": str(payload.get("requirement") or ""),
        # 「检查等级」在列协议里是必填业务口径（导入期缺失直接报 RULE_CHECK_LEVEL_REQUIRED）。
        # 页面表单也接受它：否则页面新建的规则没有等级，导出后重新导入必然被拒——
        # 「导出再导入完整保留」在页面路径上就永远不成立。
        "check_level": str((payload["check_level"] if "check_level" in payload else previous.get("check_level") if same_operator else None) or "").strip() or None,
        # 风险的成立条件同样是列协议里的必填枚举（页面表单可提交，见 DraftRuleRequest）。
        "risk_condition": str((payload["risk_condition"] if "risk_condition" in payload else previous.get("risk_condition") if same_operator else None) or "").strip() or None,
        "applicability": {"kind": applicability_kind},
        "source_node": source_node,
        "source_rule_key": source_rule_key,
        "source_rule_keys": source_rule_keys,
        "source_rule_codes": source_rule_codes,
    }
    definition = _preserve_untouched_rule_keys(definition, previous, operator=operator)
    if "source_ref" in payload:
        definition["source_ref"] = _normalized_source_ref(payload["source_ref"])
    elif isinstance(previous, dict) and previous.get("source_ref"):
        definition["source_ref"] = previous["source_ref"]
    source_ref = definition.get("source_ref")
    if existing_key is not None:
        published_codes = {
            str(rule.get("rule_code") or rule.get("source_ref"))
            for manifest in db.scalars(
                select(ScenarioVersion.manifest_json).where(ScenarioVersion.scenario_code == draft.scenario_code)
            ).all()
            if isinstance(manifest, dict)
            for rule in manifest.get("rules") or []
            if isinstance(rule, dict) and rule.get("rule_key") == existing_key
            and (rule.get("rule_code") or rule.get("source_ref"))
        }
        if published_codes and (len(published_codes) != 1 or source_ref not in published_codes):
            raise BusinessError(
                "RULE_CODE_PUBLISHED_IMMUTABLE",
                "规则首次发布后不能修改业务编号",
                status_code=409,
                details={"rule_key": existing_key, "published_codes": sorted(published_codes), "source_ref": source_ref},
            )
    if source_ref:
        duplicates = db.scalars(
            select(ScenarioDraftRule).where(
                ScenarioDraftRule.draft_id == draft.id,
                ScenarioDraftRule.source_ref == source_ref,
            )
        ).all()
        branch_material = manifest_module.internal_rule_branch_material(definition)
        for duplicate in duplicates:
            if duplicate.rule_key == existing_key:
                continue
            other_material = manifest_module.internal_rule_branch_material(duplicate.rule_json or {})
            if (
                node_type == "INTERNAL_VALIDATE"
                and duplicate.node_code == node
                and branch_material is not None
                and other_material is not None
                and branch_material != other_material
            ):
                continue
            raise BusinessError(
                "RULE_CODE_DUPLICATE",
                "规则编号重复；只有同一内部校验规则的不同材料分支可共用编号",
                status_code=409,
                details={"source_ref": source_ref, "rule_key": duplicate.rule_key},
            )
    return definition


def _normalized_source_ref(value: Any) -> str | None:
    """业务编号：留空表示页面新建、没有来源编号，不伪造一个。"""
    text = str(value or "").strip()
    return text or None


# 页面表单不呈现、但属于业务配置事实的键：它们只能由导入、人工裁决或发布编译产生。
# 保存草稿时原样保留，不因为它们不在本次提交里就当作“用户清空了该配置”。
PRESERVED_RULE_KEYS = (
    "outputs",
    "check_operator",
    "risk_level_value",
    "description",
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
    if not isinstance(previous, dict) or previous.get("operator") != operator:
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
    "LLM_EXTRACT": frozenset({"instruction"}),
    "FILE_QUALITY_CHECK": frozenset({"artifact_type"}),
    "RISK_TRIGGER": frozenset({"risk_level", "action"}),
}
# 所有比较类算子的表单都含「人工复核」开关。
FORM_OWNED_COMMON_PARAMETER_KEYS = frozenset({"requires_manual_review"})


def _form_owned_parameter_keys(operator: str) -> frozenset[str]:
    return FORM_OWNED_PARAMETER_KEYS.get(operator, frozenset()) | FORM_OWNED_COMMON_PARAMETER_KEYS


def create_draft_rule(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增规则。已保存到草稿，发布后才供新方案使用。"""
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
    item = db.scalar(
        select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft_id, ScenarioDraftRule.rule_key == rule_key)
    )
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
    dependents = db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all()
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
    dependents = db.scalars(select(ScenarioDraftRule).where(ScenarioDraftRule.draft_id == draft.id)).all()
    downstream = _downstream_rule_names(dependents, rule_key)
    if downstream:
        raise BusinessError(
            "RULE_REFERENCED_BY_RISK",
            "该规则仍是以下风险规则的来源规则，请先处理后再删除",
            status_code=409,
            details={"rule_name": item.name, "rule_keys": downstream},
        )
    db.delete(item)
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
    """在草稿中新增报告章节。已保存到草稿，发布后才供新方案使用。"""
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
    """修改草稿中的报告章节。"""
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
    item = db.scalar(
        select(ScenarioDraftSection).where(ScenarioDraftSection.draft_id == draft.id, ScenarioDraftSection.section_key == section_key)
    )
    if not item:
        raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
    db.delete(item)
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


def _calculation_store(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """草稿里的计算配置：候选列表，或历史版本留下的已编译结果。

    两者不会同时生效。都没有时返回空候选，发布结果里就没有计算段。
    """
    if isinstance(raw, dict) and isinstance(raw.get("frozen"), list):
        frozen = [item for item in raw["frozen"] if isinstance(item, dict)]
        return [], frozen
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)], None
    return [], None


def compile_draft_manifest(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
    *,
    acknowledged_capability_gaps: list[str] | None = None,
) -> dict[str, Any]:
    """把草稿编译成 manifest：发布、草稿导出与草稿视图共用这一处编译。

    `acknowledged_capability_gaps` 是本次发布逐条确认过的能力缺口规则键：它随版本
    冻结并参与 manifest_hash，因此确认集合变化必然产生一个新版本。
    """
    nodes, sheets, materials, fields, rules, sections = _draft_rows(db, draft.id)
    calculation_candidates, frozen_calculations = _calculation_store(draft.calculation_json)
    settings = get_settings()
    # 配置行 → 编译入参的映射口径只有一份：domain.manifest.compile_inputs_of_config。
    # 方案保存走同一份，两条路径的编译产物因此必然同形。
    inputs = manifest_module.compile_inputs_of_config(
        materials=materials,
        fields=fields,
        rules=rules,
        sections=sections,
        nodes=nodes,
        sheets=sheets,
        profile_version=draft.profile_version or unified_profile.UNIFIED_PROFILE_VERSION,
        scenario_schema_config=draft.schema_json,
    )
    return compile_form_manifest(
        scene_code=template.scenario_code,
        scene_name=template.scenario_name,
        namespace=template.scenario_code,
        **inputs,
        compiled_at=datetime.now(UTC),
        calculation_candidates=calculation_candidates,
        frozen_calculations=frozen_calculations,
        disabled_source_rows=list(draft.disabled_source_rows or []),
        acknowledged_capability_gaps=acknowledged_capability_gaps,
    )


def publish_draft(db: Session, scenario_code: str, payload: dict[str, Any]) -> tuple[ScenarioVersion, bool]:
    """把当前完整配置编译成不可变版本；也供能力缺口确认使用。

    发布前的检查全部由 `assess_publish` 承担，与草稿就绪状态是同一套逻辑。
    任一项不通过即原样抛错，且在抛错前不创建 `ScenarioVersion`。

    - expected_revision 必须与草稿当前修订一致，否则明确冲突（刷新后重试）。
    - 内容与最新已发布版本完全一致（同范围指纹、同名称）时返回该版本，不制造重复版本。
    - 版本号由 allocate_version 的行锁与原子计数器分配，同一事务内完成。
    - 已发布版本不可修改：发布只新增版本，不改写任何既有版本。
    """
    template = template_or_404(db, scenario_code, lock=True)
    if template.deleted_at is not None:
        raise BusinessError(
            "SCENARIO_DELETED",
            "场景已删除，不能发布",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    draft = ensure_draft(db, template)
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None and int(expected_revision) != draft.revision_no:
        raise BusinessError(
            "SCENARIO_DRAFT_REVISION_CONFLICT",
            f"配置在本次编辑期间已被修改（当前修订 {draft.revision_no}），请刷新后重试",
            status_code=409,
            details={"scenario_code": scenario_code, "expected": int(expected_revision), "actual": draft.revision_no},
        )
    acknowledged = payload.get("acknowledged_capability_gaps") or []
    manifest = assess_publish(
        db,
        template,
        draft,
        acknowledged_capability_gaps=[str(item) for item in acknowledged],
    )
    change_summary = (str(payload.get("change_summary")).strip() or None) if payload.get("change_summary") else None
    version, reused = _publish_manifest(db, template, draft, manifest, change_summary=change_summary)
    # 发布成功后**不能**无条件清空 draft.sync_error：本次确认过的能力缺口只是这一次
    # 发布接受了它们，缺口本身仍然存在。这里用**空确认集合**重新执行一次就绪检查，
    # 让仍未实现的能力继续显示为「需要本次确认」——否则页面刷新后确认复选框消失，
    # 而再次发布必然失败（没有可确认项，缺口仍然阻断）。
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
                # 确认集合是发布审计事实的一部分：它随版本冻结，也在操作日志里留痕。
                "acknowledged_capability_gaps": list(manifest.get("acknowledged_capability_gaps") or []),
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
    _save_draft(
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
    """场景版本分页列表（按场景、来源与发布时间筛选）。"""
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
                "rule_count": len([rule for rule in manifest.get("rules", []) if not manifest_module.is_system_rule(rule)]),
                "system_rule_count": len([rule for rule in manifest.get("rules", []) if manifest_module.is_system_rule(rule)]),
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
    entries.extend(diff("MATERIAL", "material_key", from_manifest.get("materials", []), to_manifest.get("materials", []), ["name", "required"]))
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
