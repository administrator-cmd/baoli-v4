import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..repositories import configuration_store as config_store

from ..config import get_settings
from ..domain.manifest import (
    draft_config_of_import,
    import_origin_of,
    rule_comparison_view,
)
from ..domain.proposals import ProposalBundle
from ..domain.workbook import WorkbookIR
from ..common.exceptions import BusinessError
from ..graphs.import_graph import (
    run_sheet_conversion,
    run_workbook_read,
)
from ..entity import FieldCandidate, ImportIssue, ImportJob, ImportSheet
from ..entity import (
    BackgroundOperation,
    ScenarioDraft,
    ScenarioTemplate,
    ScenarioVersion,
)
from ..schema.dto import SheetBindingsRequest
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
            # 记录当前执行契约版本，不约束上传文件的业务表头。
            profile_version=workbook_ir.profile_version,
            # 上传入口的场景身份：作业明确属于哪个场景，不从 Excel 内容猜。
            scenario_code=scenario_code,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
            schema_json=schema,
            profile_display_version=node_profile.NODE_PROFILE_VERSION,
            status="PARSED",
            workbook_ir_json=workbook_ir.model_dump(mode="json"),
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
) -> dict[str, Any] | None:
    """直接展示本次模型解析候选与真实来源，不叠加业务裁决。"""
    proposals = job.proposals_json
    if not isinstance(proposals, dict):
        return None
    effective = proposals
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
                    "operator": item["operator"],
                    "config_status": item.get("config_status"),
                    "original": _origin_view(origin_rules.get(item["candidate_ref"]), ("name", "operator")),
                    "comparison": comparison,
                    "source_sheet": sheet_name,
                    "source_row": item["source"]["row_no"],
                    "source_cells": item["source"]["cells"],
                }
            )
        sheets.append(
            {
                "sheet_key": sheet_key,
                "sheet_name": sheet_name,
                "suggested_role": sheet.get("suggested_role"),
                "materials": [
                    {
                        "candidate_ref": item["candidate_ref"],
                        "name": item["name"],
                        "required": item["required"],
                        # 材料业务说明随候选一起展示和发布，不是无用备注。
                        "description": item.get("description", ""),
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
        calculations.append(
            {
                "candidate_ref": candidate_ref,
                "rule_code": rule_code,
                "rule_name": item["rule_name"],
                "formula": item["formula"],
                "requirement": item.get("requirement", ""),
                "business_context": item.get("business_context", {}),
                "result_code": item["result_code"],
                "result_type": item["result_type"],
                "status": item["status"],
                "parameters": item["parameters"],
                "source_sheet": sheet_name,
                "source_row": source["row_no"],
                "source_cells": source["cells"],
                "origin": import_origin_of(original, sheet_names=sheet_names, import_job_no=job.job_no),
                "parameter_sources": nested_calc_sources(
                    item["parameters"], original.get("parameters") or [], "parameter_code"
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
    """返回执行契约版本与作业所属场景身份。"""
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
    issues = db.scalars(select(ImportIssue).where(
        ImportIssue.import_job_id == job.id,
    ).order_by(ImportIssue.id)).all()
    sheet_names = {item.sheet_key: item.sheet_name for item in sheets}
    return {
        "job_no": job.job_no,
        "source_filename": job.source_filename,
        "status": job.status,
        "profile_version": job.profile_version,
        "schema_key": job.schema_key,
        "schema_version": job.schema_version,
        "schema_json": _job_schema(job),
        # 作业所属场景及执行契约摘要；解析范围来自实际工作表选择。
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
            }
            for item in issues
        ],
        "proposals": _proposal_business_view(
            job, sheet_names,
        ),
        "manifest_draft": _manifest_business_view(job),
        # 每条来源业务记录的处置（候选 / 明确排除 / 待处理问题）。
        "coverage": job.coverage_json,
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
    job.manifest_draft_json = None
    job.coverage_json = None
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1


def retry_conversion(db: Session, job_no: str, *, operation_no: str) -> ImportJob:
    """读取输入后释放事务，模型返回时再锁定作业并提交结果。"""
    job = _conversion_job_for_write(db, job_no, operation_no)
    sheets = _ordered_sheets_of(db, job.id)
    bindings = {item.sheet_key: {"excluded": item.status == "EXCLUDED"} for item in sheets}
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
        job.status = "PROPOSED"
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














def _validated_bound_sheets(
    db: Session, job: ImportJob, template: ScenarioTemplate, sheets: list[ImportSheet],
) -> list[dict[str, Any]]:
    """只核对本次选入的工作表与解析来源，执行不依赖旧节点配置。"""
    selected = [{"sheet_key": item.sheet_key, "sheet_name": item.sheet_name,
                 "sheet_role": "RULE_LIBRARY:" + item.sheet_key, "node_code": None}
                for item in sheets if item.status == "BOUND"]
    if {item["sheet_key"] for item in selected} != {item["sheet_key"] for item in job.proposals_json["sheets"]}:
        raise BusinessError("IMPORT_SHEET_SELECTION_CHANGED", "参与的工作表已改变，请重新解析", status_code=409)
    if not selected:
        raise BusinessError("SCENARIO_SHEET_REQUIRED", "本次没有选择工作表", status_code=409)
    return selected




def apply_import_to_draft(
    db: Session, job_no: str, *, expected_job_revision: int, expected_scene_revision: int
) -> ImportJob:
    """把解析结果写入场景草稿，不生成或发布场景版本。

    不创建场景：场景身份以上传时记录的 scenario_code 为准，不从文件名、Sheet 名或表头另找。
    写入替换材料、字段、规则、报告、工作表与计算定义。
    写入前核对工作表选择和草稿修订，执行时由 LLM 按生效规则处理。
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
    if job.status not in {"PROPOSED"}:
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
            namespace=template.scenario_code,
            selected_sheets=selected_sheets,
            import_job_no=job.job_no,
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
