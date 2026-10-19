"""场景管理列表、选项与详情查询。"""

from datetime import datetime
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ...common.pagination import PageResult, page_result
from ...domain import manifest as manifest_module
from ...entity import AuditPlan, ScenarioDraft, ScenarioTemplate, ScenarioVersion
from .common import DRAFT_SOURCE_LABELS
from .lookup import template_or_404


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
) -> PageResult[dict[str, Any]]:
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
    latest_versions: dict[str, tuple[int, datetime | None, int]] = {}
    published_manifests: dict[int, dict[str, Any]] = {}
    draft_by_code: dict[str, ScenarioDraft] = {}
    draft_counts: dict[str, dict[str, int]] = {}
    if codes:
        for scenario_code, version_no, published_at, version_id in db.execute(
            select(ScenarioVersion.scenario_code, ScenarioVersion.version_no, ScenarioVersion.published_at, ScenarioVersion.id)
            .where(ScenarioVersion.scenario_code.in_(codes), ScenarioVersion.status == "PUBLISHED")
        ).all():
            latest = latest_versions.get(scenario_code)
            if latest is None or version_no > latest[0]:
                latest_versions[scenario_code] = (version_no, published_at, version_id)
        published_manifests = dict(db.execute(
            select(ScenarioVersion.id, ScenarioVersion.manifest_json)
            .where(ScenarioVersion.id.in_([version[2] for version in latest_versions.values()]))
        ).all())
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
        if draft is None and latest is not None:
            manifest = published_manifests[latest[2]]
            counts = {"material": len(manifest["materials"]), "field": len(manifest["fields"]),
                      "rule": sum(not manifest_module.is_system_rule(rule) and not rule.get("calculation_ref")
                                  for rule in manifest["rules"])}
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
                "configuration_kind": "DRAFT" if draft is not None or latest is None else "PUBLISHED",
                "draft_revision": draft.revision_no if draft else None,
                "draft_source": draft.source if draft else None,
                "draft_base_version_no": draft.base_version_no if draft else None,
                # 草稿存在与否单独返回；无编译错误不表示草稿已发布。
                "sync_error": None,
                "updated_at": template.updated_at,
            }
        )
    return page_result(items, total, page, page_size)


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
