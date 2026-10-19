"""工具 adapter：把共享领域服务接到 Agent 工具接口上。

adapter 把 `ToolExecutionContext` 中的真实任务数据与模型给出的工具参数交给实际工具实现。
材料与文件质量工具的判定实现统一在 `domain/quality_judgement.py`，不按业务节点分发。

模型能填的只有业务口径（要检查的材料角色、检查项目、标准值/单位）；任务 ID、任务配置、
会话全部由运行时经 `ToolExecutionContext` 注入，模型看不到也填不了
（schema 里没有这些键，`additionalProperties: false` 会拒绝）。

需要读材料记录的工具先按任务配置确定材料范围，再读取当前任务上传内容；材料缺失、
越权或产物未准备好时明确报错，不把缺失内容当成成功结果。"""

from __future__ import annotations
import json
from typing import Annotated, Any
from langchain_core.tools import InjectedToolArg, tool
from ....domain import file_quality, quality_judgement
from ....domain.rule_library import active
from ....common.exceptions import BusinessError
from ....entity import DocumentUpload, TaskDocument
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
    规则由审核图按任务范围与任务配置执行；该工具只负责在需要时返回规则原文和依赖配置。
    """
    if context is None or context.database_session is None or context.task_id is None:
        raise BusinessError("TOOL_CONTEXT_MISSING", "规则库查询需要审核任务上下文", status_code=500)
    from ....entity import AuditTask

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
        if item["rule_key"] not in enabled_keys or not active(item):
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
    }


def _requested_uploads(context: Any, material_keys: list[str]):
    from ....entity import AuditTask
    task = context.database_session.get(AuditTask, context.task_id)
    if task is None:
        raise BusinessError("TOOL_TASK_MISSING", "工具关联的任务不存在")
    material_scope(context, material_keys=_scope_materials(context, material_keys))
    frozen = {m["material_key"]: m for m in _current_materials(context)}
    for key in material_keys:
        identity = frozen[key]
        upload = context.database_session.get(DocumentUpload, identity.get("upload_id")) if identity.get("upload_id") else None
        if upload is None:
            raise BusinessError("TOOL_DOCUMENT_NOT_UPLOADED", "选定材料没有实际上传", details={"material_key": key})
        yield task, key, upload


@tool("TASK_DATA_READ")
def read_task_data_tool(parameters: dict[str, Any], inputs: list[dict[str, Any]], *, context: Annotated[Any, InjectedToolArg]) -> dict[str, Any]:
    """读取前置处理产物：OCR 正文 Markdown 或原文件印章信息。"""
    from ..artifacts import _artifact_for_upload, _artifact_payload
    items = []
    kind = parameters.get("artifact_type") or "OCR"
    for task, key, upload in _requested_uploads(context, parameters["material_keys"]):
        artifact = _artifact_for_upload(context.database_session, upload, kind)
        if artifact is None or artifact.status != "READY":
            raise BusinessError("TOOL_ARTIFACT_NOT_READY", "请求的材料产物尚未生成", details={"material_key": key, "artifact_type": kind})
        content = _artifact_payload(artifact).content
        if kind == "OCR":
            content = {**content, "markdown": "\n\n".join(page["text"] for page in content["pages"]), "content_type": "text/markdown"}
        items.append({"material_key": key, "upload_id": upload.id, "artifact_id": artifact.id, "artifact_type": kind, "content": content})
    return {"status": "SUCCEEDED", "message": "已读取实际材料内容", "items": items}


@tool("DOCUMENT_PROCESS")
def process_documents_tool(parameters: dict[str, Any], inputs: list[dict[str, Any]], *, context: Annotated[Any, InjectedToolArg]) -> dict[str, Any]:
    """指定服务的实际调用入口；业务模型不再选择此工具，前置流程直接调用服务。"""
    from ...shared.storage import resolve_storage_path
    from ..document_ports import document_service
    from ..artifacts import (
        _ensure_external_artifact,
        _artifact_for_upload,
        _artifact_payload,
        _submit_ocr_job,
        _complete_ocr_job,
    )
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


def _current_materials(context: Any) -> list[dict[str, Any]]:
    from ....entity import AuditTask
    from ..agent import _task_materials
    if context is None or context.database_session is None or context.task_id is None:
        raise BusinessError("TOOL_CONTEXT_MISSING", "工具调用需要任务与数据库会话")
    task = context.database_session.get(AuditTask, context.task_id)
    if task is None:
        raise BusinessError("TOOL_TASK_MISSING", "工具关联的任务不存在")
    return _task_materials(context.database_session, task)


def _material_names_of(context: Any) -> dict[str, str]:
    """材料位的中文名。取自任务配置，不由工具编造。"""
    return {
        str(item["material_key"]): item.get("material_name") or item["material_key"]
        for item in _current_materials(context)
    }


def material_scope(context: Any, *, material_keys: list[str], include_peers: bool = False) -> dict[str, Any]:
    """读取任务当前材料；不存在的材料明确报错。"""
    session = context.database_session
    identities = {m["material_key"]: m for m in _current_materials(context)}
    wanted = list(dict.fromkeys(material_keys))
    if include_peers:
        wanted = sorted({*wanted, *(k for k, m in identities.items() if m.get("upload_id"))})
    documents = {d.material_key: d for d in session.scalars(
        select(TaskDocument).where(TaskDocument.audit_task_id == context.task_id)).all()}
    missing = [k for k in wanted if k not in documents]
    if missing:
        raise BusinessError("TOOL_MATERIAL_MISSING", "请求的材料不属于当前任务", details={"materials": missing})
    uploads = {k: session.get(DocumentUpload, documents[k].current_upload_id)
               if documents[k].current_upload_id else None for k in wanted}
    return {"identity": {k: identities[k] for k in wanted}, "uploads": uploads,
            "documents": {k: documents[k] for k in wanted}, "material_names": _material_names_of(context)}


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

    判定与结果结构调 `quality_judgement.material_exists_result`，由材料存在性工具使用，
    因此工具目录声明的 `applicability` / `standard` / `comparison` / `missing_material_keys` /
    `missing_material_names` 在 Agent 路径上真实存在。已上传判定取当前任务材料记录：
    没有当前上传引用的材料位就是「未上传」。

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
    `quality_judgement.summarize_per_material`，不复制判定逻辑。
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
    # 去重比对要用全部有上传的材料位作为对照集，因此这里连同它们一起读取。
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
        "judgement_supported": file_quality.is_implemented(check_item),
        "per_material": per_material,
        "status_counts": {value: statuses.count(value) for value in sorted(set(statuses))},
    }


