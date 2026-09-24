"""工具 adapter：把共享领域服务接到 Agent 工具接口上。

为什么单独成模块
----------------
工具与节点必须**共用同一份判定实现**（`domain/quality_judgement.py`），
但两者的输入来源不同：

- 节点从 ORM 与已发布 manifest 取数（它持有会话与任务）；
- 工具只拿到 `ToolExecutionContext`（会话在 `database_session` 里）+ 模型给的业务参数。

adapter 的职责就是把后者的形状转成前者的输入，然后调**同一个**共享服务。
它不含任何判定逻辑：这里一旦出现 `if check_item == ...` 就说明判定被复制了。

模型只给业务参数
----------------
模型能填的只有业务口径：要检查的材料角色、检查项目、标准值/单位。
任务 ID、事实快照、策略版本、会话全部由运行时经 `ToolExecutionContext` 注入，
模型看不到也填不了（schema 里没有这些键，`additionalProperties: false` 会拒绝）。

工具读的不是「当前数据」
------------------------
需要读材料记录的工具**先读冻结快照**（`evaluation_snapshot`），再用它核对真实记录：

- 冻结快照里的材料位身份是权威版本（材料位、必传属性、当前上传的 id 与内容哈希、
  材料状态、修订号）；
- 真实记录必须逐项与冻结身份一致：上传引用、内容哈希、材料状态任一项不同即
  `TOOL_SNAPSHOT_MISMATCH`，本次调用不执行；
- 判定用到的取值（必传属性、材料名、上传哈希）取自冻结身份，不取当前记录。

只存哈希、执行时仍读「当前数据」的写法会让同一次评估的两次工具调用落到不同的
材料版本上，而指纹对得上只是巧合——所以快照存的是**内容**，不是只有指纹。
"""

from __future__ import annotations

from typing import Any

from ..domain import file_quality, quality_judgement
from ..domain.audit_vocab import read_material_type_status
from ..errors import BusinessError
from ..models import DocumentUpload, EvaluationSnapshot, StoredArtifact, TaskDocument
from sqlalchemy import select


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


def _frozen_materials(context: Any) -> list[dict[str, Any]]:
    record = _snapshot_record(context)
    payload = record.facts_json if isinstance(record.facts_json, dict) else {}
    materials = payload.get("materials")
    if not isinstance(materials, list) or not materials:
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
    当前上传引用、内容哈希、材料状态。判定取值一律取自**冻结身份**。

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


def check_materials_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Any,
) -> dict[str, Any]:
    """材料存在性工具：声明的材料位是否都已上传。

    判定与结果结构调 `quality_judgement.material_exists_result`——与材料检查节点
    **同一实现**，因此工具目录声明的 `applicability` / `standard` / `comparison` /
    `missing_material_keys` / `missing_material_names` 在 Agent 路径上真实存在，
    不是只写在文档里的键。

    已上传判定取冻结快照的上传引用：快照里没有上传引用的材料位就是「未上传」。

    `applicability` / `requirement` / `check_level` 由调用方经 runtime 注入
    （见 `domain.tool_catalog._RUNTIME_KEYS`），它们是**已发布配置事实**，
    模型不能提供——模型自己写一个适用条件等于它自己决定这条规则适不适用。
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


def check_file_quality_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Any,
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
        if read_material_type_status(scope["identity"][material_key].get("document_status")) == "TYPE_REJECTED":
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


def decimal_cn_equal_tool(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    context: Any = None,
) -> dict[str, Any]:
    """大小写金额一致性工具。

    调 `quality_judgement.decimal_cn_equal`——与算子 `DECIMAL_CN_EQUAL` 同一实现。
    当前该判定固定返回「口径未确认，不可判定」，工具如实透出，不伪装成已执行。
    """
    return quality_judgement.decimal_cn_equal(inputs)


def artifact_ready(context: Any, upload_id: int, artifact_type: str, upload_sha256: str) -> bool:
    """某份上传是否已有指定类型的就绪产物。

    文件质量检查的真实依据是产物（例如印章识别的结果），
    工具据此判断「证据是否已经准备好」，不拿处理成功冒充质量通过。
    """
    if context is None or context.database_session is None:
        return False
    return (
        context.database_session.scalar(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id == upload_id,
                StoredArtifact.artifact_type == artifact_type,
                StoredArtifact.upload_sha256 == upload_sha256,
                StoredArtifact.status == "READY",
            )
        )
        is not None
    )


def known_quality_items() -> tuple[str, ...]:
    """系统已知的文件检查项目。工具目录据此枚举，不另写一份名单。"""
    return tuple(sorted(file_quality.KNOWN_QUALITY_ITEMS))
