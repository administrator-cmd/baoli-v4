"""场景草稿编译、发布检查与版本保存。"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain import manifest as manifest_module
from ...domain import node_profile
from ...domain.calculation_config import calculation_store
from ...domain.manifest import compile_form_manifest, manifest_scope_hash
from ...entity.imports import ImportJob
from ...entity.scenario_drafts import ScenarioDraft
from ...entity.scenarios import ScenarioNode, ScenarioTemplate, ScenarioVersion
from ..system.operation_log import record_operation
from ..system.operation_types import OBJECT_SCENARIO
from .draft import draft_rows
from .lookup import find_latest_published_version, template_or_404, version_or_404
from .versions import allocate_version

logger = logging.getLogger(__name__)


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


def compile_draft_manifest(
    db: Session,
    template: ScenarioTemplate,
    draft: ScenarioDraft,
) -> dict[str, Any]:
    """把草稿编译成规则库快照，供发布、草稿导出与草稿视图使用。"""
    nodes, materials, fields, rules, sections = draft_rows(db, draft.id)
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
