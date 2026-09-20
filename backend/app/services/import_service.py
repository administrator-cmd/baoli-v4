import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import capabilities
from ..domain.manifest import (
    NODE_DEFINITIONS,
    RESOLVABLE_PROPERTIES,
    excluded_targets,
    resolved_targets,
    resolve_saved_decisions,
    rule_comparison_view,
)
from ..domain.proposals import ProposalBundle
from ..domain.workbook import WorkbookIR
from ..errors import BusinessError
from ..graphs.import_graph import (
    import_decision_signature,
    node_note_target_ref,
    resume_import_decision,
    run_manifest_compile,
    run_sheet_conversion,
    run_workbook_read,
)
from ..models import (
    BackgroundOperation,
    FieldCandidate,
    ImportIssue,
    ImportJob,
    ImportSheet,
    ScenarioMaterial,
    ScenarioNode,
    ScenarioRule,
    ScenarioVersion,
)
from ..schemas import ImportDecisionsRequest, PrecheckBinding, PublishRequest, SheetBindingsRequest
from .background_service import ACTIVE_STATUSES, assert_no_active_operation, latest_operation, operation_view
from ..domain.identity import public_no
from .operation_types import OBJECT_IMPORT_JOB
from .operation_log_service import record_operation
from ..domain.profile import load_rulebook_profile, role_index
from .storage_service import RULEBOOK_EXTENSIONS, save_upload
from .template_registry_service import allocate_version, ensure_template


NODE_LABELS = dict(NODE_DEFINITIONS)

# 预检查绑定的字段名直接取自 schemas.PrecheckBinding，避免两处各写一份导致漂移。
PRECHECK_BINDING_KEYS = tuple(PrecheckBinding.model_fields)


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


def _ensure_import_mutable(db: Session, job: ImportJob) -> None:
    published_version_id = db.scalar(
        select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == job.id).limit(1)
    )
    if job.status == "PUBLISHED" or published_version_id is not None:
        raise BusinessError(
            "IMPORT_ALREADY_PUBLISHED",
            "该导入作业已发布，不能再修改或重新编译",
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
    return sheet.snapshot_json.get("suggested_role") == "INSTRUCTION"


async def create_import_job(db: Session, upload: UploadFile) -> ImportJob:
    settings = get_settings()
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
        )
        workbook_ir = WorkbookIR.model_validate(graph_state["workbook_ir"])
        job = ImportJob(
            job_no=job_no,
            source_filename=upload.filename or f"规则库{suffix}",
            source_path=relative_path,
            source_sha256=sha256,
            source_size=size,
            # 格式版本以这份工作簿的表头签名识别结果为准，不按当前默认版本假定。
            profile_version=workbook_ir.profile_version,
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
        for sheet in workbook_ir.sheets:
            instruction = sheet.suggested_role == "INSTRUCTION"
            business_role = None if instruction or not sheet.visible else sheet.suggested_role
            # 只有表头、没有业务数据行的业务表必须保留角色，继续参与转换并产生零行业务集合。
            # 把它标成 NO_BUSINESS_DATA 再在预处理里排除，会让空白模板既进不了转换、
            # 又被报“缺少七张表”——两处口径自相矛盾。
            # 真正的 EMPTY（连表头都没有）无法确定列，仍按无业务数据处理。
            if instruction:
                status = "EXCLUDED"
            elif business_role and sheet.state == "NO_BUSINESS_DATA":
                status = "PARSED"
            elif sheet.state in {"EMPTY", "NO_BUSINESS_DATA"}:
                status = sheet.state
            else:
                status = "PARSED"
            db.add(
                ImportSheet(
                    import_job_id=job.id,
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    visible=sheet.visible,
                    status="HIDDEN" if not sheet.visible and status == "PARSED" else status,
                    bound_role=business_role,
                    header_row=sheet.header_candidates[0] if sheet.header_candidates else None,
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
    rule_names = {
        rule["candidate_ref"]: rule["name"]
        for sheet in effective.get("sheets", [])
        for rule in sheet.get("rules", [])
    }
    # 有效字段的值类型与单位：规则比较视图据此展示比较双方的口径与单位，不做任何换算猜测。
    field_meta = {
        field["candidate_ref"]: {"value_type": field["value_type"], "unit": field.get("unit")}
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
                        "material_type": item["material_type"],
                        "required": item["required"],
                        # 材料业务说明随候选一起展示和发布，不是无用备注。
                        "description": item.get("description", ""),
                        "decision_state": decision_state("MATERIAL", item["candidate_ref"], sheet_key),
                        "original": _origin_view(
                            origin_materials.get(item["candidate_ref"]),
                            ("name", "material_type", "required", "description"),
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
                        "required": item["required"],
                        "instruction": item["instruction"],
                        "material_name": material_names.get(item["material_ref"]),
                        "decision_state": decision_state("FIELD", item["candidate_ref"], sheet_key),
                        "original": _origin_view(
                            origin_fields.get(item["candidate_ref"]),
                            ("name", "value_type", "unit", "required", "instruction"),
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
    return {"sheets": sheets}


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


def _precheck_view(job: ImportJob) -> dict[str, Any] | None:
    """把落库的发布检查绑定还原成页面可恢复的预检查结果。

    绑定记录是发布与预检查一致性的唯一依据，页面刷新后必须原样看到它，
    不能凭当前草稿重新拼一个“看起来通过”的预检查。
    """
    payload = job.precheck_json
    if not isinstance(payload, dict):
        return None
    binding = {key: payload.get(key) for key in PRECHECK_BINDING_KEYS}
    return {
        "ready": bool(payload.get("ready", False)),
        "blockers": payload.get("blockers", []),
        "summary": payload.get("summary", {}),
        "binding": binding,
        "capability_gaps": payload.get("capability_gaps", []),
    }


def import_job_view(db: Session, job: ImportJob) -> dict[str, Any]:
    sheets = db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id).order_by(ImportSheet.id)).all()
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
    return {
        "job_no": job.job_no,
        "source_filename": job.source_filename,
        "status": job.status,
        "profile_version": job.profile_version,
        "profile_display_name": load_rulebook_profile(job.profile_version).get("display_name") or job.profile_version,
        "draft_revision": job.draft_revision,
        "sheets": [
            {
                "sheet_key": item.sheet_key,
                "name": item.sheet_name,
                "visible": item.visible,
                "status": item.status,
                # 固定排除只用于模板元数据（填写说明、隐藏元数据页）。
                # 「只有表头没有业务行」的业务表仍然带角色参与转换（产生零行业务集合），
                # 不是固定排除；把它标成固定排除会让页面显示成“已跳过”，与转换实际行为不符。
                "fixed_excluded": _is_template_metadata_sheet(item),
                "role": item.bound_role,
                "header_row": item.header_row,
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
                # WorkbookIR keeps full source facts internally. The workbench receives
                # only the bounded preview cells and never the raw cell_details payload.
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
        "precheck": _precheck_view(job),
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
    job = _job_or_404(db, job_no, lock=True)
    # 改绑定与后台转换互斥：转换进行中不能改动工作表用途（锁后检查，避免竞态）。
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    sheets = {item.sheet_key: item for item in db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id))}
    known_roles = set(role_index(job.profile_version))
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
            if not binding.excluded or binding.role is not None or binding.header_row is not None:
                raise BusinessError(
                    "TEMPLATE_METADATA_IMMUTABLE",
                    "填写说明和隐藏模板元数据不能绑定为业务工作表",
                    status_code=409,
                    details={"sheet_name": sheet.sheet_name},
                )
            sheet.status = (
                "EXCLUDED" if sheet.snapshot_json.get("suggested_role") == "INSTRUCTION" else "HIDDEN"
            )
            sheet.bound_role = None
            sheet.header_row = None
            continue
        if binding.excluded:
            sheet.status = "EXCLUDED"
            sheet.bound_role = None
            continue
        if binding.role is None:
            raise BusinessError(
                "SHEET_ROLE_REQUIRED",
                "业务工作表必须选择用途或排除该工作表",
                details={"sheet_name": sheet.sheet_name},
            )
        if binding.role not in known_roles:
            raise BusinessError("SHEET_ROLE_INVALID", "工作表用途不受支持", details={"role": binding.role})
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
        sheet.header_row = binding.header_row
        sheet.status = "BOUND"
    db.execute(delete(FieldCandidate).where(FieldCandidate.import_job_id == job.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_job_id == job.id))
    job.proposals_json = None
    job.decisions_json = None
    job.manifest_draft_json = None
    # 绑定变化会让此前的发布检查失效，不能让它继续被当成有效预检查使用。
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1
    job.status = "PARSED"
    job.failure_code = None
    job.failure_message = None
    db.commit()
    return job


def retry_conversion(db: Session, job_no: str) -> ImportJob:
    job = _job_or_404(db, job_no, lock=True)
    _ensure_import_mutable(db, job)
    sheets = db.scalars(select(ImportSheet).where(ImportSheet.import_job_id == job.id)).all()
    bindings: dict[str, dict[str, Any]] = {}
    for item in sheets:
        if _is_template_metadata_sheet(item):
            if item.status != "EXCLUDED" or item.bound_role is not None:
                raise BusinessError(
                    "TEMPLATE_METADATA_IMMUTABLE",
                    "填写说明和隐藏模板元数据不能绑定为业务工作表",
                    status_code=409,
                    details={"sheet_name": item.sheet_name},
                )
            bindings[item.sheet_key] = {
                "role": None,
                "header_row": None,
                "excluded": True,
                "hidden": not item.visible,
            }
            continue
        bindings[item.sheet_key] = {
            "role": item.bound_role,
            "header_row": item.header_row,
            "excluded": item.status == "EXCLUDED",
            "hidden": not item.visible,
        }
    db.execute(delete(FieldCandidate).where(FieldCandidate.import_job_id == job.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_job_id == job.id))
    job.proposals_json = None
    job.decisions_json = None
    job.manifest_draft_json = None
    job.coverage_json = None
    # 重新转换会整体替换草稿配置，旧发布检查绑定随之失效。
    job.precheck_json = None
    job.draft_revision = job.draft_revision + 1
    try:
        checkpoint_ns = f"import:conversion:{public_no('CKPT')}"
        graph_state = run_sheet_conversion(
            job_no=job.job_no,
            checkpoint_ns=checkpoint_ns,
            workbook_ir=job.workbook_ir_json,
            bindings=bindings,
        )
        bundle = ProposalBundle.model_validate(graph_state["proposals"])
        proposal_json = bundle.model_dump(mode="json")
        coverage_json = _bundle_coverage(bundle)
        node_notes = [note for note in (graph_state.get("node_notes") or []) if isinstance(note, dict)]
        # 可作答的待确认事项：每条进入裁决集合，与 import_graph._node_note_targets 的取舍一致。
        resolvable_notes = [note for note in node_notes if note.get("resolve")]
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
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise BusinessError("IMPORT_ISSUE_VALUE_INVALID", "该问题需要填写数值", details={"issue_no": issue_no}) from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise BusinessError(
                "IMPORT_ISSUE_VALUE_INVALID",
                "该问题需要填写不小于 0 的有限数值",
                details={"issue_no": issue_no},
            )
        return numeric
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
    for issue_no in open_issue_nos:
        source = issue_refs[issue_no].source_json
        if not isinstance(source, dict):
            raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "待裁决问题缺少来源信息")
        pending_question_sources.append(source)
    pending_signature = import_decision_signature(pending_question_sources) if pending_question_sources else None
    submitted_issue_nos = {item.target_ref for item in request.decisions if item.target_type == "ISSUE"}
    if submitted_issue_nos != open_issue_nos:
        raise BusinessError("IMPORT_ISSUE_DECISIONS_INCOMPLETE", "必须一次处理当前全部待裁决问题")
    direct_decisions: list[dict[str, Any]] = []
    resolved_decisions: list[dict[str, Any]] = []
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
                target_type not in {"MATERIAL", "FIELD", "RULE", "REPORT_SECTION"}
                or not target_ref
                or not property_name
            ):
                raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "歧义问题缺少可裁决的业务对象")
            if target_ref not in proposal_refs[target_type]:
                raise BusinessError("IMPORT_ISSUE_TARGET_INVALID", "歧义问题引用的业务对象不存在")
            resolved_decisions.append(
                {
                    "target_type": target_type,
                    "target_ref": target_ref,
                    "action": "RESOLVE",
                    "value": {property_name: resolved_value},
                }
            )
            issue_updates.append((issue, decision["value"]))
        else:
            if decision["target_ref"] not in proposal_refs[decision["target_type"]]:
                raise BusinessError("IMPORT_DECISION_TARGET_NOT_FOUND", "裁决引用的业务对象不存在", details={"target_ref": decision["target_ref"]})
            if decision["action"] == "RESOLVE":
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
    if pending_question_sources:
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
    for issue, decision_value in issue_updates:
        issue.status = "RESOLVED"
        issue.decision_json = decision_value
    job.decisions_json = _merge_decision_items(job.decisions_json, [*direct_decisions, *resolved_decisions], workflow)
    # 任何裁决都会改变有效配置：此前的发布检查绑定随之失效，必须重新预检查。
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
    """按目标合并裁决：同一目标后写覆盖先写，被覆盖的旧条目进入 history。

    直接裁决与问题裁决可能分多批提交，整体替换会把上一轮的非 ISSUE 直接裁决丢掉。
    合并键是 (target_type, target_ref)；被覆盖的旧值连同替换时间写入 history，不静默丢弃。
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
        if replaced is not None and replaced != item:
            history.append({"replaced_at": replaced_at, "previous": replaced, "replacement": item})
        merged[key] = item
    payload["items"] = list(merged.values())
    payload["history"] = history
    if isinstance(workflow, dict):
        payload["workflow"] = workflow
    else:
        payload.pop("workflow", None)
    return payload


def save_decision_draft(db: Session, job_no: str, answers: dict[str, Any]) -> ImportJob:
    """暂存业务问题的作答，不改变候选、问题状态与草稿修订。

    暂存只是让页面在提交前能保留用户已填的内容，因此不递增 draft_revision，
    也不清空已完成的预检查；真正的生效仍要经过 save_decisions。
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
    result = {"MATERIAL": set(), "FIELD": set(), "RULE": set(), "REPORT_SECTION": set(), "SHEET": set()}
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


def _compile_precheck(
    db: Session, job: ImportJob, request: PublishRequest
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int, dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if job.status not in {"PROPOSED", "PENDING_DECISION", "CONFIRMED", "PRECHECKED"}:
        blockers.append({"code": "IMPORT_STATUS_NOT_READY", "message": "导入作业尚未成功完成规则转换"})
    if not job.proposals_json:
        blockers.append({"code": "PROPOSALS_MISSING", "message": "规则尚未转换"})
    unresolved = db.scalar(
        select(func.count()).select_from(ImportIssue).where(ImportIssue.import_job_id == job.id, ImportIssue.status == "OPEN")
    )
    if unresolved:
        blockers.append({"code": "ISSUES_UNRESOLVED", "message": f"仍有 {unresolved} 个问题未处理"})
    manifest: dict[str, Any] | None = None
    coverage: dict[str, Any] = {}
    if not blockers:
        try:
            checkpoint_ns = f"import:compile:{public_no('CKPT')}"
            graph_state = run_manifest_compile(
                job_no=job.job_no,
                checkpoint_ns=checkpoint_ns,
                workbook_ir=job.workbook_ir_json,
                proposals=job.proposals_json or {},
                decisions=(job.decisions_json or {}).get("items", []),
                scene_code=_publish_scene_code(db, job, request),
                scene_name=request.scenario_name,
                namespace=job.job_no,
                compiled_at=job.created_at,
            )
            manifest = graph_state.get("manifest")
            if not isinstance(manifest, dict):
                raise BusinessError("IMPORT_GRAPH_STATE_INVALID", "导入图未返回场景编译结果", status_code=500)
            # 能力覆盖是发布检查的一部分：引用了目录里不存在的能力时在这里明确失败，
            # 不会拖到运行时才发现「没有 handler」。
            coverage = manifest.get("capability_coverage") or capabilities.coverage_of(manifest.get("rules", []))
            blockers.extend(_capability_blockers(coverage, request.acknowledged_capability_gaps))
            decision_payload = dict(job.decisions_json or {"items": []})
            workflow = decision_payload.get("workflow")
            workflow_metadata = dict(workflow) if isinstance(workflow, dict) else {}
            workflow_metadata["compile_checkpoint"] = {
                "checkpoint_ns": checkpoint_ns,
                "status": "COMPLETED",
                "stage_history": graph_state.get("stage_history", []),
            }
            decision_payload["workflow"] = workflow_metadata
            job.decisions_json = decision_payload
        except BusinessError as exc:
            blockers.append({"code": exc.code, "message": exc.message, "details": exc.details})
    return manifest, blockers, int(unresolved or 0), coverage


def _precheck_record(job: ImportJob, request: PublishRequest, manifest: dict[str, Any]) -> dict[str, Any]:
    """落库的发布检查记录：绑定 + 结果摘要。

    绑定字段名与 schemas.PrecheckBinding 保持一致；scene_code 与 manifest_hash 是本记录的
    核心，发布时据此判断“这次发布用的还是不是同一次预检查”。
    """
    return {
        "job_no": job.job_no,
        "profile_version": job.profile_version,
        "draft_revision": job.draft_revision,
        "manifest_hash": manifest["manifest_hash"],
        "template_mode": request.template_mode,
        "target_scenario_code": request.scenario_code if request.template_mode == "update" else None,
        "target_scenario_name": request.scenario_name,
        "scene_code": manifest["scene_code"],
        "scene_name": manifest["scene_name"],
        "checked_at": datetime.now(UTC).isoformat(),
        "ready": True,
        "blockers": [],
        "summary": {
            "materials": len(manifest.get("materials", [])),
            "fields": len(manifest.get("fields", [])),
            "rules": len(manifest.get("rules", [])),
        },
        "capability_gaps": manifest.get("capability_gaps", []),
        # 预检查时逐条确认过的缺口：它属于发布意图的一部分，发布必须与之一致。
        "acknowledged_capability_gaps": sorted(set(request.acknowledged_capability_gaps)),
    }


# ---------------------------------------------------------------------------
# 能力覆盖
#
# 「配置可保存」不等于「规则可执行」。覆盖结果由 domain.capabilities 计算，
# 并随已发布版本一起冻结进 manifest（参与 manifest_hash）；
# 这里只负责把「未实现」的覆盖项变成发布检查的阻断项。
# ---------------------------------------------------------------------------


def _capability_blockers(
    coverage: dict[str, Any],
    acknowledged: list[str],
) -> list[dict[str, Any]]:
    """能力缺口默认阻断发布；只有被逐条确认的规则键才放行。

    确认过的缺口紧随版本冻结（见 manifest.capability_gaps 与 manifest.capability_coverage），
    报告与任务范围里继续可见；未列出的缺口继续阻断。
    确认**不是**「忽略全部缺口」的开关：它逐条列出使用者已看过并接受的规则键。
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
        # 确认集合里出现本次并不存在的缺口：说明确认基于另一份配置，必须报出而不是忽略。
        blockers.append(
            {
                "code": "CAPABILITY_ACKNOWLEDGEMENT_STALE",
                "message": "确认的能力缺口与本次配置不一致，请重新执行发布检查后再确认",
                "details": {"rule_keys": stale},
            }
        )
    return blockers


def precheck_import(db: Session, job_no: str, request: PublishRequest) -> tuple[ImportJob, dict[str, Any]]:
    job = _job_or_404(db, job_no, lock=True)
    # 预检查与后台转换互斥：转换进行中不能基于半成品配置生成发布检查。
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    manifest, blockers, unresolved, coverage = _compile_precheck(db, job, request)
    if manifest:
        job.manifest_draft_json = manifest
        # 覆盖结果随绑定一起落库：刷新后仍能看到这次预检查覆盖到哪些能力，
        # 不会按当前草稿重新拼一个「看起来通过」的结果。
        job.precheck_json = {
            **_precheck_record(job, request, manifest),
            "ready": not blockers,
            "blockers": blockers,
            "capability_coverage": coverage,
        }
        # 有阻断项时不能停在 PRECHECKED：那是「发布已可执行」的状态，
        # 会让下一次发布请求绕过这里重新算出的阻断项。
        job.status = "PRECHECKED" if not blockers else ("PENDING_DECISION" if unresolved else "CONFIRMED")
    else:
        # 预检查失败意味着没有可发布的检查记录；旧绑定必须作废，否则会被当成有效预检查。
        job.precheck_json = None
        if job.status == "PRECHECKED":
            job.status = "PENDING_DECISION" if unresolved else "CONFIRMED"
    db.commit()
    summary = {
        "materials": len(manifest["materials"]) if manifest else 0,
        "fields": len(manifest["fields"]) if manifest else 0,
        "rules": len(manifest["rules"]) if manifest else 0,
        "issues": unresolved,
    }
    unacknowledged = [
        item["details"]["rule_key"]
        for item in blockers
        if item.get("code") == "CAPABILITY_UNIMPLEMENTED" and isinstance(item.get("details"), dict)
    ]
    result: dict[str, Any] = {
        "ready": not blockers,
        "blockers": blockers,
        "summary": summary,
        # 只有真正生成绑定时才把它回给页面；失败时不返回一个“看起来有效”的绑定。
        "binding": job.precheck_json,
        "capability_gaps": manifest.get("capability_gaps", []) if manifest else [],
        "unacknowledged_capability_gaps": unacknowledged,
        "capability_coverage": coverage,
    }
    return job, result


def _publish_scene_code(db: Session, job: ImportJob, request: PublishRequest) -> str:
    """确定本次编译/发布使用的模板身份，且不跨发布意图复用代码。

    update 直接使用请求里显式选择的模板身份，并校验它真实存在；
    new 必须分配一个新的 scene_code，绝不复用草稿里属于别次 update 意图的代码，
    但同一作业同一 new 意图重算时要保持稳定：复用 precheck_json 里记录的 scene_code。
    """
    if request.template_mode == "update":
        if not request.scenario_code:
            raise BusinessError("TEMPLATE_CODE_REQUIRED", "更新已有模板时必须选择要更新的审核模板")
        # 更新必须落到真实存在的模板身份上，不能凭中文名隐式新建。
        ensure_template(db, request.scenario_code, request.scenario_name)
        return request.scenario_code
    precheck = job.precheck_json if isinstance(job.precheck_json, dict) else None
    if precheck and precheck.get("template_mode") == "new" and precheck.get("target_scenario_code") is None:
        reused = precheck.get("scene_code")
        if isinstance(reused, str) and reused:
            return reused
    return public_no("SCN")


def _assert_publish_intent(job: ImportJob, request: PublishRequest, binding: dict[str, Any]) -> None:
    """核对本次发布请求与预检查时的意图是否一致，并指出变了哪一项。

    逐条确认的能力缺口同样属于发布意图：换了确认集合（无论增删）就是换了发布意图，
    不能借同一次预检查放行——否则一次「已确认某项不可执行」的预检查会被拿来发布
    另一份配置。
    """
    expected_target = request.scenario_code if request.template_mode == "update" else None
    changed: list[str] = []
    if binding.get("template_mode") != request.template_mode:
        changed.append("发布方式")
    if binding.get("target_scenario_code") != expected_target:
        changed.append("目标模板")
    if binding.get("target_scenario_name") != request.scenario_name:
        changed.append("模板名称")
    if binding.get("draft_revision") != job.draft_revision:
        changed.append("草稿修订")
    recorded_ack = sorted(set(binding.get("acknowledged_capability_gaps") or []))
    if recorded_ack != sorted(set(request.acknowledged_capability_gaps)):
        changed.append("已确认的能力缺口")
    if changed:
        raise BusinessError(
            "PUBLISH_PREVIEW_MISMATCH",
            f"发布内容与刚才的预检查结果不一致（{'、'.join(changed)}已变化），请重新预检查后再发布",
            status_code=409,
            details={"changed": changed},
        )


def _require_publish_binding(job: ImportJob, request: PublishRequest) -> dict[str, Any]:
    """发布必须建立在本作业一次有效的预检查之上。

    额外核对绑定记录里的 `ready`：预检查存在阻断项时不能发布，
    即使作业状态看起来仍是可发布的。
    """
    binding = job.precheck_json if isinstance(job.precheck_json, dict) else None
    if not binding or not isinstance(binding.get("manifest_hash"), str) or job.status != "PRECHECKED":
        raise BusinessError(
            "PUBLISH_PRECHECK_REQUIRED",
            "发布前必须先执行发布检查；没有有效的预检查结果不能发布",
            status_code=409,
        )
    if not binding.get("ready", False):
        raise BusinessError(
            "PUBLISH_PRECHECK_BLOCKED",
            "上一次发布检查存在阻断项，不能发布；请处理后重新执行发布检查",
            status_code=409,
            details={"blockers": binding.get("blockers", [])},
        )
    _assert_publish_intent(job, request, binding)
    return binding


def publish_import(db: Session, job_no: str, request: PublishRequest) -> ScenarioVersion:
    job = _job_or_404(db, job_no, lock=True)
    existing_for_job = db.scalar(select(ScenarioVersion).where(ScenarioVersion.import_job_id == job.id))
    if existing_for_job:
        # 同一作业重复发布返回已经发布的同一版本，不创建第二份；但必须核对意图一致，
        # 否则不同输入可以借同一个作业伪装成同一次发布。
        binding = job.precheck_json if isinstance(job.precheck_json, dict) else None
        if binding is None:
            raise BusinessError(
                "PUBLISH_PRECHECK_REQUIRED",
                "该作业已发布但缺少发布检查绑定，无法确认重复发布的意图一致",
                status_code=409,
            )
        _assert_publish_intent(job, request, binding)
        return existing_for_job
    # 发布与后台转换互斥：转换进行中不能发布半成品配置。
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    _ensure_import_mutable(db, job)
    binding = _require_publish_binding(job, request)
    manifest, blockers, _, coverage = _compile_precheck(db, job, request)
    if blockers or not manifest:
        raise BusinessError("PUBLISH_PRECHECK_FAILED", "发布检查未通过", details={"blockers": blockers})
    if binding["manifest_hash"] != manifest.get("manifest_hash"):
        raise BusinessError(
            "PUBLISH_PREVIEW_MISMATCH",
            "发布内容与刚才的预检查结果不一致，请重新预检查后再发布",
            status_code=409,
        )
    scene_code = manifest["scene_code"]
    version_no = allocate_version(db, scene_code, request.scenario_name)
    version = ScenarioVersion(
        import_job_id=job.id,
        scenario_code=scene_code,
        scenario_name=request.scenario_name,
        version_no=version_no,
        status="PUBLISHED",
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
    job.status = "PUBLISHED"
    job.manifest_draft_json = manifest
    # 场景配置草稿：导入发布后为尚无草稿的场景建立草稿（来源=EXCEL，基于本次版本），
    # 让页面配置入口直接可用；已有草稿（用户正在编辑的配置）保持不变。
    from .scenario_admin_service import ensure_draft_from_published_version

    ensure_draft_from_published_version(db, version)
    # 发布不改变草稿修订，也保留预检查绑定：页面刷新后仍能还原这次发布用的是哪次预检查。
    db.commit()
    db.refresh(version)
    return version
