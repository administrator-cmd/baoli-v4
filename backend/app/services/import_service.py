import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.manifest import (
    NODE_DEFINITIONS,
    RESOLVABLE_PROPERTIES,
    draft_config_of_import,
    excluded_targets,
    import_origin_of,
    resolved_targets,
    resolve_saved_decisions,
    rule_comparison_view,
)
from ..domain.proposals import ProposalBundle
from ..domain.workbook import WorkbookIR
from ..common.exceptions import BusinessError
from ..graphs.import_graph import (
    import_decision_signature,
    node_note_target_ref,
    resume_import_decision,
    run_sheet_conversion,
    run_workbook_read,
)
from ..entities import FieldCandidate, ImportIssue, ImportJob, ImportSheet
from ..entities import (
    BackgroundOperation,
    ScenarioDraft,
    ScenarioDraftNode,
    ScenarioTemplate,
    ScenarioVersion,
)
from ..dto import ImportDecisionsRequest, PublishRequest, SheetBindingsRequest
from .background_service import ACTIVE_STATUSES, assert_no_active_operation, latest_operation, operation_view
from ..domain.identity import public_no
from .operation_types import OBJECT_IMPORT_JOB
from .operation_log_service import record_operation
from ..domain import scenario_schema, unified_profile
from .storage_service import RULEBOOK_EXTENSIONS, save_upload


NODE_LABELS = dict(NODE_DEFINITIONS)

# 模板说明页（元数据 Sheet）在 import_sheet 里的**唯一**状态值。
# 保存绑定与转换必须统一使用该值，否则元数据 Sheet 会被误当成待绑定业务表。
METADATA_SHEET_STATUS = "EXCLUDED"


def _bundle_coverage(bundle: ProposalBundle) -> dict[str, Any] | None:
    """转换结果的来源覆盖记录。

    coverage 由转换侧提供，旧版本没有该字段时保持 None；不在这里编造覆盖统计。
    """
    coverage = getattr(bundle, "coverage", None)
    if coverage is None:
        return None
    if isinstance(coverage, dict):
        return coverage
    if hasattr(coverage, "model_dump"):
        return coverage.model_dump(mode="json")
    return None


def _job_or_404(db: Session, job_no: str, *, lock: bool = False) -> ImportJob:
    statement = select(ImportJob).where(ImportJob.job_no == job_no)
    if lock:
        statement = statement.with_for_update()
    job = db.scalar(statement)
    if not job:
        raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": job_no})
    return job


def _job_schema(job: ImportJob) -> dict[str, Any]:
    if not isinstance(job.schema_json, dict):
        raise BusinessError(
            "IMPORT_SCENARIO_SCHEMA_MISSING",
            "导入作业缺少创建时的场景 schema 快照，不能继续解释工作簿",
            status_code=409,
            details={"job_no": job.job_no},
        )
    schema = scenario_schema.normalize_schema(job.schema_json)
    if job.schema_key != schema["schema_key"] or job.schema_version != schema["schema_version"]:
        raise BusinessError(
            "IMPORT_SCENARIO_SCHEMA_MISMATCH",
            "导入作业的场景 schema 身份与快照不一致",
            status_code=409,
            details={"job_no": job.job_no},
        )
    return schema


def _job_profile(job: ImportJob) -> dict[str, Any]:
    return scenario_schema.effective_profile(_job_schema(job))


def _ensure_import_mutable(db: Session, job: ImportJob) -> None:
    published_version_id = db.scalar(
        select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == job.id).limit(1)
    )
    if job.status == "APPLIED":
        raise BusinessError(
            "IMPORT_ALREADY_APPLIED",
            "该导入作业已写入场景草稿，不能再修改。请到场景草稿里核对配置，或重新上传一份规则库",
            status_code=409,
            details={"job_no": job.job_no, "scenario_code": job.scenario_code},
        )
    if job.status == "PUBLISHED" or published_version_id is not None:
        raise BusinessError(
            "IMPORT_ALREADY_PUBLISHED",
            "该导入作业已产生场景版本，不能再修改或重新编译",
            status_code=409,
        )


def _record_import_checkpoint_failure(db: Session, job: ImportJob, error: BusinessError) -> None:
    """等待点不可恢复时落终态并记录原因，不留一个假的「等待中」状态。"""
    decision_payload = dict(job.decisions_json or {"items": []})
    workflow = decision_payload.get("workflow")
    if isinstance(workflow, dict):
        workflow = dict(workflow)
        workflow["status"] = "MISSING"
        decision_payload["workflow"] = workflow
    job.decisions_json = decision_payload
    job.status = "CHECKPOINT_MISSING"
    job.failure_code = error.code
    job.failure_message = error.message
    db.commit()


def _is_template_metadata_sheet(sheet: ImportSheet) -> bool:
    """模板元数据 Sheet（说明页）。

    判据只有一条：**名称命中统一协议声明的说明页**（`instruction_sheet`）。
    模板生成、导出与导入读的是同一个声明，因此这里是确定性判断。

    不再使用「隐藏且没有业务表头」作判据：隐藏只是 Excel 的展示属性，
    任何一张隐藏的业务 Sheet 都会被它误判成元数据，随后因为
    「保存绑定写 HIDDEN、转换要求 EXCLUDED」而必然转换失败。
    """
    name = unified_profile.instruction_sheet_name(unified_profile.load_unified_profile())
    return sheet.sheet_name == name


async def create_import_job(db: Session, upload: UploadFile, *, scenario_code: str) -> ImportJob:
    """在**指定场景**下上传规则库、读取工作簿并建立导入作业。

    `scenario_code` 是必填的：上传流程以场景为起点（用户先建场景、再在该场景下上传），
    场景不由 Excel 表头推断。猜错场景会拿另一套列协议去解释这份文件，
    报出一堆与真实问题无关的缺列错误，而用户手里的文件根本没变。

    场景必须真实存在且未删除：写一个不存在的场景编号会让作业挂在一个查不到的场景上。
    """
    settings = get_settings()
    # 场景必须真实存在且未删除：写一个不存在的场景编号会让作业挂在一个查不到的场景上。
    template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))
    if template is None or template.deleted_at is not None:
        raise BusinessError(
            "IMPORT_SCENARIO_NOT_FOUND",
            "导入作业必须属于一个已创建的场景，请先在场景管理里建立场景再上传",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    if not template.enabled:
        raise BusinessError(
            "IMPORT_SCENARIO_DISABLED",
            f"场景“{template.scenario_name}”已停用，不能在其下新建导入作业",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    schema = scenario_schema.normalize_schema(template.schema_json)
    if template.schema_key != schema["schema_key"] or template.schema_version != schema["schema_version"]:
        raise BusinessError(
            "SCENARIO_SCHEMA_MISMATCH",
            "场景 schema 身份与保存的配置不一致，不能导入",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    job_no = public_no("IMP")
    suffix = Path(upload.filename or "").suffix.lower()
    relative_path = f"imports/{job_no}/source{suffix}"
    size, sha256 = await save_upload(
        upload,
        relative_path=relative_path,
        allowed_extensions=RULEBOOK_EXTENSIONS,
        max_bytes=settings.max_rulebook_bytes,
    )
    try:
        graph_state = run_workbook_read(
            job_no=job_no,
            source_path=relative_path,
            workbook_id=public_no("wb"),
            source_filename=upload.filename or f"规则库{suffix}",
            source_size=size,
            source_sha256=sha256,
            scenario_code=scenario_code,
            schema_json=schema,
        )
        workbook_ir = WorkbookIR.model_validate(graph_state["workbook_ir"])
        job = ImportJob(
            job_no=job_no,
            source_filename=upload.filename or f"规则库{suffix}",
            source_path=relative_path,
            source_sha256=sha256,
            source_size=size,
            # 统一模板协议：系统只有一份协议，这里记录按哪一版协议解释这份文件。
            profile_version=workbook_ir.profile_version,
            # 上传入口的场景身份：作业明确属于哪个场景，不从 Excel 内容猜。
            scenario_code=scenario_code,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
            schema_json=schema,
            profile_display_version=unified_profile.UNIFIED_PROFILE_VERSION,
            status="PARSED",
            workbook_ir_json=workbook_ir.model_dump(mode="json"),
            decisions_json={
                "items": [],
                "workflow": {
                    "checkpoint_ns": "import:read",
                    "status": "COMPLETED",
                    "stage_history": graph_state.get("stage_history", []),
                },
            },
        )
        db.add(job)
        db.flush()
        # 上传时**只记录读取到的事实**，不预设绑定：绑定关系（Sheet → 节点实例）
        # 必须由用户在绑定步骤确认。系统给出的 suggested_role 只作页面预选，
        # 不写进 bound_role、也不据此生成节点——未确认的绑定等于替用户做了决定。
        for sheet in workbook_ir.sheets:
            # 只有表头、没有业务数据行的 Sheet 仍然保留：表头是确定的列协议，
            # 当前业务行没有数据不等于这张表不该被读取。
            # 真正的 EMPTY（连表头都没有）无法确定列，按无业务数据处理。
            if sheet.state in {"EMPTY", "NO_BUSINESS_DATA"}:
                status = sheet.state
            else:
                status = "PARSED"
            # 名称命中协议声明的说明页时，它就是模板元数据：固定排除，状态用
            # METADATA_SHEET_STATUS（转换期读同一个值）。**隐藏与否不参与判断**：
            # 隐藏只是 Excel 展示属性，隐藏的业务 Sheet 仍由用户绑定与导入。
            if sheet.name == unified_profile.instruction_sheet_name(scenario_schema.effective_profile(schema)):
                status = METADATA_SHEET_STATUS
            db.add(
                ImportSheet(
                    import_job_id=job.id,
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    visible=sheet.visible,
                    status=status,
                    bound_role=None,
                    bound_node_code=None,
                    # 表头行要等用户确认。候选留在 snapshot_json.header_candidates，不把第一行写成已确认表头。
                    header_row=None,
                    snapshot_json=sheet.model_dump(mode="json"),
                )
            )
        db.commit()
        db.refresh(job)
        return job
    except Exception:
        db.rollback()
        raise


def get_import_job(db: Session, job_no: str) -> ImportJob:
    """按作业编号取导入作业；取不到即 404。"""
    return _job_or_404(db, job_no)


def _proposal_business_view(
    job: ImportJob,
    sheet_names: dict[str, str],
    open_targets: set[tuple[str, str]],
) -> dict[str, Any] | None:
    """业务候选视图。

    取值来源是「原始候选 + 已保存裁决」的叠加结果：用户改过的字段类型、材料引用和
    说明在刷新后仍然有效，而原始候选、来源 Sheet/行/单元格始终保持原样不被覆盖。
    每个候选带一个业务状态：自动识别 / 待补充 / 已人工处理 / 已排除。
    """
    proposals = job.proposals_json
    if not isinstance(proposals, dict):
        return None
    decisions = [item for item in (job.decisions_json or {}).get("items", []) if isinstance(item, dict)]
    effective = resolve_saved_decisions(proposals, decisions)
    excluded_sheets, excluded_refs = excluded_targets(decisions)
    resolved_refs = resolved_targets(decisions)

    def decision_state(target_type: str, candidate_ref: str, sheet_key: str) -> str:
        if sheet_key in excluded_sheets or (target_type, candidate_ref) in excluded_refs:
            return "EXCLUDED"
        if (target_type, candidate_ref) in open_targets:
            return "PENDING"
        if (target_type, candidate_ref) in resolved_refs:
            return "MANUAL"
        return "AUTO"

    material_names = {
        material["candidate_ref"]: material["name"]
        for sheet in effective.get("sheets", [])
        for material in sheet.get("materials", [])
    }
    field_labels = {
        field["candidate_ref"]: (
            f"{material_names[field['material_ref']]} · {field['name']}"
            if field.get("material_ref") in material_names
            else field["name"]
        )
        for sheet in effective.get("sheets", [])
        for field in sheet.get("fields", [])
    }
    rule_names: dict[str, str] = {}
    for sheet in effective.get("sheets", []):
        for rule in sheet.get("rules", []):
            material_scope = [material_names.get(ref) or ref for ref in rule.get("material_refs", [])]
            rule_names[rule["candidate_ref"]] = (
                f"{rule['name']}（{'、'.join(material_scope)}）" if material_scope else rule["name"]
            )
    # 有效字段的值类型、单位与币种：规则比较视图据此展示双方口径，不做任何换算猜测。
    field_meta = {
        field["candidate_ref"]: {
            "value_type": field["value_type"],
            "unit": field.get("unit"),
            "currency": field.get("currency"),
        }
        for sheet in effective.get("sheets", [])
        for field in sheet.get("fields", [])
    }
    sheets: list[dict[str, Any]] = []
    # 原始候选用于保留来源事实；两者按 candidate_ref 一一对应。
    origins = {
        sheet["sheet_key"]: sheet for sheet in proposals.get("sheets", [])
    }
    for sheet in effective.get("sheets", []):
        sheet_key = sheet.get("sheet_key")
        sheet_name = sheet_names.get(sheet_key)
        if not sheet_name:
            raise BusinessError("IMPORT_PROPOSAL_SHEET_MISSING", "规则转换结果引用了不存在的工作表", status_code=500)
        origin = origins.get(sheet_key, {})
        origin_materials = {item["candidate_ref"]: item for item in origin.get("materials", [])}
        origin_fields = {item["candidate_ref"]: item for item in origin.get("fields", [])}
        origin_rules = {item["candidate_ref"]: item for item in origin.get("rules", [])}
        rule_views: list[dict[str, Any]] = []
        for item in sheet.get("rules", []):
            comparison = rule_comparison_view(item, material_names, field_labels, rule_names, field_meta)
            rule_views.append(
                {
                    "candidate_ref": item["candidate_ref"],
                    "name": item["name"],
                    "node": item["node"],
                    "node_name": NODE_LABELS.get(item["node"], item["node"]),
                    "operator": item["operator"],
                    "decision_state": decision_state("RULE", item["candidate_ref"], sheet_key),
                    "original": _origin_view(origin_rules.get(item["candidate_ref"]), ("name", "node", "operator")),
                    "comparison": comparison,
                    # 方向、阈值与支持状态必须随候选直接可见：不支持或单位不一致的规则
                    # 不能在页面上被当成可直接发布的普通规则。
                    "support_status": comparison["support_status"],
                    "support_message": comparison["support_message"],
                    "source_sheet": sheet_name,
                    "source_row": item["source"]["row_no"],
                    "source_cells": item["source"]["cells"],
                }
            )
        sheets.append(
            {
                "sheet_key": sheet_key,
                "sheet_name": sheet_name,
                "excluded": sheet_key in excluded_sheets,
                "suggested_role": sheet.get("suggested_role"),
                "materials": [
                    {
                        "candidate_ref": item["candidate_ref"],
                        "name": item["name"],
                        "required": item["required"],
                        # 材料业务说明随候选一起展示和发布，不是无用备注。
                        "description": item.get("description", ""),
                        "decision_state": decision_state("MATERIAL", item["candidate_ref"], sheet_key),
                        "original": _origin_view(
                            origin_materials.get(item["candidate_ref"]),
                            ("name", "required", "description"),
                        ),
                        "source_sheet": sheet_name,
                        "source_row": item["source"]["row_no"],
                        "source_cells": item["source"]["cells"],
                    }
                    for item in sheet.get("materials", [])
                ],
                "fields": [
                    {
                        "candidate_ref": item["candidate_ref"],
                        "name": item["name"],
                        "value_type": item["value_type"],
                        "unit": item.get("unit"),
                        "currency": item.get("currency"),
                        "required": item["required"],
                        "instruction": item["instruction"],
                        "material_ref": item["material_ref"],
                        "material_name": material_names.get(item["material_ref"]),
                        "decision_state": decision_state("FIELD", item["candidate_ref"], sheet_key),
                        "original": _origin_view(
                            origin_fields.get(item["candidate_ref"]),
                            ("name", "value_type", "unit", "currency", "required", "instruction"),
                        ),
                        "source_sheet": sheet_name,
                        "source_row": item["source"]["row_no"],
                        "source_cells": item["source"]["cells"],
                    }
                    for item in sheet.get("fields", [])
                ],
                "rules": rule_views,
                "report_sections": [
                    {"section_name": item["section_name"]} for item in sheet.get("report_sections", [])
                ],
                "question_count": len(sheet.get("questions", [])),
            }
        )
    original_calculations = {
        item["candidate_ref"]: item for item in proposals.get("calculations", [])
    }

    def nested_calc_sources(
        rows: list[dict[str, Any]], originals: list[dict[str, Any]], key: str
    ) -> list[dict[str, Any]]:
        original_by_code = {row[key]: row for row in originals}
        result: list[dict[str, Any]] = []
        for row in rows:
            code = row.get(key)
            original_row = original_by_code.get(code) if isinstance(code, str) else None
            result.append({
                "code": code if isinstance(code, str) else None,
                "origin": import_origin_of(
                    original_row, sheet_names=sheet_names, import_job_no=job.job_no
                ) if original_row and original_row.get("source") else None,
            })
        return result

    calculations: list[dict[str, Any]] = []
    for item in effective.get("calculations", []):
        candidate_ref = item["candidate_ref"]
        original = original_calculations.get(candidate_ref)
        if original is None:
            raise BusinessError(
                "IMPORT_CALC_ORIGIN_MISSING",
                "计算候选找不到原始定义，无法核对跨表来源",
                status_code=500,
                details={"candidate_ref": candidate_ref},
            )
        rule_code = item["rule_code"]
        source = item["source"]
        sheet_key = source["sheet_key"]
        sheet_name = sheet_names.get(sheet_key)
        if not sheet_name:
            raise BusinessError("IMPORT_PROPOSAL_SHEET_MISSING", "计算候选引用了不存在的工作表", status_code=500)
        identities = {("CALC_RULE", candidate_ref), ("CALC_RULE", rule_code)}
        if sheet_key in excluded_sheets or identities & excluded_refs:
            state = "EXCLUDED"
        elif identities & open_targets:
            state = "PENDING"
        elif identities & resolved_refs:
            state = "MANUAL"
        else:
            state = "AUTO"
        calculations.append(
            {
                "candidate_ref": candidate_ref,
                "rule_code": rule_code,
                "rule_name": item["rule_name"],
                "formula": item["formula"],
                "result_code": item["result_code"],
                "result_type": item["result_type"],
                "node_code": item["node_code"],
                "status": item["status"],
                "parameters": item["parameters"],
                "bands": item["bands"],
                "bands_text": item.get("bands_text") or "",
                "unresolved_identifiers": item["unresolved_identifiers"],
                "decision_state": state,
                "source_sheet": sheet_name,
                "source_row": source["row_no"],
                "source_cells": source["cells"],
                "origin": import_origin_of(original, sheet_names=sheet_names, import_job_no=job.job_no),
                "parameter_sources": nested_calc_sources(
                    item["parameters"], original.get("parameters") or [], "parameter_code"
                ),
                "band_sources": nested_calc_sources(
                    item["bands"], original.get("bands") or [], "condition_code"
                ),
            }
        )
    return {"sheets": sheets, "calculations": calculations}


def _origin_view(origin: dict[str, Any] | None, properties: tuple[str, ...]) -> dict[str, Any] | None:
    """保留原始候选值，供页面在人工改过之后仍能对照原始识别结果。"""
    if not isinstance(origin, dict):
        return None
    return {name: origin.get(name) for name in properties}


def _issue_business_source(issue: ImportIssue, sheet_names: dict[str, str]) -> dict[str, Any]:
    """把问题来源投影为工作表名称、行号和单元格，避免暴露候选引用。"""
    payload = issue.source_json if isinstance(issue.source_json, dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    sheet_key = source.get("sheet_key")
    return {
        "target_type": payload.get("target_type"),
        "sheet_name": sheet_names.get(sheet_key, sheet_key),
        "row_no": source.get("row_no"),
        "cells": source.get("cells", []),
    }


def _manifest_business_view(job: ImportJob) -> dict[str, Any] | None:
    """场景草稿只向前端暴露名称标识；字段键和规则键属于内部技术字段。"""
    manifest = job.manifest_draft_json
    if not isinstance(manifest, dict):
        return None
    return {
        "scene_code": manifest.get("scene_code"),
        "scene_name": manifest.get("scene_name"),
    }


def _unprocessed_note(snapshot: dict[str, Any]) -> str | None:
    """超出读取范围时明确说明未处理范围，不能宣称整份 Excel 已完整转换。"""
    settings = get_settings()
    notes: list[str] = []
    if snapshot.get("rows_truncated"):
        notes.append(f"本表共 {snapshot['max_row']} 行，只读取了前 {settings.workbook_preview_rows} 行")
    if snapshot.get("columns_truncated"):
        notes.append(f"本表共 {snapshot['max_column']} 列，只读取了前 {settings.workbook_preview_columns} 列")
    if snapshot.get("cells_truncated"):
        notes.append(f"部分单元格内容超过 {settings.workbook_cell_chars} 字，超出部分没有被读取")
    return "；".join(notes) if notes else None


def _job_profile_summary(db: Session, job: ImportJob) -> dict[str, Any]:
    """保持原有 profile 摘要形状，同时按作业的场景 schema 展示 Sheet 协议。"""
    profile = _job_profile(job)
    summary = unified_profile.profile_summary(profile)
    # 角色目录是完整协议，不是场景草稿里已经确认的 Sheet。
    # 新场景还没有 scenario_draft_sheet 时也必须能选择角色。
    scenario_name: str | None = None
    if job.scenario_code:
        template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == job.scenario_code))
        scenario_name = template.scenario_name if template is not None else None
    return {
        **summary,
        "scenario_code": job.scenario_code,
        "scenario_name": scenario_name,
        "scenario_schema_key": job.schema_key,
        "scenario_schema_version": job.schema_version,
    }


def _ordered_sheets_of(db: Session, job_id: int) -> list[ImportSheet]:
    """按上传顺序（也就是 Excel 里的 Sheet 顺序）读取本作业的工作表。

    **先只排 id，再按 id 取行**，不直接对带 `snapshot_json` 的行做排序：
    `import_sheet` 每行带着这张表的整份读取快照（实测单表 261 KB），而 MySQL 的
    filesort 要求**整行**放进 `sort_buffer_size`（本机 256 KB）。行一旦大于缓冲，
    查询直接报 1038 Out of sort memory，整个导入详情页 500。
    只排 id 时参与排序的是 8 字节的整数，与行大小无关，因此不受缓冲限制。

    这不是「调大缓冲」能根治的：快照大小由上传文件决定，没有上界；
    换一个更大的缓冲只是把同一个错误推到更大的文件上。
    """
    ordered_ids = db.scalars(
        select(ImportSheet.id).where(ImportSheet.import_job_id == job_id).order_by(ImportSheet.id)
    ).all()
    if not ordered_ids:
        return []
    rows = {item.id: item for item in db.scalars(select(ImportSheet).where(ImportSheet.id.in_(ordered_ids))).all()}
    return [rows[item_id] for item_id in ordered_ids]


def import_job_view(db: Session, job: ImportJob) -> dict[str, Any]:
    """导入作业的业务视图：工作表、问题、候选、草稿修订号与最新后台操作。"""
    sheets = _ordered_sheets_of(db, job.id)
    issues = db.scalars(select(ImportIssue).where(ImportIssue.import_job_id == job.id).order_by(ImportIssue.id)).all()
    sheet_names = {item.sheet_key: item.sheet_name for item in sheets}
    open_targets = {
        (item.source_json.get("target_type"), item.source_json.get("target_ref"))
        for item in issues
        if item.status == "OPEN" and isinstance(item.source_json, dict)
    }
    # 暂存作答只用于回填未决问题；已裁决的问题以 decision_json 为准。
    draft_answers = (job.decisions_json or {}).get("draft_answers")
    draft_answers = draft_answers if isinstance(draft_answers, dict) else {}
    manifest_draft = job.manifest_draft_json if isinstance(job.manifest_draft_json, dict) else {}
    profile = _job_profile(job)
    profile_roles = set(unified_profile.sheet_index(profile))
    return {
        "job_no": job.job_no,
        "source_filename": job.source_filename,
        "status": job.status,
        "profile_version": job.profile_version,
        "schema_key": job.schema_key,
        "schema_version": job.schema_version,
        "schema_json": _job_schema(job),
        # 作业所属场景与统一模板协议摘要；具体场景只使用系统能力全集中的一部分 Sheet。
        "profile": _job_profile_summary(db, job),
        "draft_revision": job.draft_revision,
        "sheets": [
            {
                "sheet_key": item.sheet_key,
                "name": item.sheet_name,
                "visible": item.visible,
                "status": item.status,
                # 固定排除只用于模板元数据（填写说明、隐藏元数据页）。
                # 「只有表头没有业务行」的业务表仍然参与转换（产生零行业务集合），
                # 不是固定排除；把它标成固定排除会让页面显示成“已跳过”，与转换实际行为不符。
                "fixed_excluded": _is_template_metadata_sheet(item),
                # 已确认的绑定：Sheet 角色 + 所属**节点实例**。两者都由用户确认后落库，
                # 刷新页面时直接读它们，不重新按表头推导。
                #
                # 历史作业可能保存协议旧角色名。
                # 旧 role 会同时命中多张表，单独一个旧名不足以确定角色，因此这里
                # **如实标出它无法解释**，由页面提示用户重新确认绑定；
                # 不静默按第一个同名角色解释。
                "role": item.bound_role,
                "role_legacy_unmapped": (
                    item.bound_role is not None
                    and item.bound_role not in profile_roles
                    and unified_profile.role_of_legacy_role(profile, item.bound_role) is None
                ),
                "node_code": item.bound_node_code,
                # 建议值：只用于页面预选。它**不是**绑定结果，页面必须让用户确认后保存。
                "suggested_role": item.snapshot_json.get("suggested_role"),
                "suggested_role_label": unified_profile.sheet_role_label(
                    profile, item.snapshot_json.get("suggested_role")
                ),
                "role_label": unified_profile.sheet_role_label(profile, item.bound_role),
                "header_row": item.header_row,
                "header_candidates": list(item.snapshot_json.get("header_candidates") or []),
                "summary": {
                    "max_row": item.snapshot_json["max_row"],
                    "max_column": item.snapshot_json["max_column"],
                    "non_empty_rows": item.snapshot_json["non_empty_rows"],
                    "used_range": item.snapshot_json.get("used_range"),
                    "merged_ranges": item.snapshot_json["merged_ranges"],
                    "truncated": any(
                        item.snapshot_json[key]
                        for key in ("rows_truncated", "columns_truncated", "cells_truncated")
                    ),
                    "unprocessed_note": _unprocessed_note(item.snapshot_json),
                },
                # WorkbookIR 内部保留完整来源事实；工作台只拿到有上限的预览单元格，
                # 不返回原始的 cell_details 载荷。
                "rows": [
                    {"row_no": row["row_no"], "cells": row["cells"]}
                    for row in item.snapshot_json["preview_rows"]
                ],
            }
            for item in sheets
        ],
        "issues": [
            {
                "issue_no": item.issue_no,
                "category": item.category,
                "severity": item.severity,
                "status": item.status,
                "title": item.title,
                "description": item.description,
                "source": _issue_business_source(item, sheet_names),
                # 作答形式：选择题用 options；数值/文本输入题没有候选选项，页面需要渲染输入控件。
                "input_kind": (item.source_json or {}).get("input_kind") or "SELECT",
                "options": item.options_json,
                # 未决问题优先回填暂存作答，其次才是上一次的裁决记录。
                "decision": draft_answers.get(item.issue_no)
                if item.status == "OPEN" and item.issue_no in draft_answers
                else item.decision_json,
            }
            for item in issues
        ],
        "proposals": _proposal_business_view(job, sheet_names, open_targets),
        "manifest_draft": _manifest_business_view(job),
        # 每条来源业务记录的处置（候选 / 明确排除 / 待处理问题）。
        "coverage": job.coverage_json,
        # 已配置但当前无法无条件执行的检查：编译前为空列表，不从别处编造。
        "capability_gaps": manifest_draft.get("capability_gaps", []),
        "failure": {"code": job.failure_code, "message": job.failure_message} if job.failure_code else None,
        "latest_operation": operation_view(latest) if (latest := latest_operation(db, OBJECT_IMPORT_JOB, job.job_no)) else None,
        "created_at": job.created_at,
    }


def abort_import_job(db: Session, job_no: str) -> BackgroundOperation:
    """中止该导入作业尚未结束的后台操作。

    只把命令本身标成终态 INTERRUPTED 并释放活跃占用，让作业回到可重试 / 可删除的状态。
    这是「释放占用」而不是「回滚」：作业状态与草稿数据一概不改，重试时由 retry_conversion
    按自己的草稿修订整体重建。没有活跃操作时明确报错，不静默成功。

    执行线程不被强杀，所以语义要说清：
    - 线程还没认领命令（PENDING，或刚受理就被中止）：它启动时看到的已不是活跃状态，直接退出，转换不会发生；
    - 线程已经在跑：它可能正处在一次 MySQL 写入中，强行打断比让它自己跑完更危险。它会跑完并
      按真实结果覆盖状态——中止不是回滚，服务端不会为了配合「已中止」而使假状态。
      两个线程写同一份草稿由作业行锁串行化（retry_conversion 全程持有），不会并发写。

    这里刻意**不取作业行锁**：retry_conversion 会在整个转换期间持有它（含 LLM 调用），
    取锁会让中止请求一等等到转换自己结束，那正好是用户点「中止」要避免的情况。
    本函数只改 background_operation，不碰 import_job，因此无需该锁。
    """
    _job_or_404(db, job_no)
    operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_type == OBJECT_IMPORT_JOB,
            BackgroundOperation.object_no == job_no,
            BackgroundOperation.status.in_(ACTIVE_STATUSES),
        )
        .order_by(BackgroundOperation.id.desc())
        .with_for_update()
    )
    if operation is None:
        raise BusinessError(
            "OPERATION_NOT_ACTIVE",
            "该导入作业没有正在进行的操作，无需中止",
            status_code=409,
            details={"job_no": job_no},
        )
    job = db.scalar(select(ImportJob).where(ImportJob.job_no == job_no))
    previous_status = operation.status
    operation.status = "INTERRUPTED"
    operation.failure_code = "OPERATION_ABORTED"
    operation.failure_message = "该操作已被人工中止，可重新发起转换"
    operation.finished_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_IMPORT_JOB,
        object_no=job_no,
        object_name=job.source_filename if job else job_no,
        operation_type="ABORT",
        detail={"job_no": job_no, "operation_no": operation.operation_no, "previous_status": previous_status},
    )
    db.commit()
    db.refresh(operation)
    return operation


def import_job_summaries(db: Session) -> list[dict[str, Any]]:
    # 只取摘要要用的列：整行 SELECT * 时，import_job 的大 JSON 列会撑爆 ORDER BY 的
    # 排序缓冲（默认 256KB），直接报 1038 Out of sort memory。
    """导入作业摘要列表（只取摘要列，避免大 JSON 列撑爆排序缓冲）。"""
    rows = db.execute(
        select(ImportJob.job_no, ImportJob.source_filename, ImportJob.status, ImportJob.created_at)
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
    ).all()
    return [
        {
            "job_no": row.job_no,
            "source_filename": row.source_filename,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _draft_nodes_of_scenario(db: Session, scenario_code: str | None) -> dict[str, Any]:
    """作业所属场景草稿里用户配置的节点实例（node_code → 节点行）。

    绑定必须指向**用户真实建立的节点实例**：允许绑定到一个不存在的 node_code，
    等于让这条绑定在发布时无处落地，而页面会显示成绑定成功。
    作业没有记录场景时返回空字典，由调用方按「无可绑定节点」明确报错。
    """
    if not scenario_code:
        return {}
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    if draft is None:
        return {}
    return {
        item.node_code: item
        for item in db.scalars(select(ScenarioDraftNode).where(ScenarioDraftNode.draft_id == draft.id)).all()
    }


def update_sheet_bindings(db: Session, job_no: str, request: SheetBindingsRequest) -> ImportJob:
    """保存 Sheet → 节点实例的绑定（含 Sheet 角色与表头行）。

    绑定是**本次场景配置的明确输入**：只有已绑定的 Sheet 才按对应节点协议进行校验、
    导入、发布并进入任务工作流；未绑定或被排除的 Sheet 不自动推断用途，也不进入执行范围。

    一张 Sheet 只能绑定一个节点（本函数的入参就是单值），一个节点可以绑定多张 Sheet
    （财报的计算定义/参数/判定三表都绑到同一个 CALCULATION 实例上）。
    绑定必须指向用户真实建立的节点实例，且该节点类型必须在这张 Sheet 的
    `node_types` 声明范围内——绑定到不相干的节点类型会让这张表按错误的协议被解释。
    """
    job = _job_or_404(db, job_no, lock=True)
    # 改绑定与后台转换互斥：转换进行中不能改动工作表用途（锁后检查，避免竞态）。
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    sheets = {item.sheet_key: item for item in db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id))}
    profile = _job_profile(job)
    supported_roles = set(unified_profile.sheet_index(profile))
    nodes = _draft_nodes_of_scenario(db, job.scenario_code)
    request_keys = [item.sheet_key for item in request.bindings]
    if len(request_keys) != len(set(request_keys)):
        raise BusinessError("SHEET_BINDING_DUPLICATE", "工作表绑定包含重复项")
    missing_sheet_keys = set(sheets) - set(request_keys)
    if missing_sheet_keys:
        raise BusinessError(
            "SHEET_BINDING_INCOMPLETE",
            "请为当前导入作业的全部工作表提交绑定结果",
            details={"missing_sheet_count": len(missing_sheet_keys)},
        )
    for binding in request.bindings:
        sheet = sheets.get(binding.sheet_key)
        if not sheet:
            raise BusinessError("SHEET_NOT_FOUND", "绑定引用了不存在的工作表", details={"sheet_key": binding.sheet_key})
        if _is_template_metadata_sheet(sheet):
            if not binding.excluded or binding.role is not None or binding.header_row is not None or binding.node_code is not None:
                raise BusinessError(
                    "TEMPLATE_METADATA_IMMUTABLE",
                    "模板说明页不能绑定为业务工作表",
                    status_code=409,
                    details={"sheet_name": sheet.sheet_name},
                )
            sheet.status = METADATA_SHEET_STATUS
            sheet.bound_role = None
            sheet.bound_node_code = None
            sheet.header_row = None
            continue
        if binding.excluded:
            sheet.status = "EXCLUDED"
            sheet.bound_role = None
            sheet.bound_node_code = None
            continue
        if binding.role is None:
            raise BusinessError(
                "SHEET_ROLE_REQUIRED",
                "业务工作表必须选择用途或排除该工作表",
                details={"sheet_name": sheet.sheet_name},
            )
        if binding.role not in supported_roles:
            raise BusinessError("SHEET_ROLE_INVALID", "工作表用途不受支持", details={"role": binding.role})
        if binding.node_code is None:
            raise BusinessError(
                "SHEET_NODE_REQUIRED",
                "业务工作表必须绑定到一个业务节点；未绑定的 Sheet 不进入执行范围",
                details={"sheet_name": sheet.sheet_name},
            )
        node = nodes.get(binding.node_code)
        if node is None:
            raise BusinessError(
                "SHEET_NODE_UNKNOWN",
                "工作表绑定的业务节点不在当前场景的节点配置里：请先在场景配置里建立该节点",
                details={
                    "sheet_name": sheet.sheet_name,
                    "node_code": binding.node_code,
                    "configured_nodes": sorted(nodes),
                },
            )
        if not node.enabled:
            raise BusinessError(
                "SHEET_NODE_DISABLED",
                f"工作表不能绑定到已停用的节点“{node.node_code}”",
                status_code=409,
                details={"sheet_name": sheet.sheet_name, "node_code": node.node_code},
            )
        allowed_types = unified_profile.node_types_of_sheet_role(profile, binding.role)
        if node.node_type not in allowed_types:
            raise BusinessError(
                "SHEET_NODE_TYPE_MISMATCH",
                f"工作表用途「{unified_profile.sheet_role_label(profile, binding.role)}」"
                f"不能绑定到节点类型「{node.node_type}」上",
                details={
                    "sheet_name": sheet.sheet_name,
                    "role": binding.role,
                    "node_code": binding.node_code,
                    "node_type": node.node_type,
                    "allowed_node_types": list(allowed_types),
                },
            )
        if binding.header_row is None:
            raise BusinessError("SHEET_HEADER_REQUIRED", "业务工作表必须选择表头行", details={"sheet_name": sheet.sheet_name})
        convertible_rows = {row["row_no"] for row in sheet.snapshot_json["preview_rows"]}
        if binding.header_row not in convertible_rows:
            raise BusinessError(
                "SHEET_HEADER_INVALID",
                "所选表头行不在当前工作表的可转换范围内，请重新选择表头行",
                details={"sheet_name": sheet.sheet_name, "header_row": binding.header_row},
            )
        sheet.bound_role = binding.role
        sheet.bound_node_code = binding.node_code
        sheet.header_row = binding.header_row
        sheet.status = "BOUND"
    # 统一协议里一个 sheet_role 只声明一次，它描述的是「这张表是什么」。
    # 两张实际 Sheet 同时绑同一个 role 会让转换器按同一份列协议读两份不同的表，
    # 后读到的覆盖先读到的，而页面看起来两张都绑好了。这里显式拒绝，
    # 不依赖前端单选，也不静默只保留其中一张。
    role_owners: dict[str, list[str]] = {}
    for sheet in sheets.values():
        if sheet.status == "BOUND" and sheet.bound_role:
            role_owners.setdefault(sheet.bound_role, []).append(sheet.sheet_name)
    duplicated = {role: names for role, names in role_owners.items() if len(names) > 1}
    if duplicated:
        labels = [
            f"{unified_profile.sheet_role_label(profile, role)}（{'、'.join(names)}）"
            for role, names in sorted(duplicated.items())
        ]
        raise BusinessError(
            "SHEET_ROLE_DUPLICATED",
            "同一个 Sheet 用途被多张工作表占用：一张用途只能对应一张表，请改为只保留一张，"
            "或给其余工作表选择各自的用途。冲突项：" + "；".join(labels),
            status_code=409,
            details={"duplicated_roles": sorted(duplicated)},
        )
    db.execute(delete(FieldCandidate).where(FieldCandidate.import_job_id == job.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_job_id == job.id))
    job.proposals_json = None
    job.decisions_json = None
    job.manifest_draft_json = None
    # 历史作业可能留有导入发布检查绑定；绑定变化后必须作废。
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1
    job.status = "PARSED"
    job.failure_code = None
    job.failure_message = None
    db.commit()
    return job


def retry_conversion(db: Session, job_no: str) -> ImportJob:
    """提交规则转换前的校验与准备；模板元数据工作表必须保持排除状态。"""
    job = _job_or_404(db, job_no, lock=True)
    _ensure_import_mutable(db, job)
    sheets = db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id)).all()
    bindings: dict[str, dict[str, Any]] = {}
    for item in sheets:
        if _is_template_metadata_sheet(item):
            if item.bound_role is not None:
                raise BusinessError(
                    "TEMPLATE_METADATA_IMMUTABLE",
                    "模板说明页不能绑定为业务工作表",
                    status_code=409,
                    details={"sheet_name": item.sheet_name},
                )
            if item.status != METADATA_SHEET_STATUS:
                # 历史导入作业把说明页记成过别的值（HIDDEN）。说明页身份由名称确定性
                # 判定，含义与 METADATA_SHEET_STATUS 完全相同，因此这里显式归一，
                # 而不是让它带着旧值进入「不是 EXCLUDED 就报错」的检查。
                item.status = METADATA_SHEET_STATUS
            bindings[item.sheet_key] = {
                "role": None,
                "node_code": None,
                "header_row": None,
                "excluded": True,
            }
            continue
        bindings[item.sheet_key] = {
            "role": item.bound_role,
            # 节点实例码随绑定一起进转换：转换器据此把这张表归到用户配置的那个节点实例上。
            "node_code": item.bound_node_code,
            "header_row": item.header_row,
            "excluded": item.status == "EXCLUDED",
        }
    db.execute(delete(FieldCandidate).where(FieldCandidate.import_job_id == job.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_job_id == job.id))
    job.proposals_json = None
    job.decisions_json = None
    job.manifest_draft_json = None
    job.coverage_json = None
    # 重新转换会整体替换候选配置，历史导入发布检查绑定随之失效。
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1
    try:
        checkpoint_ns = f"import:conversion:{public_no('CKPT')}"
        graph_state = run_sheet_conversion(
            job_no=job.job_no,
            checkpoint_ns=checkpoint_ns,
            workbook_ir=job.workbook_ir_json,
            bindings=bindings,
            scenario_code=job.scenario_code,
            schema_json=_job_schema(job),
        )
        bundle = ProposalBundle.model_validate(graph_state["proposals"])
        proposal_json = bundle.model_dump(mode="json")
        coverage_json = _bundle_coverage(bundle)
        node_notes = [note for note in (graph_state.get("node_notes") or []) if isinstance(note, dict)]
        # 可作答的待确认事项：每条进入裁决集合，与 import_graph._node_note_targets 的取舍一致。
        resolvable_notes = [note for note in node_notes if note.get("resolve")]
        if coverage_json is not None:
            coverage_json["notices"] = [
                {
                    "code": note["code"],
                    "message": note["message"],
                    "sheet_name": note["sheet_name"],
                    "row_no": note["row_no"],
                }
                for note in node_notes if not note.get("resolve")
            ]
        for note in resolvable_notes:
            resolve = note["resolve"]
            # 落库的裁决目标身份与 import_decision_signature 同口径（真实业务对象 + 被裁决属性）：
            # 另造一套合成身份会让转换期与恢复期算出两个不同的 decision_signature。
            target_ref = node_note_target_ref(note)
            db.add(
                ImportIssue(
                    import_job_id=job.id,
                    issue_no=public_no("ISS"),
                    category="BUSINESS_CONFIRMATION",
                    severity="BLOCKER",
                    status="OPEN",
                    title=note["question_title"],
                    description=note["message"],
                    source_json={
                        "source": {
                            "sheet_key": note["sheet_key"],
                            "row_no": note["row_no"],
                            "cells": [note["cell"]] if note.get("cell") else [],
                        },
                        "target_type": resolve["target_type"],
                        "target_ref": resolve["target_ref"],
                        "property": resolve["property"],
                        "input_kind": note.get("input_kind") or "SELECT",
                        "resolve": resolve,
                        "node_note_code": note["code"],
                        "node_note_target": target_ref,
                    },
                    options_json=note.get("question_options") or [],
                )
            )
        needs_decision = any(sheet.questions for sheet in bundle.sheets) or bool(resolvable_notes)
        decision_count = graph_state.get("decision_count")
        decision_signature = graph_state.get("decision_signature")
        # 待裁决集合由「模型问题 + 可作答的节点版待确认事项」共同决定，两者在同一份
        # 契约下统计。只统计 sheet.questions 会让节点版待确认事项的存在被漏判。
        # 计数与 import_decision_signature 的条目数口径一致：每条可作答事项算一条，
        # 不按目标身份去重（同一业务对象上的两个属性是两条独立待确认事项）。
        expected_decision_count = sum(len(sheet.questions) for sheet in bundle.sheets) + len(resolvable_notes)
        if graph_state.get("needs_decision") != needs_decision:
            raise BusinessError("IMPORT_GRAPH_STATE_INVALID", "导入图的待裁决状态与转换结果不一致", status_code=500)
        if not isinstance(decision_count, int) or decision_count != expected_decision_count:
            raise BusinessError("IMPORT_GRAPH_STATE_INVALID", "导入图的待裁决数量与转换结果不一致", status_code=500)
        if not isinstance(decision_signature, str) or not decision_signature:
            raise BusinessError("IMPORT_GRAPH_STATE_INVALID", "导入图未记录待裁决问题检查点", status_code=500)
        if needs_decision and not graph_state.get("interrupted"):
            raise BusinessError("IMPORT_INTERRUPT_MISSING", "导入裁决流程未产生等待点", status_code=500)
        for sheet in bundle.sheets:
            for candidate in sheet.fields:
                db.add(
                    FieldCandidate(
                        import_job_id=job.id,
                        candidate_ref=candidate.candidate_ref,
                        name=candidate.name,
                        value_type=candidate.value_type,
                        material_ref=candidate.material_ref,
                        proposal_json=candidate.model_dump(mode="json"),
                        status="PROPOSED",
                    )
                )
            for question in sheet.questions:
                db.add(
                    ImportIssue(
                        import_job_id=job.id,
                        issue_no=public_no("ISS"),
                        category="BUSINESS_AMBIGUITY",
                        severity="BLOCKER",
                        status="OPEN",
                        title=question.title,
                        description=question.description,
                        source_json={
                            "source": question.source.model_dump(mode="json"),
                            "target_type": question.target_type,
                            "target_ref": question.target_ref,
                            "property": question.property,
                            # 作答形式与“作答值如何落到候选属性”必须随问题一起保存，
                            # 否则缺少阈值这类输入题会被当成选择题处理。
                            "input_kind": question.input_kind,
                            "resolve": question.resolve.model_dump(mode="json") if question.resolve else None,
                        },
                        options_json=[item.model_dump(mode="json") for item in question.options],
                    )
                )
        job.proposals_json = proposal_json
        job.coverage_json = coverage_json
        job.decisions_json = {
            "items": [],
            "workflow": {
                "checkpoint_ns": checkpoint_ns,
                "decision_signature": decision_signature,
                "decision_count": decision_count,
                "status": "WAITING" if needs_decision else "COMPLETED",
                "stage_history": graph_state.get("stage_history", []),
            },
        }
        job.status = "PENDING_DECISION" if needs_decision else "PROPOSED"
        job.failure_code = None
        job.failure_message = None
        db.commit()
        return job
    except BusinessError as exc:
        job.status = "EXTERNAL_FAILED" if exc.code in {
            "LLM_CONFIG_MISSING",
            "LLM_TIMEOUT",
            "LLM_AUTH_FAILED",
            "LLM_NETWORK_FAILED",
            "LLM_EXTERNAL_FAILED",
        } else "CONTRACT_INVALID"
        if exc.code == "LLM_SOURCE_REF_INVALID":
            job.status = "SOURCE_REF_INVALID"
        job.failure_code = exc.code
        job.failure_message = exc.message
        db.commit()
        raise


def _input_answer_value(issue_no: str, input_kind: str, value: Any) -> Any:
    """业务输入题的作答校验：只接受能真正落到业务参数上的取值，不做默认兜底。"""
    if input_kind == "DECIMAL":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise BusinessError("IMPORT_ISSUE_VALUE_INVALID", "该问题需要填写数值", details={"issue_no": issue_no})
        try:
            numeric = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise BusinessError("IMPORT_ISSUE_VALUE_INVALID", "该问题需要填写数值", details={"issue_no": issue_no}) from exc
        if not numeric.is_finite() or numeric < 0:
            raise BusinessError(
                "IMPORT_ISSUE_VALUE_INVALID",
                "该问题需要填写不小于 0 的有限数值",
                details={"issue_no": issue_no},
            )
        return str(numeric)
    if input_kind == "TEXT":
        if not isinstance(value, str) or not value.strip():
            raise BusinessError("IMPORT_ISSUE_VALUE_INVALID", "该问题需要填写文本内容", details={"issue_no": issue_no})
        return value.strip()
    raise BusinessError(
        "IMPORT_ISSUE_VALUE_INVALID",
        "该问题的作答形式不受支持",
        details={"issue_no": issue_no, "input_kind": input_kind},
    )


def save_decisions(db: Session, job_no: str, request: ImportDecisionsRequest) -> ImportJob:
    """提交问题裁决或候选修正；问题提交仍须一次处理全部未决项。"""
    job = _job_or_404(db, job_no, lock=True)
    # 裁决与后台转换互斥：转换进行中不能提交业务结论。锁后检查，消除「先查后锁」的竞态。
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    # 提交者看到的草稿修订必须与当前一致：过期提交会覆盖别人的修改，必须显式冲突。
    if request.expected_revision is not None and request.expected_revision != job.draft_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT",
            "草稿已被其他操作修改，请刷新后重新提交裁决",
            status_code=409,
            details={"expected": request.expected_revision, "actual": job.draft_revision},
        )
    # 只要作业未发布且已完成转换就允许提交：零问题的作业也要能做候选原地修正，
    # 不再要求作业恰好停在 PENDING_DECISION。仍有未决问题时由下面的完整性校验兜底。
    if not job.proposals_json:
        raise BusinessError("IMPORT_PROPOSALS_MISSING", "规则尚未完成转换")
    issue_refs = {item.issue_no: item for item in db.scalars(select(ImportIssue).where(ImportIssue.import_job_id == job.id))}
    submitted_targets = [(item.target_type, item.target_ref) for item in request.decisions]
    if len(submitted_targets) != len(set(submitted_targets)):
        raise BusinessError("IMPORT_DECISION_DUPLICATE", "同一业务对象不能提交多个裁决")
    open_issue_nos = {
        item.issue_no
        for item in issue_refs.values()
        if item.status == "OPEN"
    }
    pending_question_sources: list[dict[str, Any]] = []
    pending_targets: set[tuple[str, str]] = set()
    for issue_no in open_issue_nos:
        source = issue_refs[issue_no].source_json
        if not isinstance(source, dict):
            raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "待裁决问题缺少来源信息")
        pending_question_sources.append(source)
        pending_targets.add((source.get("target_type"), source.get("target_ref")))
    pending_signature = import_decision_signature(pending_question_sources) if pending_question_sources else None
    submitted_issue_nos = {item.target_ref for item in request.decisions if item.target_type == "ISSUE"}
    if submitted_issue_nos and submitted_issue_nos != open_issue_nos:
        raise BusinessError("IMPORT_ISSUE_DECISIONS_INCOMPLETE", "必须一次处理当前全部待裁决问题")
    if open_issue_nos and not request.decisions:
        raise BusinessError("IMPORT_ISSUE_DECISIONS_INCOMPLETE", "必须一次处理当前全部待裁决问题")
    direct_decisions: list[dict[str, Any]] = []
    resolved_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    resolved_parameter_keys: dict[tuple[str, str], set[str]] = {}
    issue_updates: list[tuple[ImportIssue, dict[str, Any]]] = []
    proposal_refs = _proposal_refs(job.proposals_json)
    for submitted in request.decisions:
        decision = submitted.model_dump(mode="json")
        if decision["target_type"] == "ISSUE":
            issue = issue_refs.get(decision["target_ref"])
            if not issue:
                raise BusinessError("IMPORT_ISSUE_NOT_FOUND", "裁决引用的问题不存在")
            if decision["action"] != "RESOLVE" or "selected" not in decision["value"]:
                raise BusinessError("IMPORT_ISSUE_DECISION_INVALID", "问题裁决内容不完整")
            source = issue.source_json or {}
            input_kind = source.get("input_kind") or "SELECT"
            resolve = source.get("resolve") if isinstance(source.get("resolve"), dict) else None
            selected = decision["value"]["selected"]
            if input_kind == "SELECT":
                allowed = {json.dumps(item["value"], ensure_ascii=False, sort_keys=True) for item in issue.options_json or []}
                if json.dumps(selected, ensure_ascii=False, sort_keys=True) not in allowed:
                    raise BusinessError("IMPORT_ISSUE_OPTION_INVALID", "问题裁决不在允许选项中")
                resolved_value = selected
            else:
                # 缺少阈值一类参数用业务输入控件作答：没有候选选项，只按值类型校验。
                resolved_value = _input_answer_value(issue.issue_no, input_kind, selected)
                if resolve and resolve.get("mode") == "SET_PARAMETER":
                    parameter_key = resolve.get("parameter_key")
                    if not isinstance(parameter_key, str) or not parameter_key:
                        raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "业务输入问题的参数名缺失，不能保存裁决")
                    resolved_value = {**(resolve.get("base_parameters") or {}), parameter_key: resolved_value}
            target_type = source.get("target_type")
            target_ref = source.get("target_ref")
            property_name = source.get("property")
            if resolve and resolve.get("mode") == "SET_PARAMETER" and input_kind == "SELECT":
                # 选择题方式的参数作答（例如把「高」映射到具体等级）：把选中值并入已有参数，
                # 不能整体替换 parameters，否则会丢掉 action 一类的既有配置。
                parameter_key = resolve.get("parameter_key")
                if not isinstance(parameter_key, str) or not parameter_key:
                    raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "业务问题的参数名缺失，不能保存裁决")
                resolved_value = {**(resolve.get("base_parameters") or {}), parameter_key: selected}
            if (
                # 计算候选可按候选引用或业务规则编号定位；_proposal_refs 与此同口径。
                target_type not in {"MATERIAL", "FIELD", "RULE", "REPORT_SECTION", "CALC_RULE"}
                or not target_ref
                or not property_name
            ):
                raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "歧义问题缺少可裁决的业务对象")
            if target_ref not in proposal_refs[target_type]:
                raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "歧义问题引用的业务对象不存在")
            target_key = (target_type, target_ref)
            resolved = resolved_by_target.setdefault(
                target_key,
                {"target_type": target_type, "target_ref": target_ref, "action": "RESOLVE", "value": {}},
            )
            existing = resolved["value"].get(property_name)
            if resolve and resolve.get("mode") == "SET_PARAMETER":
                parameter_key = resolve["parameter_key"]
                answered_keys = resolved_parameter_keys.setdefault(target_key, set())
                if parameter_key in answered_keys:
                    raise BusinessError(
                        "IMPORT_ISSUE_RESOLUTION_CONFLICT",
                        "同一业务对象的同一参数有多项待裁决问题，请重新转换规则库",
                        details={"target_type": target_type, "target_ref": target_ref, "parameter_key": parameter_key},
                    )
                answered_keys.add(parameter_key)
                resolved["value"][property_name] = {
                    **resolved_value,
                    **(existing or {}),
                    parameter_key: resolved_value[parameter_key],
                }
            elif property_name in resolved["value"] and existing != resolved_value:
                raise BusinessError(
                    "IMPORT_ISSUE_RESOLUTION_CONFLICT",
                    "同一业务对象的同一属性有相互冲突的裁决问题，请重新转换规则库",
                    details={"target_type": target_type, "target_ref": target_ref, "property": property_name},
                )
            else:
                resolved["value"][property_name] = resolved_value
            issue_updates.append((issue, decision["value"]))
        else:
            if decision["target_ref"] not in proposal_refs[decision["target_type"]]:
                raise BusinessError("IMPORT_DECISION_TARGET_NOT_FOUND", "裁决引用的业务对象不存在", details={"target_ref": decision["target_ref"]})
            if (decision["target_type"], decision["target_ref"]) in pending_targets:
                raise BusinessError(
                    "IMPORT_TARGET_HAS_OPEN_ISSUE",
                    "该候选仍有待处理的业务问题，请先提交对应结论后再修改或排除",
                    status_code=409,
                    details={"target_type": decision["target_type"], "target_ref": decision["target_ref"]},
                )
            if decision["action"] == "RESOLVE":
                if decision["target_type"] == "SHEET" and "suggested_role" in decision["value"]:
                    raise BusinessError(
                        "IMPORT_SHEET_REBIND_REQUIRED",
                        "工作表用途只能在绑定步骤修改，请重新绑定并转换规则库",
                        status_code=409,
                        details={"sheet_key": decision["target_ref"]},
                    )
                # 直接裁决的属性白名单沿用 manifest.RESOLVABLE_PROPERTIES，非法属性当场报错，
                # 不留到编译期才暴露。
                allowed = RESOLVABLE_PROPERTIES.get(decision["target_type"], set())
                invalid = sorted(key for key in decision["value"] if key not in allowed)
                if invalid:
                    raise BusinessError(
                        "MANIFEST_DECISION_PROPERTY_INVALID",
                        f"裁决不能修改属性：{'、'.join(invalid)}",
                        details={"target_type": decision["target_type"], "property": invalid},
                    )
            direct_decisions.append(decision)
    workflow = (job.decisions_json or {}).get("workflow")
    merged_decisions = _merge_decision_items(
        job.decisions_json, [*direct_decisions, *resolved_by_target.values()], workflow
    )
    # 先校验本轮裁决的候选类型，再恢复不可由数据库回滚的图检查点。
    resolve_saved_decisions(job.proposals_json, merged_decisions["items"])
    if submitted_issue_nos:
        if not isinstance(workflow, dict):
            error = BusinessError("IMPORT_CHECKPOINT_MISSING", "导入裁决流程检查点不存在，不能恢复裁决", status_code=409)
            _record_import_checkpoint_failure(db, job, error)
            raise error
        checkpoint_ns = workflow.get("checkpoint_ns")
        if not isinstance(checkpoint_ns, str) or not checkpoint_ns or workflow.get("status") != "WAITING":
            error = BusinessError("IMPORT_CHECKPOINT_MISSING", "导入裁决流程检查点不存在或已失效，不能恢复裁决", status_code=409)
            _record_import_checkpoint_failure(db, job, error)
            raise error
        decision_count = len(pending_question_sources)
        try:
            graph_state = resume_import_decision(
                job_no=job.job_no,
                checkpoint_ns=checkpoint_ns,
                decision_signature=pending_signature or "",
                decision_count=decision_count,
                decisions=[item.model_dump(mode="json") for item in request.decisions],
            )
        except BusinessError as exc:
            if exc.code == "IMPORT_CHECKPOINT_MISSING":
                _record_import_checkpoint_failure(db, job, exc)
            raise
        workflow = {
            "checkpoint_ns": checkpoint_ns,
            "decision_signature": pending_signature,
            "decision_count": decision_count,
            "status": "RESUMED",
            "stage_history": graph_state.get("stage_history", []),
        }
        merged_decisions["workflow"] = workflow
    for issue, decision_value in issue_updates:
        issue.status = "RESOLVED"
        issue.decision_json = decision_value
    job.decisions_json = merged_decisions
    # 裁决会改变候选配置，历史导入发布检查绑定随之失效。
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1
    # 会话未启用 autoflush，必须先落盘本轮的裁决状态，否则这里统计到的仍是裁决前的未决数量。
    db.flush()
    unresolved = db.scalar(
        select(func.count()).select_from(ImportIssue).where(ImportIssue.import_job_id == job.id, ImportIssue.status == "OPEN")
    )
    job.status = "CONFIRMED" if unresolved == 0 else "PENDING_DECISION"
    db.commit()
    return job


def _merge_decision_items(
    previous: dict[str, Any] | None,
    submissions: list[dict[str, Any]],
    workflow: Any,
) -> dict[str, Any]:
    """按目标合并裁决：RESOLVE 按属性更新，其他动作替换目标状态。

    直接裁决与问题裁决可能分多批提交，整体替换会把此前批次的非 ISSUE 直接裁决丢掉。
    合并键是 (target_type, target_ref)；同一候选分批修改不同属性时保留先前属性。
    parameters 是完整的业务参数对象，按属性整体替换；同一轮的参数问题已在上游汇总。
    被覆盖的旧值连同替换时间写入 history，不静默丢弃。
    直接裁决的属性白名单仍由 manifest.RESOLVABLE_PROPERTIES 在编译期统一校验。
    """
    payload = dict(previous) if isinstance(previous, dict) else {"items": []}
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload.get("items", []):
        if isinstance(item, dict) and item.get("target_type") and item.get("target_ref"):
            merged[(item["target_type"], item["target_ref"])] = item
    history = [item for item in payload.get("history", []) if isinstance(item, dict)]
    replaced_at = datetime.now(UTC).isoformat()
    for item in submissions:
        key = (item["target_type"], item["target_ref"])
        replaced = merged.get(key)
        effective = item
        if replaced is not None and replaced["action"] == item["action"] == "RESOLVE":
            effective = {**item, "value": {**replaced["value"], **item["value"]}}
        if replaced is not None and replaced != effective:
            history.append({"replaced_at": replaced_at, "previous": replaced, "replacement": effective})
        merged[key] = effective
    payload["items"] = list(merged.values())
    payload["history"] = history
    if isinstance(workflow, dict):
        payload["workflow"] = workflow
    else:
        payload.pop("workflow", None)
    return payload


def save_decision_draft(db: Session, job_no: str, answers: dict[str, Any]) -> ImportJob:
    """暂存业务问题的作答，不改变候选、问题状态与草稿修订。

    暂存只是让页面在提交前保留已填内容，因此不递增 draft_revision，也不改变候选、
    问题状态或历史导入发布检查绑定；正式裁决仍由 save_decisions 提交。
    """
    job = _job_or_404(db, job_no, lock=True)
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    if not job.proposals_json:
        raise BusinessError("IMPORT_PROPOSALS_MISSING", "规则尚未完成转换")
    if not isinstance(answers, dict):
        raise BusinessError("IMPORT_DECISION_DRAFT_INVALID", "暂存作答必须是问题号到作答值的映射")
    known_issue_nos = set(db.scalars(select(ImportIssue.issue_no).where(ImportIssue.import_job_id == job.id)))
    unknown = sorted(str(key) for key in answers if key not in known_issue_nos)
    if unknown:
        raise BusinessError(
            "IMPORT_ISSUE_NOT_FOUND",
            "暂存作答引用了不存在的问题",
            details={"issue_no": unknown[:20]},
        )
    payload = dict(job.decisions_json) if isinstance(job.decisions_json, dict) else {"items": []}
    draft = payload.get("draft_answers")
    merged = dict(draft) if isinstance(draft, dict) else {}
    merged.update(answers)
    if not merged:
        # 空暂存等价于清空，保持不写入空对象。
        payload.pop("draft_answers", None)
    else:
        payload["draft_answers"] = merged
    job.decisions_json = payload
    db.commit()
    return job


def _proposal_refs(proposals: dict[str, Any]) -> dict[str, set[str]]:
    result = {"MATERIAL": set(), "FIELD": set(), "RULE": set(), "REPORT_SECTION": set(), "SHEET": set(), "CALC_RULE": set()}
    # 计算规则候选跨 Sheet（定义 / 参数 / 判定三张表按编号关联），因此挂在 bundle 顶层，
    # 不在 sheets 里；漏了它会让 CALC_RULE 的裁决在校验阶段直接 KeyError。
    for item in proposals.get("calculations", []) or []:
        if item.get("candidate_ref"):
            result["CALC_RULE"].add(item["candidate_ref"])
        if item.get("rule_code"):
            result["CALC_RULE"].add(item["rule_code"])
    for sheet in proposals.get("sheets", []):
        result["SHEET"].add(sheet["sheet_key"])
        for proposal_type, key, target_type in (
            ("materials", "candidate_ref", "MATERIAL"),
            ("fields", "candidate_ref", "FIELD"),
            ("rules", "candidate_ref", "RULE"),
            ("report_sections", "candidate_ref", "REPORT_SECTION"),
        ):
            for item in sheet.get(proposal_type, []):
                result[target_type].add(item[key])
    return result


# 「配置可保存」不等于「规则可执行」。覆盖结果由 domain.capabilities 计算，
# 并随已发布版本一起冻结进 manifest（参与 manifest_hash）。这里把未实现的
# 覆盖项转换为场景发布检查的阻断项。
def _capability_blockers(
    coverage: dict[str, Any],
    acknowledged: list[str],
) -> list[dict[str, Any]]:
    """能力缺口默认阻断发布；只有被逐条确认的规则键才放行。

    确认过的缺口随版本冻结（见 manifest.capability_gaps 与 manifest.capability_coverage），
    报告与任务范围里继续可见；未列出的缺口继续阻断。
    确认不是“忽略全部缺口”的开关：它逐条列出使用者已看过并接受的规则键。
    """
    acknowledged_keys = set(acknowledged)
    gaps = [row for row in coverage.get("rules", []) if not row.get("implemented")]
    blockers: list[dict[str, Any]] = []
    for row in gaps:
        if row.get("rule_key") in acknowledged_keys:
            continue
        blockers.append(
            {
                "code": "CAPABILITY_UNIMPLEMENTED",
                "message": (
                    f"规则“{row['rule_name']}”引用的能力"
                    f"“{row.get('capability_label') or row.get('capability_id')}”当前没有判定实现，"
                    "发布后运行时会明确报“不可判定”，不会给出结论"
                    f"（{row.get('unimplemented_reason') or '未说明'}）"
                ),
                "details": {
                    "rule_key": row.get("rule_key"),
                    "rule_name": row.get("rule_name"),
                    "capability_id": row.get("capability_id"),
                    "capability_version": row.get("capability_version"),
                    "unimplemented_reason": row.get("unimplemented_reason"),
                },
            }
        )
    stale = sorted(acknowledged_keys - {row.get("rule_key") for row in gaps})
    if stale:
        # 确认集合里出现本次并不存在的缺口，说明确认基于另一份配置，必须显式阻断。
        blockers.append(
            {
                "code": "CAPABILITY_ACKNOWLEDGEMENT_STALE",
                "message": "确认的能力缺口与本次配置不一致，请重新执行发布检查后再确认",
                "details": {"rule_keys": stale},
            }
        )
    return blockers


def _validated_bound_sheets(
    db: Session,
    job: ImportJob,
    template: ScenarioTemplate,
    sheets: list[ImportSheet],
) -> list[dict[str, Any]]:
    """写草稿前**重新校验**每个已绑定 Sheet，返回本次选用的 Sheet 清单。

    绑定是在导入工作台确认的，而写草稿发生在之后：这期间用户可能删掉节点、停用节点、
    改掉节点能力类型，或新导入一份规则库。此时按**过期绑定**编译会把一份对不上的配置
    写进草稿。因此这里按提交时刻的有效状态逐项复核，任何一项不通过就整次拒绝——
    调用方在替换草稿之前调用它，草稿不会被覆盖成半成品。

    校验项（与 `update_sheet_bindings` 的确认口径一致）：

    - role 属于统一协议；
    - header_row 仍在可转换范围内；
    - node_code 仍存在、仍启用、且其能力类型仍在该 Sheet 允许的类型中；
    - role 没有重复（两张表争同一个用途）。
    """
    profile = _job_profile(job)
    supported_roles = set(unified_profile.sheet_index(profile))
    nodes = _draft_nodes_of_scenario(db, template.scenario_code)
    selected: list[dict[str, Any]] = []
    role_owners: dict[str, list[str]] = {}
    for item in sheets:
        if item.status != "BOUND":
            continue
        if not item.bound_role or not item.bound_node_code:
            raise BusinessError(
                "IMPORT_BINDING_INCOMPLETE",
                f"工作表“{item.sheet_name}”标记为已绑定，却缺少用途或节点",
                status_code=409,
                details={"sheet_name": item.sheet_name, "job_no": job.job_no},
            )
        role = item.bound_role
        if role not in supported_roles:
            raise BusinessError(
                "SHEET_ROLE_INVALID",
                f"工作表“{item.sheet_name}”的用途“{role}”不在统一模板协议里，请重新确认绑定",
                status_code=409,
                details={"sheet_name": item.sheet_name, "role": role},
            )
        header_row = item.header_row
        convertible_rows = {row["row_no"] for row in item.snapshot_json["preview_rows"]}
        if not isinstance(header_row, int) or header_row not in convertible_rows:
            raise BusinessError(
                "SHEET_HEADER_INVALID",
                f"工作表“{item.sheet_name}”确认的表头行已不在可转换范围内，请重新确认表头行",
                status_code=409,
                details={"sheet_name": item.sheet_name, "header_row": header_row},
            )
        node = nodes.get(item.bound_node_code)
        if node is None:
            raise BusinessError(
                "SHEET_NODE_UNKNOWN",
                f"工作表“{item.sheet_name}”绑定的节点“{item.bound_node_code}”已不在当前场景的节点配置里，"
                "请重新确认绑定",
                status_code=409,
                details={
                    "sheet_name": item.sheet_name,
                    "node_code": item.bound_node_code,
                    "configured_nodes": sorted(nodes),
                },
            )
        if not node.enabled:
            raise BusinessError(
                "SHEET_NODE_DISABLED",
                f"工作表“{item.sheet_name}”绑定的节点“{node.node_code}”已停用，请重新确认绑定",
                status_code=409,
                details={"sheet_name": item.sheet_name, "node_code": node.node_code},
            )
        allowed_types = unified_profile.node_types_of_sheet_role(profile, role)
        if node.node_type not in allowed_types:
            raise BusinessError(
                "SHEET_NODE_TYPE_MISMATCH",
                f"工作表用途「{unified_profile.sheet_role_label(profile, role)}」与节点“{node.node_code}”"
                f"的能力类型“{node.node_type}”不再匹配，请重新确认绑定",
                status_code=409,
                details={
                    "sheet_name": item.sheet_name,
                    "role": role,
                    "node_code": node.node_code,
                    "node_type": node.node_type,
                    "allowed_node_types": list(allowed_types),
                },
            )
        role_owners.setdefault(role, []).append(item.sheet_name)
        selected.append({"sheet_key": item.sheet_key, "sheet_name": item.sheet_name, "sheet_role": role, "node_code": node.node_code})
    duplicated = {role: names for role, names in role_owners.items() if len(names) > 1}
    if duplicated:
        labels = [
            f"{unified_profile.sheet_role_label(profile, role)}（{'、'.join(names)}）"
            for role, names in sorted(duplicated.items())
        ]
        raise BusinessError(
            "SHEET_ROLE_DUPLICATED",
            "同一个 Sheet 用途被多张工作表占用：一张用途只能对应一张表。冲突项：" + "；".join(labels),
            status_code=409,
            details={"duplicated_roles": sorted(duplicated)},
        )
    if not selected:
        raise BusinessError(
            "SCENARIO_SHEET_REQUIRED",
            "本次导入没有已确认绑定的 Sheet，不能写入场景草稿",
            status_code=409,
            details={"job_no": job.job_no},
        )
    return selected


def apply_import_to_draft(
    db: Session, job_no: str, *, expected_job_revision: int, expected_scene_revision: int
) -> ImportJob:
    """把已确认的导入结果写入场景草稿，不生成或发布场景版本。

    不创建场景。场景身份以上传时记录的 scenario_code 为准，
    不从文件名、Sheet 名或表头另找一个场景。写入会替换该场景当前草稿里的
    材料、字段、规则、报告、选用 Sheet 与计算候选；节点实例保留
    场景草稿，不会按 Excel 另造一套流程。

    **写草稿前先按提交时刻的有效绑定复校**（`_validated_bound_sheets`）：
    绑定确认之后节点可能被删除、停用或改类型，按过期绑定编译会把一份对不上的配置
    写进草稿。任一校验失败时整次拒绝，草稿不会被覆盖后只留下一个 `sync_error`。
    """
    from .operation_types import OBJECT_SCENARIO
    from .scenario_admin_service import (
        _bump_draft,
        _latest_published_version,
        _commit_config_change,
        ensure_draft,
        load_import_config_into_draft,
        template_or_404,
    )

    job = _job_or_404(db, job_no, lock=True)
    if job.status == "APPLIED":
        return job
    if job.draft_revision != expected_job_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT",
            "导入候选在本次确认期间已修改，请重新核对后再写入场景草稿",
            status_code=409,
            details={"job_no": job_no, "expected": expected_job_revision, "actual": job.draft_revision},
        )
    existing_version_id = db.scalar(select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == job.id).limit(1))
    if job.status == "PUBLISHED" or existing_version_id is not None:
        raise BusinessError(
            "IMPORT_ALREADY_PUBLISHED",
            "该导入作业已经产生过场景版本，不能再改写场景草稿",
            status_code=409,
            details={"job_no": job.job_no},
        )
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    if not job.scenario_code:
        raise BusinessError(
            "IMPORT_SCENARIO_REQUIRED",
            "导入作业没有所属场景，不能写入草稿。请先创建场景，再在该场景下重新上传",
            status_code=409,
            details={"job_no": job.job_no},
        )
    template = template_or_404(db, job.scenario_code, lock=True)
    if template.deleted_at is not None:
        raise BusinessError(
            "SCENARIO_DELETED",
            "场景已删除，不能写入草稿",
            status_code=409,
            details={"scenario_code": job.scenario_code},
        )
    job_schema = _job_schema(job)
    template_schema = scenario_schema.normalize_schema(template.schema_json)
    if job_schema != template_schema or job.schema_key != template.schema_key or job.schema_version != template.schema_version:
        raise BusinessError(
            "IMPORT_SCENARIO_SCHEMA_CHANGED",
            "场景导入协议已在上传后改变，请按当前场景重新导入规则库",
            status_code=409,
            details={"job_no": job.job_no, "scenario_code": job.scenario_code},
        )
    if job.status not in {"PROPOSED", "PENDING_DECISION", "CONFIRMED"}:
        raise BusinessError("IMPORT_STATUS_NOT_READY", "导入作业尚未成功完成规则转换", status_code=409)
    if not job.proposals_json:
        raise BusinessError("PROPOSALS_MISSING", "规则尚未转换", status_code=409)
    unresolved = db.scalar(
        select(func.count()).select_from(ImportIssue).where(ImportIssue.import_job_id == job.id, ImportIssue.status == "OPEN")
    )
    if unresolved:
        raise BusinessError(
            "ISSUES_UNRESOLVED",
            f"仍有 {unresolved} 个问题未处理，不能写入场景草稿",
            status_code=409,
            details={"unresolved": int(unresolved)},
        )
    sheets = db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id)).all()
    selected_sheets = _validated_bound_sheets(db, job, template, list(sheets))
    try:
        draft = ensure_draft(db, template)
        if (
            draft.schema_json != job_schema
            or draft.schema_key != job.schema_key
            or draft.schema_version != job.schema_version
        ):
            raise BusinessError(
                "IMPORT_DRAFT_SCHEMA_MISMATCH",
                "场景草稿的导入协议与当前作业不一致，请重新导入规则库",
                status_code=409,
                details={"job_no": job.job_no, "scenario_code": job.scenario_code},
            )
        if draft.revision_no != expected_scene_revision:
            raise BusinessError(
                "SCENARIO_DRAFT_REVISION_CONFLICT",
                "场景草稿在本次确认期间已修改，请重新核对后再写入",
                status_code=409,
                details={
                    "scenario_code": template.scenario_code,
                    "expected": expected_scene_revision,
                    "actual": draft.revision_no,
                },
            )
        config = draft_config_of_import(
            proposals=job.proposals_json or {},
            decisions=(job.decisions_json or {}).get("items", []),
            namespace=template.scenario_code,
            selected_sheets=selected_sheets,
            import_job_no=job.job_no,
        )
        load_import_config_into_draft(db, draft, config, profile_version=job.profile_version)
        draft.source = "EXCEL"
        latest = _latest_published_version(db, template.scenario_code)
        if latest is not None and draft.base_version_no is None:
            draft.base_version_no = latest.version_no
        _bump_draft(draft)
        job.status = "APPLIED"
        job.manifest_draft_json = None
        record_operation(
            db,
            object_type=OBJECT_IMPORT_JOB,
            object_no=job.job_no,
            object_name=job.source_filename,
            operation_type="APPLY_DRAFT",
            detail={"job_no": job.job_no, "scenario_code": template.scenario_code},
        )
        record_operation(
            db,
            object_type=OBJECT_SCENARIO,
            object_no=template.scenario_code,
            object_name=template.scenario_name,
            operation_type="IMPORT_APPLY",
            detail={"job_no": job.job_no, "scenario_code": template.scenario_code, "sheet_count": len(config["sheets"])},
        )
        _commit_config_change(db, template, draft)
    except Exception:
        db.rollback()
        raise
    db.refresh(job)
    return job


def publish_import(db: Session, job_no: str, request: PublishRequest) -> ScenarioVersion:
    """历史作业若已经产生场景版本，重复调用仍返回该版本。

    **只读行为**：本函数不修改作业状态、不写任何状态机，也不产生新版本。
    新的导入不在导入页发布场景——可执行的场景版本只由管理员在场景页显式发布。

    已有版本时核对本次请求与当时的发布检查绑定一致（草稿修订、场景身份、逐条确认的
    能力缺口），否则不同输入可以借同一个作业伪装成同一次历史发布。绑定不存在时
    明确拒绝，不当作「没有差异」放行。
    """
    job = _job_or_404(db, job_no, lock=True)
    existing_for_job = db.scalar(select(ScenarioVersion).where(ScenarioVersion.import_job_id == job.id))
    if existing_for_job:
        binding = job.precheck_json if isinstance(job.precheck_json, dict) else None
        if binding is None:
            raise BusinessError(
                "PUBLISH_PRECHECK_REQUIRED",
                "该作业已有场景版本但缺少当时的发布检查绑定，无法确认重复调用的意图一致",
                status_code=409,
            )
        changed: list[str] = []
        if binding.get("draft_revision") != job.draft_revision:
            changed.append("草稿修订")
        if binding.get("scene_code") != job.scenario_code:
            changed.append("场景")
        if (binding.get("scene_name") or "") != (request.scenario_name or ""):
            changed.append("场景名称")
        recorded_ack = sorted(set(binding.get("acknowledged_capability_gaps") or []))
        if recorded_ack != sorted(set(request.acknowledged_capability_gaps)):
            changed.append("已确认的能力缺口")
        if changed:
            raise BusinessError(
                "PUBLISH_PREVIEW_MISMATCH",
                f"本次发布请求与该作业当时的发布检查结果不一致（{'、'.join(changed)}已变化）",
                status_code=409,
                details={"changed": changed, "job_no": job.job_no},
            )
        return existing_for_job
    raise BusinessError(
        "IMPORT_DOES_NOT_PUBLISH_SCENE",
        "规则库导入不能直接发布场景。请把本次导入写入该场景草稿，在场景页核对配置后执行「发布场景」",
        status_code=409,
        details={
            "job_no": job.job_no,
            "scenario_code": job.scenario_code,
            "apply_path": f"/import-jobs/{job.job_no}/apply-draft",
        },
    )
