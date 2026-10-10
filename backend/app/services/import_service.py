import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..repositories import configuration_store as config_store

from ..config import get_settings
from ..domain.manifest import (
    RESOLVABLE_PROPERTIES,
    calculation_decision_aliases,
    draft_config_of_import,
    excluded_targets,
    import_origin_of,
    normalize_decision_targets,
    normalize_resolution_value,
    resolved_targets,
    resolve_saved_decisions,
    rule_comparison_view,
)
from ..domain.proposals import ProposalBundle, report_property_target_refs
from ..domain.workbook import WorkbookIR
from ..common.exceptions import BusinessError
from ..graphs.import_graph import (
    import_decision_signature,
    run_sheet_conversion,
    run_workbook_read,
)
from ..entity import FieldCandidate, ImportIssue, ImportJob, ImportSheet
from ..entity import (
    BackgroundOperation,
    ScenarioDraft,
    ScenarioDraftNode,
    ScenarioTemplate,
    ScenarioVersion,
)
from ..schema.dto import ImportDecisionsRequest, PublishRequest, SheetBindingsRequest
from .background_service import ACTIVE_STATUSES, assert_no_active_operation, latest_operation, operation_view
from ..domain.identity import public_no
from .operation_types import OBJECT_IMPORT_JOB
from .operation_log_service import record_operation
from ..domain import scenario_schema, node_profile
from .storage_service import RULEBOOK_EXTENSIONS, save_upload


# 工作表不参与解析时的状态值，保存选择与转换共用。
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
    """只有空表由系统排除；说明内容由模型按真实单元格识别。"""
    return sheet.snapshot_json["state"] == "EMPTY"




async def create_import_job(db: Session, upload: UploadFile, *, scenario_code: str) -> ImportJob:
    """在指定场景下上传规则库、读取工作簿并建立导入作业。

    `scenario_code` 是必填的：上传流程以场景为起点（用户先建场景、再在该场景下上传），
    场景不由 Excel 表头推断；解析使用该场景的实际节点与能力目录。

    场景必须真实存在且未删除：写一个不存在的场景编号会让作业挂在一个查不到的场景上。
    """
    settings = get_settings()
    # 场景必须真实存在且未删除：写一个不存在的场景编号会让作业挂在一个查不到的场景上。
    template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))
    if template is None:
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
            # 记录内部节点能力与历史参考格式版本，不约束上传文件的业务表头。
            profile_version=workbook_ir.profile_version,
            # 上传入口的场景身份：作业明确属于哪个场景，不从 Excel 内容猜。
            scenario_code=scenario_code,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
            schema_json=schema,
            profile_display_version=node_profile.NODE_PROFILE_VERSION,
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
        # 默认选择所有非空表；布局与业务语义交由 LLM 理解。
        for sheet in workbook_ir.sheets:
            status = "EXCLUDED" if sheet.state == "EMPTY" else "BOUND"
            db.add(
                ImportSheet(
                    import_job_id=job.id,
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    visible=sheet.visible,
                    status=status,
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
    node_names: dict[str, str],
    node_types: dict[str, str],
) -> dict[str, Any] | None:
    """业务候选视图。

    取值来源是「原始候选 + 已保存裁决」的叠加结果：用户改过的字段类型、材料引用和
    说明在刷新后仍然有效，而原始候选、来源 Sheet/行/单元格始终保持原样不被覆盖。
    每个候选带一个业务状态：自动识别 / 待补充 / 已人工处理 / 已排除。
    """
    proposals = job.proposals_json
    if not isinstance(proposals, dict):
        return None
    decisions = normalize_decision_targets(
        proposals, [item for item in (job.decisions_json or {}).get("items", []) if isinstance(item, dict)],
    )
    effective = resolve_saved_decisions(proposals, decisions, node_types=node_types)
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
    calculation_meta = {
        item["result_code"]: {
            "rule_name": item["rule_name"],
            "node_name": node_names.get(item["node_code"]),
        }
        for item in effective.get("calculations", [])
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
            comparison = rule_comparison_view(
                item, material_names, field_labels, rule_names, field_meta,
                calculation_meta=calculation_meta,
            )
            rule_views.append(
                {
                    "candidate_ref": item["candidate_ref"],
                    "name": item["name"],
                    "node": item["node"],
                    "node_name": node_names.get(item["node"]),
                    "operator": item["operator"],
                    "config_status": item.get("config_status"),
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
                "question_count": 0,
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
                "execution_mode": item.get("execution_mode", "CHECK"),
                "execution_engine": item.get("execution_engine", "PROGRAM"),
                "judgement_instruction": item.get("judgement_instruction", ""),
                "formula": item["formula"],
                "result_code": item["result_code"],
                "result_type": item["result_type"],
                "node_code": item["node_code"],
                "node_name": node_names.get(item["node_code"]),
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
        notes.append(f"本表共 {snapshot['max_row']} 行，预览仅显示前 {settings.workbook_preview_rows} 行，解析读取完整内容")
    if snapshot.get("columns_truncated"):
        notes.append(f"本表共 {snapshot['max_column']} 列，预览仅显示前 {settings.workbook_preview_columns} 列，解析读取完整内容")
    if snapshot.get("cells_truncated"):
        notes.append(f"部分单元格内容超过 {settings.workbook_cell_chars} 字，超出部分仅在预览中省略，解析读取完整原文")
    return "；".join(notes) if notes else None


def _job_profile_summary(db: Session, job: ImportJob) -> dict[str, Any]:
    """返回节点能力摘要与作业所属场景身份。"""
    summary = node_profile.node_summary()
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

    先只排 id，再按 id 取行，不直接对带 `snapshot_json` 的行做排序：`import_sheet` 每行带着
    这张表的整份读取快照（实测单表 261 KB），而 MySQL 的 filesort 要求整行放进
    `sort_buffer_size`（本机 256 KB），行一旦大于缓冲，查询直接报 1038 Out of sort memory，
    整个导入详情页 500。只排 id 时参与排序的是 8 字节整数，与行大小无关。

    这不是「调大缓冲」能根治的：快照大小由上传文件决定，没有上界，换更大的缓冲只是把同一个
    错误推到更大的文件上。
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
    # 历史业务审批记录仍保留，不再作为作答事项或候选待处理状态返回。
    issues = db.scalars(select(ImportIssue).where(
        ImportIssue.import_job_id == job.id,
        ImportIssue.category.notin_(["BUSINESS_AMBIGUITY", "BUSINESS_CONFIRMATION"]),
    ).order_by(ImportIssue.id)).all()
    sheet_names = {item.sheet_key: item.sheet_name for item in sheets}
    report_sections = {
        section["candidate_ref"]: section
        for sheet in (job.proposals_json or {}).get("sheets", [])
        for section in sheet.get("report_sections", [])
    }
    open_targets = set()
    for item in issues:
        if item.status != "OPEN" or not isinstance(item.source_json, dict):
            continue
        source = item.source_json
        if source.get("resolution_notice"):
            continue
        target_type, target_ref = source.get("target_type"), source.get("target_ref")
        target_refs = (
            report_property_target_refs(report_sections, target_ref, source.get("property"))
            if target_type == "REPORT_SECTION" else [target_ref]
        )
        open_targets.update((target_type, ref) for ref in target_refs)
    # 暂存作答只用于回填未决问题；已裁决的问题以 decision_json 为准。
    draft_answers = (job.decisions_json or {}).get("draft_answers")
    draft_answers = draft_answers if isinstance(draft_answers, dict) else {}
    manifest_draft = job.manifest_draft_json if isinstance(job.manifest_draft_json, dict) else {}
    draft_nodes = _draft_nodes_of_scenario(db, job.scenario_code)
    return {
        "job_no": job.job_no,
        "source_filename": job.source_filename,
        "status": job.status,
        "profile_version": job.profile_version,
        "schema_key": job.schema_key,
        "schema_version": job.schema_version,
        "schema_json": _job_schema(job),
        # 作业所属场景及节点能力摘要；解析范围来自实际工作表选择。
        "profile": _job_profile_summary(db, job),
        "draft_revision": job.draft_revision,
        "sheets": [
            {
                "sheet_key": item.sheet_key,
                "name": item.sheet_name,
                "visible": item.visible,
                "status": item.status,
                # 只有空表标为系统排除；说明页和隐藏表不因名称或展示属性被排除。
                "fixed_excluded": _is_template_metadata_sheet(item),
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
                "resolution_notice": (item.source_json or {}).get("resolution_notice"),
                "options": item.options_json,
                # 未决问题优先回填暂存作答，其次才是上一次的裁决记录。
                "decision": draft_answers.get(item.issue_no)
                if item.status == "OPEN" and item.issue_no in draft_answers
                else item.decision_json,
            }
            for item in issues
        ],
        "proposals": _proposal_business_view(
            job, sheet_names, open_targets,
            {code: node.name for code, node in draft_nodes.items()},
            {code: node.node_type for code, node in draft_nodes.items()},
        ),
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

    只把命令本身标成终态 INTERRUPTED 并释放活跃占用，让作业回到可重试 / 可删除的状态。这是
    「释放占用」而不是「回滚」：作业状态与草稿数据一概不改，重试时由 retry_conversion 按自己
    的草稿修订整体重建。没有活跃操作时明确报错，不静默成功。

    不强杀正在等待模型的线程；模型返回后写入前必须重新检查命令状态，中止结果不能覆盖草稿。
    模型调用期间不持有数据库事务。中止与转换落库都按「作业、命令」顺序加锁，
    保证中止提交以后，旧转换不能再写入候选或覆盖中止状态。
    """
    job = _job_or_404(db, job_no, lock=True)
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
    previous_status = operation.status
    operation.status = "INTERRUPTED"
    operation.failure_code = "OPERATION_ABORTED"
    operation.failure_message = "该操作已被人工中止，可重新发起转换"
    operation.finished_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_IMPORT_JOB,
        object_no=job_no,
        object_name=job.source_filename,
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

    用于模型能力目录、候选归属展示和节点引用核对；没有场景或草稿时返回空字典。
    """
    if not scenario_code:
        return {}
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    if draft is None:
        return {}
    return {
        item.node_code: item
        for item in config_store.rows(db, ScenarioDraftNode, draft.id)
    }


def update_sheet_bindings(db: Session, job_no: str, request: SheetBindingsRequest) -> ImportJob:
    """保存本次参与解析的工作表；用途、表头和业务引用由 LLM 理解。"""
    job = _job_or_404(db, job_no, lock=True)
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    sheets = {item.sheet_key: item for item in db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id))}
    keys = [item.sheet_key for item in request.bindings]
    if len(keys) != len(set(keys)) or set(keys) != set(sheets):
        raise BusinessError("SHEET_BINDING_INCOMPLETE", "工作表选择必须包含全部实际工作表，且不能重复")
    for binding in request.bindings:
        sheet = sheets[binding.sheet_key]
        sheet.status = "EXCLUDED" if binding.excluded else "BOUND"
    db.execute(delete(FieldCandidate).where(FieldCandidate.import_job_id == job.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_job_id == job.id))
    job.proposals_json = None
    job.decisions_json = None
    job.manifest_draft_json = None
    # 解析范围变化后，历史导入发布检查记录不再有效。
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1
    job.status = "PARSED"
    job.failure_code = None
    job.failure_message = None
    db.commit()
    return job




def _conversion_job_for_write(
    db: Session, job_no: str, operation_no: str, *, draft_revision: int | None = None,
) -> ImportJob:
    """短事务内确认转换仍有效；与中止共用加锁顺序，拒绝旧线程写回。"""
    job = _job_or_404(db, job_no, lock=True)
    operation = db.scalar(
        select(BackgroundOperation).where(
            BackgroundOperation.operation_no == operation_no,
            BackgroundOperation.object_type == OBJECT_IMPORT_JOB,
            BackgroundOperation.object_no == job_no,
        ).with_for_update().execution_options(populate_existing=True)
    )
    if operation is None:
        raise BusinessError("OPERATION_NOT_FOUND", "转换操作不存在", status_code=404)
    if operation.status != "RUNNING":
        raise BusinessError(
            "OPERATION_NOT_ACTIVE", "转换操作已结束或被中止，拒绝写入旧结果", status_code=409,
            details={"operation_no": operation_no, "status": operation.status},
        )
    if draft_revision is not None and job.draft_revision != draft_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT", "导入配置已变化，拒绝写入旧转换结果", status_code=409,
            details={"job_no": job_no},
        )
    _ensure_import_mutable(db, job)
    return job


def _reset_conversion_candidates(db: Session, job: ImportJob) -> None:
    """新结果与旧候选的替换在同一短事务中提交。"""
    db.execute(delete(FieldCandidate).where(FieldCandidate.import_job_id == job.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_job_id == job.id))
    job.proposals_json = None
    job.decisions_json = None
    job.manifest_draft_json = None
    job.coverage_json = None
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1


def retry_conversion(db: Session, job_no: str, *, operation_no: str) -> ImportJob:
    """读取输入后释放事务，模型返回时再锁定作业并提交结果。"""
    job = _conversion_job_for_write(db, job_no, operation_no)
    sheets = _ordered_sheets_of(db, job.id)
    nodes = [
        {"node_code": node.node_code, "node_type": node.node_type, "name": node.name,
         "enabled": node.enabled, "depends_on": node.depends_on}
        for node in _draft_nodes_of_scenario(db, job.scenario_code).values()
    ]
    bindings = {item.sheet_key: {"excluded": item.status == "EXCLUDED", "available_nodes": nodes} for item in sheets}
    draft_revision = job.draft_revision
    workbook_ir = job.workbook_ir_json
    scenario_code = job.scenario_code
    schema_json = _job_schema(job)
    # 此前只有读取：显式结束事务并释放行锁，后续不得访问已过期的 ORM 输入。
    db.rollback()
    try:
        checkpoint_ns = f"import:conversion:{public_no('CKPT')}"
        graph_state = run_sheet_conversion(
            job_no=job_no,
            operation_no=operation_no,
            checkpoint_ns=checkpoint_ns,
            workbook_ir=workbook_ir,
            bindings=bindings,
            scenario_code=scenario_code,
            schema_json=schema_json,
        )
        job = _conversion_job_for_write(db, job_no, operation_no, draft_revision=draft_revision)
        _reset_conversion_candidates(db, job)
        bundle = ProposalBundle.model_validate(graph_state["proposals"])
        proposal_json = bundle.model_dump(mode="json")
        coverage_json = _bundle_coverage(bundle)
        node_notes = [note for note in (graph_state.get("node_notes") or []) if isinstance(note, dict)]
        # 导入不生成业务审批事项；技术说明仅随覆盖信息留存。
        if coverage_json is not None:
            coverage_json["notices"] = [
                {"code": note["code"], "message": note["message"],
                 "sheet_name": note["sheet_name"], "row_no": note["row_no"]}
                for note in node_notes
            ]
        needs_decision = False
        decision_count = graph_state.get("decision_count")
        decision_signature = graph_state.get("decision_signature")
        expected_decision_count = 0
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
        # 撤销本次未提交的候选；只给仍在执行且输入未变化的命令记录转换失败。
        db.rollback()
        job = _conversion_job_for_write(db, job_no, operation_no, draft_revision=draft_revision)
        _reset_conversion_candidates(db, job)
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
        if not numeric.is_finite():
            raise BusinessError(
                "IMPORT_ISSUE_VALUE_INVALID",
                "该问题需要填写有限数值",
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
    """提交可选的业务补充或候选修正，允许分批保存。"""
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
    # 新作业解析完成即可补充业务说明；历史等待作业也可分批保存答案。
    if not job.proposals_json:
        raise BusinessError("IMPORT_PROPOSALS_MISSING", "规则尚未完成转换")
    aliases = calculation_decision_aliases(job.proposals_json)
    current_proposals = resolve_saved_decisions(
        job.proposals_json, (job.decisions_json or {}).get("items", []),
        node_types={code: node.node_type for code, node in _draft_nodes_of_scenario(db, job.scenario_code).items()},
    )
    report_sections = {
        section["candidate_ref"]: section
        for sheet in current_proposals["sheets"] for section in sheet.get("report_sections", [])
    }
    excluded_sheets, excluded_refs = excluded_targets((job.decisions_json or {}).get("items", []))
    excluded_report_refs = {
        ref for ref, section in report_sections.items()
        if section["source"]["sheet_key"] in excluded_sheets or ("REPORT_SECTION", ref) in excluded_refs
    }

    def target_key(target_type: str, target_ref: str) -> tuple[str, str]:
        return target_type, aliases.get(target_ref, target_ref) if target_type == "CALC_RULE" else target_ref

    issue_refs = {item.issue_no: item for item in db.scalars(select(ImportIssue).where(
        ImportIssue.import_job_id == job.id,
        ImportIssue.category.notin_(["BUSINESS_AMBIGUITY", "BUSINESS_CONFIRMATION"]),
    ))}
    submitted_targets = [target_key(item.target_type, item.target_ref) for item in request.decisions]
    if len(submitted_targets) != len(set(submitted_targets)):
        raise BusinessError("IMPORT_DECISION_DUPLICATE", "同一业务对象不能提交多个裁决")
    open_issue_nos = {
        item.issue_no
        for item in issue_refs.values()
        if item.status == "OPEN"
    }
    submitted_issue_nos = {item.target_ref for item in request.decisions if item.target_type == "ISSUE"}
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
            if source.get("resolution_notice"):
                raise BusinessError(
                    "IMPORT_ISSUE_READ_ONLY", source["resolution_notice"],
                    details={"issue_no": issue.issue_no},
                )
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
            resolved_key = target_key(target_type, target_ref)
            resolved = resolved_by_target.setdefault(
                resolved_key,
                {"target_type": target_type, "target_ref": resolved_key[1], "action": "RESOLVE", "value": {}},
            )
            existing = resolved["value"].get(property_name)
            if resolve and resolve.get("mode") == "SET_PARAMETER":
                parameter_key = resolve["parameter_key"]
                answered_keys = resolved_parameter_keys.setdefault(resolved_key, set())
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
            if decision["action"] == "RESOLVE":
                if decision["target_type"] == "SHEET" and "suggested_role" in decision["value"]:
                    raise BusinessError(
                        "IMPORT_SHEET_REBIND_REQUIRED",
                        "历史表级用途不能通过候选裁决修改，请重新选择参与解析的工作表并解析规则库",
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
    # 报告共享属性只需作答一次，存储为同报告各章节的明确裁决，不修改模型原始返回。
    resolved_by_target = {
        target_key(item["target_type"], item["target_ref"]): item
        for item in _expand_report_resolutions(
            report_sections, list(resolved_by_target.values()), excluded_report_refs=excluded_report_refs,
        )
    }
    # 仅当该候选的全部问题已有答案时清除待确认说明；未答问题不阻断导入。
    # 显式状态作答优先。
    candidate_statuses = {
        (target_type, item["candidate_ref"]): item.get("config_status")
        for sheet in job.proposals_json["sheets"]
        for collection, target_type in (
            ("materials", "MATERIAL"), ("fields", "FIELD"),
            ("rules", "RULE"), ("report_sections", "REPORT_SECTION"),
        )
        for item in sheet.get(collection, [])
    }
    for item in job.proposals_json.get("calculations", []):
        for ref in (item["candidate_ref"], item["rule_code"]):
            candidate_statuses[("CALC_RULE", ref)] = item["status"]
    remaining_targets = {
        target_key((issue.source_json or {}).get("target_type"), (issue.source_json or {}).get("target_ref"))
        for issue_no, issue in issue_refs.items()
        if issue_no in open_issue_nos - submitted_issue_nos
    }
    for resolved_key, resolved in resolved_by_target.items():
        if candidate_statuses.get(resolved_key) == "PENDING" and resolved_key not in remaining_targets:
            status_property = "status" if resolved_key[0] == "CALC_RULE" else "config_status"
            resolved["value"].setdefault(status_property, "CLEAR")
    workflow = (job.decisions_json or {}).get("workflow")
    merged_decisions = _merge_decision_items(
        job.decisions_json,
        _expand_report_resolutions(
            report_sections, [*direct_decisions, *resolved_by_target.values()], excluded_report_refs=excluded_report_refs,
        ), workflow,
        proposals=job.proposals_json,
    )
    # 先校验本轮裁决的候选类型，再恢复不可由数据库回滚的图检查点。
    resolve_saved_decisions(
        job.proposals_json, merged_decisions["items"],
        node_types={code: node.node_type for code, node in _draft_nodes_of_scenario(db, job.scenario_code).items()},
    )
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
        select(func.count()).select_from(ImportIssue).where(
            ImportIssue.import_job_id == job.id,
            ImportIssue.status == "OPEN",
            ImportIssue.category.notin_(["BUSINESS_AMBIGUITY", "BUSINESS_CONFIRMATION"]),
        )
    )
    job.status = "CONFIRMED" if unresolved == 0 else "PROPOSED"
    db.commit()
    return job


def _expand_report_resolutions(
    sections: dict[str, dict[str, Any]], submissions: list[dict[str, Any]],
    *, excluded_report_refs: set[str],
) -> list[dict[str, Any]]:
    """同批报告共享属性应用于全部同编号章节；冲突答案明确失败，章节启停不传播。"""
    # 本轮明确修改报告编号时，按修改后的归属同步共享属性，不把它传回原报告。
    sections = {ref: dict(section) for ref, section in sections.items()}
    for item in submissions:
        if item["target_type"] == "REPORT_SECTION" and item["action"] == "RESOLVE" and "report_code" in item["value"]:
            sections[item["target_ref"]]["report_code"] = item["value"]["report_code"]
    others = [item for item in submissions if item["target_type"] != "REPORT_SECTION"]
    reports: dict[str, dict[str, Any]] = {}

    def merge(target_ref: str, action: str, values: dict[str, Any]) -> None:
        existing = reports.get(target_ref)
        if existing is None:
            reports[target_ref] = {
                "target_type": "REPORT_SECTION", "target_ref": target_ref,
                "action": action, "value": dict(values),
            }
            return
        conflicts = [key for key, value in values.items() if key in existing["value"] and existing["value"][key] != value]
        if existing["action"] != action or conflicts:
            raise BusinessError(
                "IMPORT_ISSUE_RESOLUTION_CONFLICT", "同一报告的共享属性或章节裁决相互冲突",
                details={"target_ref": target_ref, "report_code": sections[target_ref].get("report_code"),
                         "conflicts": conflicts},
            )
        existing["value"].update(values)

    for item in submissions:
        if item["target_type"] != "REPORT_SECTION":
            continue
        target_ref = item["target_ref"]
        merge(target_ref, item["action"], {})
        if item["action"] != "RESOLVE":
            merge(target_ref, item["action"], item["value"])
            continue
        for property_name, value in item["value"].items():
            value = normalize_resolution_value("REPORT_SECTION", property_name, value)
            for ref in report_property_target_refs(sections, target_ref, property_name):
                # 排除状态只由该章节自己的显式裁决改变，不能被其它章节的答案恢复。
                if ref in excluded_report_refs and ref != target_ref:
                    continue
                merge(ref, "RESOLVE", {property_name: value})
    return [*others, *reports.values()]


def _merge_decision_items(
    previous: dict[str, Any] | None,
    submissions: list[dict[str, Any]],
    workflow: Any,
    *,
    proposals: dict[str, Any],
) -> dict[str, Any]:
    """按目标合并裁决：RESOLVE 按属性更新，其他动作替换目标状态。

    直接裁决与问题裁决可能分多批提交，整体替换会把此前批次的非 ISSUE 直接裁决丢掉。
    计算的候选引用与业务编号先统一为候选引用；同一候选分批修改不同属性时保留先前属性。
    parameters 是完整的业务参数对象，按属性整体替换；同一轮的参数问题已在上游汇总。
    被覆盖的旧值连同替换时间写入 history，不静默丢弃。
    直接裁决的属性白名单仍由 manifest.RESOLVABLE_PROPERTIES 在编译期统一校验。
    """
    payload = dict(previous) if isinstance(previous, dict) else {"items": []}
    aliases = calculation_decision_aliases(proposals)
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    saved = [item for item in payload.get("items", [])
             if isinstance(item, dict) and item.get("target_type") and item.get("target_ref")]
    history = [item for item in payload.get("history", []) if isinstance(item, dict)]
    replaced_at = datetime.now(UTC).isoformat()
    for raw in [*saved, *submissions]:
        target_ref = raw["target_ref"]
        if raw["target_type"] == "CALC_RULE":
            target_ref = aliases.get(target_ref, target_ref)
        item = {**raw, "target_ref": target_ref}
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
    # 计算规则候选的定义、参数和判定来源可跨表，因此挂在 bundle 顶层，
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


def _validated_bound_sheets(
    db: Session, job: ImportJob, template: ScenarioTemplate, sheets: list[ImportSheet],
) -> list[dict[str, Any]]:
    """核对选入表与候选来源，复核裁决后保留候选的节点仍然有效。"""
    selected = [{"sheet_key": item.sheet_key, "sheet_name": item.sheet_name,
                 "sheet_role": "RULE_LIBRARY:" + item.sheet_key, "node_code": None}
                for item in sheets if item.status == "BOUND"]
    keys = {item["sheet_key"] for item in selected}
    if keys != {item["sheet_key"] for item in job.proposals_json["sheets"]}:
        raise BusinessError("IMPORT_SHEET_SELECTION_CHANGED", "参与的工作表已改变，请重新解析", status_code=409)
    decisions = normalize_decision_targets(job.proposals_json, (job.decisions_json or {}).get("items", []))
    nodes = _draft_nodes_of_scenario(db, template.scenario_code)
    effective = resolve_saved_decisions(
        job.proposals_json, decisions,
        node_types={code: node.node_type for code, node in nodes.items()},
    )
    excluded_sheets, excluded_refs = excluded_targets(decisions)
    active_sheets = [sheet for sheet in effective["sheets"] if sheet["sheet_key"] not in excluded_sheets]
    candidates = [
        *[rule for sheet in active_sheets for rule in sheet["rules"]
          if ("RULE", rule["candidate_ref"]) not in excluded_refs],
        *[{**field, "node_type": "FIELD_EXTRACTION"}
          for sheet in active_sheets for field in sheet["fields"]
          if ("FIELD", field["candidate_ref"]) not in excluded_refs],
        *[item for item in effective["calculations"]
          if item["source"]["sheet_key"] not in excluded_sheets
          and ("CALC_RULE", item["candidate_ref"]) not in excluded_refs],
    ]
    for candidate in candidates:
        code = candidate.get("node") or candidate["node_code"]
        node = nodes.get(code)
        disabled_rule = candidate.get("config_status") == "DISABLED" and "operator" in candidate
        if node is None or node.node_type != candidate["node_type"] or (not node.enabled and not disabled_rule):
            raise BusinessError("IMPORT_NODE_CHANGED", "配置引用的节点已删除、停用或改变类型，请重新解析", status_code=409, details={"node_code": code})
    if not selected:
        raise BusinessError("SCENARIO_SHEET_REQUIRED", "本次没有选择工作表", status_code=409)
    return selected




def apply_import_to_draft(
    db: Session, job_no: str, *, expected_job_revision: int, expected_scene_revision: int
) -> ImportJob:
    """把已确认的导入结果写入场景草稿，不生成或发布场景版本。

    不创建场景：场景身份以上传时记录的 scenario_code 为准，不从文件名、Sheet 名或表头另找。
    写入会替换该场景当前草稿里的材料、字段、规则、报告、选用 Sheet 与计算候选；节点实例保留
    场景草稿，不会按 Excel 另造一套流程。

    写草稿前重新核对所选工作表及候选引用的实际节点（`_validated_bound_sheets`）；
    解析后节点可能被删除、停用或改类型。任一校验失败时整次拒绝，不覆盖原草稿。
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
    sheets = _ordered_sheets_of(db, job.id)
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
            node_types={code: node.node_type for code, node in _draft_nodes_of_scenario(db, job.scenario_code).items()},
        )
        load_import_config_into_draft(db, draft, config, profile_version=job.profile_version)
        draft.config_json = {**draft.config_json, "import_proposals": job.proposals_json,
                             "import_job_no": job.job_no}
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

    只读行为：本函数不修改作业状态、不写任何状态机，也不产生新版本。
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
