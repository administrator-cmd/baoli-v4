"""工具 adapter：把共享领域服务接到 Agent 工具接口上。

adapter 把 `ToolExecutionContext` 中的真实任务数据与模型给出的工具参数交给实际工具实现。
文件质量工具的判定实现统一在 `domain/materials/quality_judgement.py`，不按业务节点分发。

模型能填的只有业务口径（要检查的材料角色、检查项目、标准值/单位）；任务 ID、任务配置、
会话全部由运行时经 `ToolExecutionContext` 注入，模型看不到也填不了
（schema 里没有这些键，`additionalProperties: false` 会拒绝）。

需要读材料记录的工具先按任务配置确定材料范围，再读取当前任务上传内容；材料缺失、
越权或产物未准备好时明确报错，不把缺失内容当成成功结果。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg, tool

from app.common.exceptions import BusinessError
from app.domain.materials import file_quality, quality_judgement
from app.domain.configuration.rule_library import active
from app.integrations.documents.ocr_concurrency import OCR_CONCURRENCY_LIMITER
from app.modules.audit.artifact.execution import ArtifactExecution
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD


def _scenario_version_no(db: Any, task: Any) -> int | None:
    """任务冻结的场景版本号；场景版本发布后不可变。"""
    from app.modules.scenario.template.model import ScenarioVersion
    version = ScenarioTemplateCRUD(db).get_of(ScenarioVersion, task.scenario_version_id)
    return version.version_no if version is not None else None


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
    from app.modules.audit.task.crud import AuditTaskCRUD
    from app.modules.audit.task.model import AuditTask

    task = AuditTaskCRUD(context.database_session).get_of(AuditTask, context.task_id)
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

    detailed = bool(requested_codes or requested_keys)
    if detailed:
        # 按明确身份查询：返回完整配置，供汇总/风险规则核对来源规则配置。
        items = [{"kind": item.get("kind") or "RULE", "rule_code": item.get("rule_code"),
                  "name": item.get("name"), "config": item} for item in rules if matches(item)]
        message = f"已从本次任务冻结规则库查询到 {len(items)} 条生效规则"
    else:
        # 目录查询：不返回完整配置，避免整库规则配置撑大工具结果。
        items = []
        for item in rules:
            if not matches(item):
                continue
            entry = {
                "kind": item.get("kind") or "RULE",
                "rule_code": item.get("rule_code"),
                "name": item.get("name"),
                "execution_mode": item.get("execution_mode"),
                "rule_role": item.get("rule_role"),
                "requirement": item.get("requirement") or "",
            }
            # 没有业务编号的规则只能靠内部键定位；只在缺少编号时补充，避免目录重新变长。
            if not entry["rule_code"]:
                entry["rule_key"] = item.get("rule_key")
            items.append(entry)
        message = (f"已从本次任务冻结规则库查询到 {len(items)} 条生效规则；"
                   "目录结果省略完整配置，需要完整配置请按 rule_codes 或 rule_keys 查询")
    return {
        "status": "SUCCEEDED",
        "message": message,
        "items": items,
        "source": "TASK_FROZEN_MANIFEST",
        "scenario_version_no": _scenario_version_no(context.database_session, task),
        "plan_scope_hash": schema.get("plan_scope_hash"),
    }


def _material_snapshot(context: Any) -> dict[str, Any]:
    """任务材料位与当前上传的一次读取结果。

    只读视图、ORM 实例和中文名都从这一份快照派生，工具调用不再重复查询。
    """
    from app.modules.audit.execution.service.state import _material_states
    from app.modules.audit.shared import _current_uploads, _material_entries
    from app.modules.audit.task.crud import AuditTaskCRUD
    from app.modules.audit.task.model import AuditTask
    if context is None or context.database_session is None or context.task_id is None:
        raise BusinessError("TOOL_CONTEXT_MISSING", "工具调用需要任务与数据库会话")
    task = AuditTaskCRUD(context.database_session).get_of(AuditTask, context.task_id)
    if task is None:
        raise BusinessError("TOOL_TASK_MISSING", "工具关联的任务不存在")
    pairs = _current_uploads(context.database_session, task)
    identities = _material_entries(pairs, _material_states(pairs))
    return {
        "task": task,
        "identity": {item["material_key"]: item for item in identities},
        "documents": {document.material_key: document for document, _ in pairs},
        "uploads": {document.material_key: upload for document, upload in pairs},
        "material_names": {document.material_key: document.material_name for document, _ in pairs},
    }


def _scope_view(snapshot: dict[str, Any], *, material_keys: list[str],
                include_peers: bool = False) -> dict[str, Any]:
    """从快照中取指定材料位；不属于当前任务的材料明确报错。"""
    identities, documents, uploads = snapshot["identity"], snapshot["documents"], snapshot["uploads"]
    wanted = list(dict.fromkeys(material_keys))
    if include_peers:
        wanted = sorted({*wanted, *(key for key, item in identities.items() if item.get("upload_id"))})
    missing = [key for key in wanted if key not in documents]
    if missing:
        raise BusinessError("TOOL_MATERIAL_MISSING", "请求的材料不属于当前任务", details={"materials": missing})
    return {"identity": {key: identities[key] for key in wanted},
            "uploads": {key: uploads[key] for key in wanted},
            "documents": {key: documents[key] for key in wanted},
            "material_names": snapshot["material_names"]}


def _requested_uploads(context: Any, material_keys: list[str]):
    requested = _scope_materials(context, material_keys)
    snapshot = _material_snapshot(context)
    uploads = _scope_view(snapshot, material_keys=requested)["uploads"]
    for key in material_keys:
        upload = uploads[key]
        if upload is None:
            raise BusinessError("TOOL_DOCUMENT_NOT_UPLOADED", "选定材料没有实际上传", details={"material_key": key})
        from app.modules.audit.execution.service.material_precheck import AuditMaterialPrecheckService
        AuditMaterialPrecheckService(context.database_session).assert_processing_allowed(snapshot["task"], upload)
        yield snapshot["task"], key, upload


def read_artifact_items(context: Any, *, material_keys: list[str], artifact_type: str) -> list[dict[str, Any]]:
    """按当前任务的授权材料读取指定产物；工具与抽取预读共用这一份取数逻辑。"""
    from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
    items = []
    for _, key, upload in _requested_uploads(context, material_keys):
        artifact = AuditArtifactArtifactsService(context.database_session)._artifact_for_upload( upload, artifact_type)
        if artifact is None or artifact.status != "READY":
            raise BusinessError("TOOL_ARTIFACT_NOT_READY", "请求的材料产物尚未生成", details={"material_key": key, "artifact_type": artifact_type})
        content = AuditArtifactArtifactsService()._artifact_payload(artifact).content
        if artifact_type == "OCR":
            # 每页 text 即唯一正文；blocks 是同一段正文的副本，不再返回。
            content = {
                **content,
                "pages": [{key: value for key, value in page.items() if key != "blocks"}
                          for page in content["pages"]],
                "content_type": "text/markdown",
            }
        items.append({"material_key": key, "upload_id": upload.id, "filename": upload.original_filename,
                      "artifact_id": artifact.id, "artifact_type": artifact_type, "content": content})
    return items


@tool("TASK_DATA_READ")
def read_task_data_tool(parameters: dict[str, Any], inputs: list[dict[str, Any]], *, context: Annotated[Any, InjectedToolArg]) -> dict[str, Any]:
    """读取前置处理产物：OCR 正文、原文件印章信息或材料类型识别结论。"""
    kind = parameters.get("artifact_type") or "OCR"
    items = read_artifact_items(context, material_keys=parameters["material_keys"], artifact_type=kind)
    return {"status": "SUCCEEDED", "message": "已读取实际材料内容", "items": items}


@tool("DOCUMENT_PROCESS")
def process_documents_tool(parameters: dict[str, Any], inputs: list[dict[str, Any]], *, context: Annotated[Any, InjectedToolArg]) -> dict[str, Any]:
    """指定服务的实际调用入口；业务模型不再选择此工具，前置流程直接调用服务。"""
    from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
    from app.modules.audit.artifact.service.document_ports import document_service
    from app.services.shared.storage import resolve_storage_path
    service = parameters["service"]
    db = context.database_session
    items = []
    for task, key, upload in _requested_uploads(context, parameters["material_keys"]):
        kwargs = {"file_path": resolve_storage_path(upload.relative_path), "filename": upload.original_filename, "content_type": upload.content_type}
        if service == "OCR":
            parsed = AuditArtifactArtifactsService(db)._artifact_for_upload( upload, "DOCUMENT_PARSE")
            if parsed is None or parsed.status != "READY":
                raise BusinessError("PROCESSED_ARTIFACT_MISSING", "OCR需要文件处理产物；请先明确调用DOCUMENT_PARSE", details={"material_key": key})
            with OCR_CONCURRENCY_LIMITER.slot():
                artifact = ArtifactExecution(db)._complete_ocr_job(
                    task, upload, ArtifactExecution(db)._submit_ocr_job(task, upload, parsed),
                )
        else:
            functions = {"DOCUMENT_PARSE": document_service().process_document, "SEAL": document_service().detect_seals, "MATERIAL_CLASSIFY": document_service().validate_material_type}
            if service == "MATERIAL_CLASSIFY":
                kwargs["material_type"] = upload.declared_material_type
            artifact = ArtifactExecution(db)._ensure_external_artifact( task, upload, service, lambda: functions[service](**kwargs))
        items.append({"material_key": key, "upload_id": upload.id, "artifact_id": artifact.id, "artifact_type": service, "content": AuditArtifactArtifactsService()._artifact_payload(artifact).content})
    return {"status": "SUCCEEDED", "message": "指定服务已返回实际产物", "items": items}


def material_scope(context: Any, *, material_keys: list[str], include_peers: bool = False) -> dict[str, Any]:
    """读取任务当前材料；不存在的材料明确报错。"""
    return _scope_view(_material_snapshot(context), material_keys=material_keys, include_peers=include_peers)


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
