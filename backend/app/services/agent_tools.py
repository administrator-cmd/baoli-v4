"""工具 adapter：把共享领域服务接到 Agent 工具接口上。

工具与节点必须共用同一份判定实现（`domain/quality_judgement.py`），但输入来源不同：
节点从 ORM 与已发布 manifest 取数，工具只拿到 `ToolExecutionContext` 与模型给的业务参数。
adapter 的职责就是把后者的形状转成前者的输入，再调同一个共享服务——它不含任何判定逻辑，
一旦出现 `if check_item == ...` 就说明判定被复制了。

模型能填的只有业务口径（要检查的材料角色、检查项目、标准值/单位）；任务 ID、事实快照、
策略版本、会话全部由运行时经 `ToolExecutionContext` 注入，模型看不到也填不了
（schema 里没有这些键，`additionalProperties: false` 会拒绝）。

需要读材料记录的工具先读冻结快照，再用它核对真实记录：快照里的材料位身份是权威版本，
真实记录必须逐项与冻结身份一致（上传引用、内容哈希、材料状态任一项不同即
`TOOL_SNAPSHOT_MISMATCH`，本次不执行），判定取用的取值也取自冻结身份。
只存哈希、执行时仍读「当前数据」的写法会让同一次评估的两次工具调用落到不同的材料版本上，
而指纹对得上只是巧合——所以快照存的是内容，不是只有指纹。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg, tool

from ..domain import quality_judgement
from ..common.exceptions import BusinessError
from ..entity import DocumentUpload, EvaluationSnapshot, TaskDocument
from sqlalchemy import select


@tool("RULE_LIBRARY_QUERY")
def query_rule_library_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Annotated[Any, InjectedToolArg],
) -> dict[str, Any]:
    """查询本次审核任务冻结的生效规则库。

    查询只读任务创建时固化的 manifest：不会读取场景草稿、当前数据库规则或模型自行提供的规则。
    规则由审核图按任务范围与事实快照执行；该工具只负责在需要时返回规则原文和依赖配置。
    """
    if context is None or context.database_session is None or context.task_id is None:
        raise BusinessError("TOOL_CONTEXT_MISSING", "规则库查询需要审核任务上下文", status_code=500)
    from ..entity import AuditTask

    task = context.database_session.get(AuditTask, context.task_id)
    if task is None:
        raise BusinessError("TOOL_TASK_MISSING", "规则库查询关联的审核任务不存在", status_code=409)
    manifest = task.manifest_json
    schema = task.task_schema_json
    if not isinstance(manifest, dict) or not isinstance(manifest.get("rules"), list):
        raise BusinessError("TOOL_RULE_LIBRARY_MISSING", "审核任务没有冻结的规则库", status_code=409)
    if not isinstance(schema, dict) or not isinstance(schema.get("enabled_rule_keys"), list):
        raise BusinessError("TOOL_RULE_SCOPE_MISSING", "审核任务没有冻结的规则启用范围", status_code=409)
    enabled_keys = set(schema["enabled_rule_keys"])
    rules = manifest["rules"]
    query = str(parameters.get("query") or "").strip().casefold()
    kind = str(parameters.get("kind") or "ALL").upper()
    if kind not in {"ALL", "RULE", "CALCULATION"}:
        raise BusinessError("TOOL_ARGUMENTS_INVALID", "规则库查询的 kind 只能是 ALL、RULE 或 CALCULATION")
    requested_codes = {
        str(value).strip().casefold()
        for value in (parameters.get("rule_codes") or [])
        if str(value).strip()
    }
    requested_keys = {
        str(value).strip().casefold()
        for value in (parameters.get("rule_keys") or [])
        if str(value).strip()
    }

    def matches(item: dict[str, Any]) -> bool:
        if item["rule_key"] not in enabled_keys or not item.get("enabled", True):
            return False
        calculation = item.get("kind") == "CALCULATION"
        if (kind == "RULE" and calculation) or (kind == "CALCULATION" and not calculation):
            return False
        codes = {str(item.get(name) or "").strip().casefold() for name in ("rule_code", "result_code")}
        if requested_codes and not codes & requested_codes:
            return False
        if requested_keys and str(item["rule_key"]).casefold() not in requested_keys:
            return False
        return not query or query in json.dumps(item, ensure_ascii=False).casefold()

    items = [{"kind": item.get("kind") or "RULE", "rule_code": item.get("rule_code"),
              "name": item.get("name"), "config": item} for item in rules if matches(item)]
    return {
        "status": "SUCCEEDED",
        "message": f"已从本次任务冻结规则库查询到 {len(items)} 条生效规则",
        "items": items,
        "source": "TASK_FROZEN_MANIFEST",
        "scenario_version_no": schema.get("scenario_version_no"),
        "plan_scope_hash": schema.get("plan_scope_hash"),
        "manifest_hash": manifest.get("manifest_hash"),
    }


def _snapshot_record(context: Any) -> EvaluationSnapshot:
    """本次调用依据的冻结快照。缺失即显式失败——不能退回读当前数据。"""
    if context is None or context.database_session is None:
        raise BusinessError(
            "TOOL_CONTEXT_MISSING",
            "工具调用需要任务上下文与数据库会话，本次调用未提供",
            status_code=500,
        )
    if not context.fact_snapshot_ref:
        raise BusinessError(
            "TOOL_SNAPSHOT_MISSING",
            "工具调用缺少冻结事实快照身份，本次调用未执行",
            status_code=500,
            details={"tool_context": "fact_snapshot_ref"},
        )
    record = context.database_session.scalar(
        select(EvaluationSnapshot).where(
            EvaluationSnapshot.snapshot_ref == context.fact_snapshot_ref,
            EvaluationSnapshot.audit_task_id == context.task_id,
        )
    )
    if record is None:
        raise BusinessError(
            "TOOL_SNAPSHOT_MISSING",
            "工具调用依据的冻结事实快照不存在，本次调用未执行",
            status_code=409,
            details={"snapshot_ref": context.fact_snapshot_ref},
        )
    if record.fact_snapshot_hash != context.fact_snapshot_hash:
        raise BusinessError(
            "TOOL_SNAPSHOT_MISMATCH",
            "工具调用声明的快照指纹与冻结记录不一致，本次调用未执行",
            details={
                "snapshot_ref": record.snapshot_ref,
                "frozen": record.fact_snapshot_hash,
                "declared": context.fact_snapshot_hash,
            },
        )
    return record


def _requested_uploads(context: Any, material_keys: list[str]):
    from ..entity import AuditTask
    task = context.database_session.get(AuditTask, context.task_id)
    if task is None:
        raise BusinessError("TOOL_TASK_MISSING", "工具关联的任务不存在")
    material_scope(context, material_keys=_scope_materials(context, material_keys))
    frozen = {m["material_key"]: m for m in _frozen_materials(context)}
    for key in material_keys:
        identity = frozen[key]
        upload = context.database_session.get(DocumentUpload, identity.get("upload_id")) if identity.get("upload_id") else None
        if upload is None:
            raise BusinessError("TOOL_DOCUMENT_NOT_UPLOADED", "选定材料没有实际上传", details={"material_key": key})
        yield task, key, upload


@tool("TASK_DATA_READ")
def read_task_data_tool(parameters: dict[str, Any], inputs: list[dict[str, Any]], *, context: Annotated[Any, InjectedToolArg]) -> dict[str, Any]:
    """读取冻结上传的完整原文或已生成的真实产物。"""
    from pathlib import Path
    import hashlib
    from openpyxl import load_workbook
    from .storage_service import resolve_storage_path
    from .audit.artifacts import _artifact_for_upload, _artifact_payload
    items = []
    kind = parameters.get("artifact_type") or "ORIGINAL"
    for task, key, upload in _requested_uploads(context, parameters["material_keys"]):
        if kind != "ORIGINAL":
            artifact = _artifact_for_upload(context.database_session, upload, kind)
            if artifact is None or artifact.status != "READY":
                raise BusinessError("TOOL_ARTIFACT_NOT_READY", "请求的材料产物尚未生成", details={"material_key": key, "artifact_type": kind})
            content = _artifact_payload(artifact).content
            items.append({"material_key": key, "upload_id": upload.id, "artifact_id": artifact.id, "artifact_type": kind, "content": content})
            continue
        path = resolve_storage_path(upload.relative_path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != upload.sha256:
            raise BusinessError("TOOL_DOCUMENT_CONTENT_CHANGED", "实际文件内容与冻结上传不一致")
        suffix = Path(upload.original_filename).suffix.lower()
        if suffix in {".xlsx", ".xlsm"}:
            workbook = load_workbook(path, data_only=False, read_only=True)
            cached = load_workbook(path, data_only=True, read_only=True)
            try:
                content = [{"sheet": sheet.title, "cells": [{"cell": cell.coordinate, "value": str(cell.value),
                            "cached_value": str(cached[sheet.title][cell.coordinate].value) if cell.data_type == "f" else None}
                            for row in sheet for cell in row if cell.value is not None]} for sheet in workbook]
            finally:
                workbook.close()
                cached.close()
        elif suffix in {".txt", ".csv", ".json", ".md", ".html", ".xml"}:
            content = path.read_text(encoding="utf-8-sig")
        else:
            raise BusinessError("TOOL_ORIGINAL_FORMAT_UNSUPPORTED", "当前原始文件读取工具无法读取该格式；可显式调用DOCUMENT_PROCESS生成OCR后读取", details={"format": suffix, "material_key": key})
        items.append({"material_key": key, "upload_id": upload.id, "sha256": upload.sha256, "filename": upload.original_filename, "content": content})
    return {"status": "SUCCEEDED", "message": "已读取实际材料内容", "items": items}


@tool("DOCUMENT_PROCESS")
def process_documents_tool(parameters: dict[str, Any], inputs: list[dict[str, Any]], *, context: Annotated[Any, InjectedToolArg]) -> dict[str, Any]:
    """按模型明确选择的服务处理材料，不执行固定处理阶段。"""
    from .storage_service import resolve_storage_path
    from .audit.document_ports import document_service
    from .audit.artifacts import _ensure_external_artifact, _artifact_for_upload, _artifact_payload, _submit_ocr_job, _complete_ocr_job
    service = parameters["service"]
    db = context.database_session
    items = []
    for task, key, upload in _requested_uploads(context, parameters["material_keys"]):
        kwargs = {"file_path": resolve_storage_path(upload.relative_path), "filename": upload.original_filename, "content_type": upload.content_type}
        if service == "OCR":
            parsed = _artifact_for_upload(db, upload, "DOCUMENT_PARSE")
            if parsed is None or parsed.status != "READY":
                raise BusinessError("PROCESSED_ARTIFACT_MISSING", "OCR需要文件处理产物；请先明确调用DOCUMENT_PARSE", details={"material_key": key})
            artifact = _complete_ocr_job(db, task, upload, _submit_ocr_job(db, task, upload, parsed))
        else:
            functions = {"DOCUMENT_PARSE": document_service().process_document, "SEAL": document_service().detect_seals, "MATERIAL_CLASSIFY": document_service().validate_material_type}
            if service == "MATERIAL_CLASSIFY":
                kwargs["material_type"] = upload.declared_material_type
            artifact = _ensure_external_artifact(db, task, upload, service, lambda: functions[service](**kwargs))
        items.append({"material_key": key, "upload_id": upload.id, "artifact_id": artifact.id, "artifact_type": service, "content": _artifact_payload(artifact).content})
    return {"status": "SUCCEEDED", "message": "指定服务已返回实际产物", "items": items}


def _frozen_materials(context: Any) -> list[dict[str, Any]]:
    record = _snapshot_record(context)
    payload = record.facts_json if isinstance(record.facts_json, dict) else {}
    materials = payload.get("materials")
    if not isinstance(materials, list):
        raise BusinessError(
            "TOOL_SNAPSHOT_INVALID",
            "冻结事实快照里没有材料位身份，无法核对材料，本次调用未执行",
            status_code=500,
            details={"snapshot_ref": record.snapshot_ref},
        )
    return [item for item in materials if isinstance(item, dict) and item.get("material_key")]


def _material_names_of(context: Any) -> dict[str, str]:
    """材料位的中文名。取自冻结快照，不由工具编造、也不取当前记录。"""
    return {
        str(item["material_key"]): item.get("material_name") or item["material_key"]
        for item in _frozen_materials(context)
    }


def material_scope(context: Any, *, material_keys: list[str], include_peers: bool = False) -> dict[str, Any]:
    """本次判定需要的材料位：冻结身份 + 已核对的真实记录。

    核对逐项做，任一项不同即报 `TOOL_SNAPSHOT_MISMATCH`：
    当前上传引用、内容哈希、材料状态。判定取值一律取自冻结身份。

    `include_peers` 为真时同时核对全部有上传的材料位（去重比对的对照集），
    因为对照集的内容哈希参与判定，用当前值会让结论无法复现。
    """
    session = context.database_session
    frozen = _frozen_materials(context)
    by_key = {str(item["material_key"]): item for item in frozen}
    wanted = [str(key) for key in material_keys]
    if include_peers:
        wanted = sorted({*wanted, *(key for key, item in by_key.items() if item.get("upload_id"))})
    documents = {
        item.material_key: item
        for item in session.scalars(
            select(TaskDocument).where(TaskDocument.audit_task_id == context.task_id)
        ).all()
    }
    uploads: dict[str, DocumentUpload | None] = {}
    for material_key in wanted:
        identity = by_key.get(material_key)
        if identity is None:
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"材料位“{material_key}”不在本次评估冻结的事实快照里，本次调用未执行",
                details={"material_key": material_key, "snapshot_ref": context.fact_snapshot_ref},
            )
        document = documents.get(material_key)
        if document is None:
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"材料位“{material_key}”在任务里已不存在，本次调用未执行",
                details={"material_key": material_key},
            )
        upload_id = identity.get("upload_id")
        upload = session.get(DocumentUpload, upload_id) if upload_id else None
        if upload_id and upload is None:
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"冻结快照引用的上传记录（{upload_id}）已不存在，本次调用未执行",
                details={"material_key": material_key, "upload_id": upload_id},
            )
        if (document.current_upload_id or None) != (upload_id or None):
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"材料位“{material_key}”的当前上传已变化，本次调用未执行；"
                "评估期间的输入变化必须重新冻结快照后重跑",
                details={
                    "material_key": material_key,
                    "frozen_upload_id": upload_id,
                    "current_upload_id": document.current_upload_id,
                },
            )
        if upload is not None and upload.sha256 != identity.get("upload_sha256"):
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"材料位“{material_key}”的上传内容哈希与冻结快照不一致，本次调用未执行",
                details={
                    "material_key": material_key,
                    "upload_id": upload.id,
                    "frozen_sha256": identity.get("upload_sha256"),
                    "current_sha256": upload.sha256,
                },
            )
        if upload is not None and upload.upload_no != identity.get("upload_revision"):
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"材料位“{material_key}”的上传修订号与冻结快照不一致，本次调用未执行",
                details={
                    "material_key": material_key,
                    "frozen_revision": identity.get("upload_revision"),
                    "current_revision": upload.upload_no,
                },
            )
        if document.status != identity.get("document_status"):
            raise BusinessError(
                "TOOL_SNAPSHOT_MISMATCH",
                f"材料位“{material_key}”的材料状态与冻结快照不一致，本次调用未执行",
                details={
                    "material_key": material_key,
                    "frozen_status": identity.get("document_status"),
                    "current_status": document.status,
                },
            )
        uploads[material_key] = upload
    return {
        "identity": {key: by_key[key] for key in wanted},
        "uploads": uploads,
        "documents": {key: documents[key] for key in wanted},
        "material_names": _material_names_of(context),
    }


def _scope_materials(context: Any, requested: list[str]) -> list[str]:
    """本次实际要检查的材料位。

    模型给的材料角色必须落在 `context.material_refs` 授权范围内——
    越权引用显式报错，不静默收窄成「范围内的那部分」：静默收窄会让模型以为
    它检查过了全部材料，而实际只查了一部分。
    """
    scope = context.scoped_materials
    outside = sorted(set(requested) - scope) if scope else []
    if outside:
        raise BusinessError(
            "TOOL_MATERIAL_OUT_OF_SCOPE",
            "请求检查的材料位超出本次授权范围，本次调用未执行",
            details={"outside": outside, "allowed": sorted(scope)},
        )
    return list(requested)


@tool("MATERIAL_EXISTS")
def check_materials_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Annotated[Any, InjectedToolArg],
) -> dict[str, Any]:
    """材料存在性工具：声明的材料位是否都已上传。

    判定与结果结构调 `quality_judgement.material_exists_result`，与材料检查节点同一实现，
    因此工具目录声明的 `applicability` / `standard` / `comparison` / `missing_material_keys` /
    `missing_material_names` 在 Agent 路径上真实存在。已上传判定取冻结快照的上传引用：
    快照里没有上传引用的材料位就是「未上传」。

    `applicability` / `requirement` / `check_level` 由调用方经 runtime 注入，它们是已发布
    配置事实——模型自己写一个适用条件等于它自己决定这条规则适不适用。
    """
    material_refs = parameters.get("material_refs")
    if not isinstance(material_refs, list) or not material_refs:
        raise BusinessError(
            "RULE_PARAMETER_MISSING",
            "材料存在性检查必须声明要核对的材料位",
            details={"parameter": "material_refs"},
        )
    applicability = parameters.get("applicability")
    if not isinstance(applicability, dict):
        raise BusinessError(
            "TOOL_RUNTIME_MISSING",
            "材料存在性检查缺少调用方注入的适用条件，本次调用未执行",
            details={"parameter": "applicability"},
        )
    requirement = parameters.get("requirement")
    check_level = parameters.get("check_level")
    requested = _scope_materials(context, [str(item) for item in material_refs])
    scope = material_scope(context, material_keys=requested)
    available = {key for key, upload in scope["uploads"].items() if upload is not None}
    required_by_key = {key: bool(item.get("required")) for key, item in scope["identity"].items()}
    state = quality_judgement.applicability_state(applicability, available)
    if not state["applicable"]:
        # 适用条件不成立：与节点同一口径记不适用并保留原因，绝不写成通过。
        missing = "、".join(
            scope["material_names"].get(key, key) for key in state["missing_material_keys"]
        )
        status: str | None = "NOT_APPLICABLE"
        message: str | None = f"适用条件“{state['label']}”不成立：尚未上传的材料 {missing}，当前规则不适用"
    else:
        status = None
        message = None
    return quality_judgement.material_exists_result(
        required_keys=requested,
        available_keys=available,
        required_by_key=required_by_key,
        material_names=scope["material_names"],
        applicability=state,
        requirement=requirement if isinstance(requirement, str) else None,
        operator_label="材料是否齐全",
        check_level=check_level if isinstance(check_level, str) else None,
        status=status,
        message=message,
    )


@tool("FILE_QUALITY_CHECK")
def check_file_quality_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Annotated[Any, InjectedToolArg],
) -> dict[str, Any]:
    """文件质量工具：对授权范围内的材料逐份判定一个检查项目。

    判定调 `quality_judgement.judge_quality_item` 与
    `quality_judgement.summarize_per_material`——与文件处理节点同一实现。
    """
    check_item = parameters.get("check_item")
    if not isinstance(check_item, str) or not check_item:
        raise BusinessError(
            "RULE_PARAMETER_MISSING",
            "文件质量检查必须声明检查项目",
            details={"parameter": "check_item"},
        )
    material_refs = parameters.get("material_refs")
    if not isinstance(material_refs, list) or not material_refs:
        raise BusinessError(
            "RULE_PARAMETER_MISSING",
            "文件质量检查必须声明要检查的材料位",
            details={"parameter": "material_refs"},
        )
    requested = _scope_materials(context, [str(item) for item in material_refs])
    # 去重比对要用全部有上传的材料位作为对照集，因此这里连同它们一起核对快照身份。
    scope = material_scope(context, material_keys=requested, include_peers=True)
    uploads_by_key = scope["uploads"]
    material_names = scope["material_names"]
    standard_value = parameters.get("standard_value")
    unit = parameters.get("unit")
    peers = [
        {
            "id": upload.id,
            "sha256": scope["identity"][key].get("upload_sha256"),
            "material_key": key,
            "label": material_names.get(key, key),
        }
        for key, upload in uploads_by_key.items()
        if upload is not None
    ]
    per_material: list[dict[str, Any]] = []
    for material_key in requested:
        upload = uploads_by_key.get(material_key)
        if upload is None:
            per_material.append(
                {
                    "material_key": material_key,
                    "material_name": material_names.get(material_key, material_key),
                    "status": "NOT_APPLICABLE",
                    "message": "该材料未上传，本检查不适用",
                }
            )
            continue
        # 人工已确认与材料位不符的材料位：依据不可用，不能判定通过。
        # 与节点同一口径——类型待复核不算「依据不可用」。
        if scope["identity"][material_key].get("document_status") == "TYPE_REJECTED":
            per_material.append(
                {
                    "material_key": material_key,
                    "material_name": material_names.get(material_key, material_key),
                    "filename": upload.original_filename,
                    "status": "EVIDENCE_INSUFFICIENT",
                    "message": "材料类型已被人工确认与材料位不符，文件质量检查缺少有效依据，不能判定通过",
                }
            )
            continue
        verdict = quality_judgement.judge_quality_item(
            check_item,
            upload=upload,
            standard_value=standard_value if isinstance(standard_value, str) else None,
            unit=unit if isinstance(unit, str) else None,
            peers=peers,
            gap=None,
        )
        per_material.append(
            {
                "material_key": material_key,
                "material_name": material_names.get(material_key, material_key),
                "filename": upload.original_filename,
                **verdict,
            }
        )
    summary = quality_judgement.summarize_per_material(per_material)
    statuses = [item["status"] for item in per_material]
    return {
        **summary,
        "check_item": check_item,
        "per_material": per_material,
        "status_counts": {value: statuses.count(value) for value in sorted(set(statuses))},
    }


@tool("DECIMAL_CN_EQUAL")
def decimal_cn_equal_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Annotated[Any, InjectedToolArg] = None,
) -> dict[str, Any]:
    """大小写金额一致性工具。

    调 `quality_judgement.decimal_cn_equal`——与算子 `DECIMAL_CN_EQUAL` 同一实现。
    当前该判定固定返回「口径未确认，不可判定」，工具如实透出，不伪装成已执行。
    """
    return quality_judgement.decimal_cn_equal(inputs)
