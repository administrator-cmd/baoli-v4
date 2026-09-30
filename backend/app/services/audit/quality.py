"""文件处理节点与文件质量检查。

文件处理按材料位逐份执行：8018 材料识别（**只记录不判定**）、8010 处理、OCR、印章识别
（按需启用）。印章是否启用由两条依据决定，缺一不可：文件名质量规则声明
`artifact_type = SEAL`，或字段在 03_字段 中声明了「产物来源=印章识别」。

文件质量检查逐份材料判定并保留每份的结果：不合格优先、其次不可判定、全部适用结果合格
才通过。不可判定的检查项目（图像清晰度、页码连续、有效签章、骑缝章）缺少已确认的判定
依据，明确记为不可判定并给出能力缺口，绝不因为文件处理成功就当作通过。

材料类型待复核**不阻断**这些检查：判定依据是真实上传记录，类型本身作为待确认事项另行说明。"""

from ...domain import file_quality, quality_judgement
from ...domain.audit_vocab import MATERIAL_TYPE_REJECTED, MATERIAL_TYPE_REVIEW, declared_artifact_source, is_type_unavailable, read_material_type_status
from ...domain.file_quality import CHECK_FILE_DUPLICATE, CHECK_FILE_FORMAT, CHECK_FILE_SIZE
from ...domain.identity import public_no
from ...domain.manifest import OPERATOR_LABELS, rule_node_type_of
from ...common.exceptions import BusinessError
from .document_ports import document_service
from ...entity import AuditTask, DocumentUpload, ManualReview, NodeExecution, RuleResult, StoredArtifact
from ..storage_service import resolve_storage_path
from .artifacts import _complete_ocr_job, _ensure_external_artifact, _material_classify_content, _submit_ocr_job
from .evidence import _copy_artifact_evidence_to_rule
from .rules import _build_index_if_needed
from .shared import _applicability_json, _applicability_state, _current_uploads, _is_subject_rule, _material_names, _not_applicable_message
from .state import _material_classify_result
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


def _seal_field_material_keys(
    fields: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """材料位 -> 声明了「产物来源=印章识别」的字段 key。

    印章识别的启用依据之一是**字段自己的声明**：声明取自印章产物的字段，其所属
    材料位必须有印章产物，否则该字段没有可取的来源。只看规则的适用材料会漏掉这一类
    （表单路径的比较规则不填写适用材料列），使声明无法落实。
    """
    by_material: dict[str, list[str]] = {}
    for field in fields:
        if declared_artifact_source(field) is None:
            continue
        by_material.setdefault(field["material_key"], []).append(field["field_key"])
    return by_material

def _seal_requirements(manifest: dict[str, Any], enabled_keys: set[str]) -> dict[str, Any]:
    """需要印章产物的启用规则与其材料范围。

    印章按需启用，来源有两条，缺一不可：

      1. 文件名质量规则声明 `artifact_type = SEAL`（检查项目是甲方签章 / 乙方签章 / 骑缝章）；
      2. **主体核对规则自身**：规则引用的字段在 03_字段 中声明了「产物来源=印章识别」。
         这类规则不需要额外启用某条文件质量检查才取得印章产物，也不需要再启用一条条款规则。

    未启用时报告与页面显示为“未配置”，不能因为服务不可用就静默删掉步骤。
    """
    quality_rules = [
        rule
        for rule in manifest["rules"]
        if rule_node_type_of(rule) == "DOCUMENT_PREPARE"
        and rule["operator"] == "FILE_QUALITY_CHECK"
        and rule["rule_key"] in enabled_keys
        and rule.get("parameters", {}).get("artifact_type") == "SEAL"
    ]
    subject_rules = [
        rule
        for rule in manifest["rules"]
        if rule["rule_key"] in enabled_keys and _is_subject_rule(rule)
    ]
    names = [rule["name"] for rule in quality_rules]
    for rule in subject_rules:
        if rule["name"] not in names:
            names.append(rule["name"])
    material_keys: set[str] = {key for rule in quality_rules for key in rule["materials"]}
    for rule in subject_rules:
        # 主体核对的印章候选来自规则材料范围与输入字段所属材料；
        # 待核对主体本身取自合同正文，它的材料位不需要印章产物。
        for key in rule.get("materials") or []:
            material_keys.add(key)
    return {"rule_names": names, "material_keys": material_keys}

def _seal_required_material_keys(
    manifest: dict[str, Any],
    task: AuditTask,
    enabled_keys: set[str],
) -> dict[str, Any]:
    """本次任务真正需要印章产物的规则、字段与材料位。

    字段范围按**任务实际执行的字段**收敛：任务快照之外的字段不参与抽取，
    不因它们声明了印章来源就去调用印章识别服务。声明了印章来源的字段，其材料位
    一律纳入范围——否则该字段的抽取没有可取值的来源，只能失败。
    """
    requirements = _seal_requirements(manifest, enabled_keys)
    seal_fields_by_material = _seal_field_material_keys(task.task_schema_json["fields"])
    material_keys: set[str] = set(requirements["material_keys"])
    material_keys.update(seal_fields_by_material)
    return {
        "rule_names": requirements["rule_names"],
        "field_keys": sorted({key for keys in seal_fields_by_material.values() for key in keys}),
        "material_keys": material_keys,
    }

def _type_review_recorded(db: Session, task: AuditTask, upload: DocumentUpload) -> bool:
    """该上传是否已经有材料类型复核记录。**已裁决的也算**：类型这件事已经交给人处理过一次。

    只看「有没有未失效的待办」会让裁决过一轮的材料位在下一批被重新挂上同一条待办，
    形成「裁决 → 重挂 → 再裁决」的循环。材料位换了上传（重新上传文件）才会重新进入
    待裁决状态，因为那时依据真的变了。
    """
    return db.scalar(
        select(ManualReview.id)
        .where(
            ManualReview.audit_task_id == task.id,
            ManualReview.document_upload_id == upload.id,
            ManualReview.review_type == "MATERIAL_TYPE_MISMATCH",
        )
        .limit(1)
    ) is not None

def _sync_type_review_pending(db: Session, task: AuditTask) -> dict[str, Any]:
    """保证「类型待复核」的材料位都有可提交的人工复核待办。

    材料类型的自动识别只供参考，不阻断审核（真实原文照常参与判定），但类型本身仍需
    由人定案。部分历史任务只保留了识别产物，没有可提交的裁决入口。

    这里按当前有效的 MATERIAL_CLASSIFY 产物把识别结论与命中关键词快照进待办，供人据实裁决；
    已裁决过的材料位不再重复挂。**不替人下结论、也不改材料状态**：材料状态只在人工提交
    裁决时改变。识别产物不可用时如实写明，不伪造结论。

    只在**当前没有任何待处理复核项**时补挂（文件处理节点、进入复核节点却没有待办可处理时）：
    规则结论与字段事实的待办优先，补挂材料类型待办不能把它们挤掉、更不能让已排队的那一批
    被重新洗牌。历史任务在进入复核等待点之前确实一条待办都没有，那时才由这里给出入口。
    """
    if db.scalar(
        select(ManualReview.id)
        .where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
        .limit(1)
    ) is not None:
        return {
            "created": [],
            "created_count": 0,
            "skipped_reason": "当前已有待处理的人工复核项，本次不补挂材料类型待办",
        }
    created: list[dict[str, Any]] = []
    for document, upload in _current_uploads(db, task):
        if upload is None or not is_type_unavailable(document.status):
            continue
        if _type_review_recorded(db, task, upload):
            continue
        service_result = _material_classify_result(_material_classify_content(db, upload))
        # 类型复核是**待确认事项**，不是「依据不可用」：真实原文照常参与判定，
        # 因此文案不能说成「依赖它的检查缺少有效依据」——那会把待确认说成阻断。
        pending_note = (
            "材料类型待复核：自动识别给出的类型结论尚未由人定案，本材料仍在审核范围内，"
            "依赖它的检查按材料中的真实原文判定。请确认该文件与材料位是否相符。"
            if read_material_type_status(document.status) == MATERIAL_TYPE_REVIEW
            else "材料类型已被人工确认与材料位不符，依赖它的检查缺少有效依据。"
        )
        message = pending_note + (
            "自动识别结论仅供判断参考，不构成结论。"
            if service_result is not None
            else "该上传没有可用的自动识别结论，请按文件内容判断。"
        )
        db.add(
            ManualReview(
                review_no=public_no("REV"),
                audit_task_id=task.id,
                document_upload_id=upload.id,
                review_type="MATERIAL_TYPE_MISMATCH",
                status="PENDING",
                system_judgment_json={
                    "status": read_material_type_status(document.status),
                    "message": message,
                    "material_status": document.status,
                    "service_code": "MATERIAL_CLASSIFY",
                    "service_result": service_result,
                },
                business_note=message,
            )
        )
        created.append(
            {
                "material_key": document.material_key,
                "material_name": document.material_name,
                "filename": upload.original_filename,
                "material_status": document.status,
            }
        )
    if created:
        db.flush()
    return {"created": created, "created_count": len(created)}

def _document_prepare(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    seal_requirements = _seal_required_material_keys(manifest, task, enabled_keys)
    seal_rule_names = seal_requirements["rule_names"]
    seal_material_keys = seal_requirements["material_keys"]
    seal_required = bool(seal_material_keys)
    processed = 0
    steps: list[dict[str, Any]] = []
    # 各材料位的 8018 识别结论，随本节点结果一起返回供页面显示；不影响任何执行分支。
    material_classify_by_key: dict[str, dict[str, Any] | None] = {}
    for document, upload in _current_uploads(db, task):
        if not upload:
            continue
        source_path = resolve_storage_path(upload.relative_path)

        # 8018/8010/印章与 OCR 一样先占位再调用：占位提交后才联系供应商，中断不会被自动重放。
        # doc_type 取**本次上传时用户声明的类型**（上传入口已按 8018 目录核对过），
        # 不来自场景配置：材料位不声明类型，同一材料位换文件时可以换类型。
        _ensure_external_artifact(
            db,
            task,
            upload,
            "MATERIAL_CLASSIFY",
            lambda: document_service().validate_material_type(
                file_path=source_path,
                filename=upload.original_filename,
                content_type=upload.content_type,
                material_type=upload.declared_material_type,
            ),
        )
        # 8018 的识别结论只记录不判定：不改材料状态、不挂人工复核，后续节点照常执行。
        material_classify_by_key[document.material_key] = _material_classify_content(db, upload)
        # 正文抽取依赖的 8010 处理链路必须真实执行，不因本模板没有质量规则而跳过。
        parsed_artifact = _ensure_external_artifact(
            db,
            task,
            upload,
            "DOCUMENT_PARSE",
            lambda: document_service().process_document(
                file_path=source_path,
                filename=upload.original_filename,
                content_type=upload.content_type,
            ),
        )

        ocr_artifact = _submit_ocr_job(db, task, upload, parsed_artifact)
        _complete_ocr_job(db, task, upload, ocr_artifact)

        seal_enabled_for_upload = seal_required and document.material_key in seal_material_keys
        if seal_enabled_for_upload:
            _ensure_external_artifact(
                db,
                task,
                upload,
                "SEAL",
                lambda: document_service().detect_seals(
                    file_path=source_path,
                    filename=upload.original_filename,
                    content_type=upload.content_type,
                ),
            )
        if not is_type_unavailable(document.status):
            # 文件处理完成只表达“产物已就绪”，不覆盖“类型待复核 / 类型已否”这类人工尚未推翻的结论。
            document.status = "PREPARED"
        upload.status = "PROCESSED"
        processed += 1
        steps.append(
            {
                "material_name": document.material_name,
                "filename": upload.original_filename,
                "material_classify": "COMPLETED",
                "document_parse": "COMPLETED",
                "ocr": "COMPLETED",
                "seal": "COMPLETED" if seal_enabled_for_upload else "NOT_CONFIGURED",
            }
        )
    quality_summary = _execute_file_quality_rules(db, task, manifest, execution)
    # 类型待复核的材料位要有可提交的裁决入口，否则任务会停在「没有待复核项、但类型仍未定案」
    # 的状态上——历史任务的自动识别结论就留在产物里没有人能处理。
    # 本函数只在当前一条待办都没有时才补挂，不会挤掉已经在排队的规则与字段复核。
    type_review_pending = _sync_type_review_pending(db, task)
    # 只有本任务确实有需要检索取证的规则时才建立索引：明确的字段算术规则不应因为
    # Embedding 不可用而增加依赖，也不应让整个文件处理节点因缺模型而失败。
    index_summary = _build_index_if_needed(db, task, manifest)
    # 本节点已经真实执行了正文抽取链路；没有质量规则只说明该子步骤未配置，不能把整个节点标成“未执行”。
    skipped = processed == 0
    return {
        "processed_documents": processed,
        "documents": steps,
        # 各材料位的 8018 识别结论（含置信度）：只记录与显示，不参与任何执行分支。
        "material_classify": material_classify_by_key,
        # 本次建立的证据索引版本与模型：检索记录据此可追溯；不需要检索时为未启用。
        "evidence_index": index_summary,
        # 已配置但当前无法形成结论的检查（供应商未提供质量结论）必须如实呈现，不能被当成已通过。
        "capability_gaps": manifest.get("capability_gaps") or [],
        # 按需执行的决定在这里显式保存，供页面与报告判断“未配置印章”还是“印章失败”。
        "seal": {
            "required": seal_required,
            "status": "ENABLED" if seal_required else "NOT_CONFIGURED",
            "source_rules": seal_rule_names,
            # 声明「产物来源=印章识别」的字段：它们没有印章产物就无源可取，
            # 因此即使没有主体核对规则，这些字段所在材料位也要执行印章识别。
            "source_field_keys": seal_requirements["field_keys"],
            "material_keys": sorted(seal_material_keys),
            "message": (
                "已按字段的「产物来源=印章识别」声明与主体核对规则启用印章识别"
                if seal_required
                else "本任务没有声明印章产物来源的字段，也没有需要印章产物的规则，本次未启用印章识别"
            ),
        },
        **quality_summary,
        # 本次为“类型待复核”材料位补齐的待办：谁、依据什么进了人工复核必须能核对。
        "type_review_pending": type_review_pending,
        "skipped": skipped,
        "skip_reason": "当前任务没有已上传材料，文件处理节点没有可执行对象" if skipped else None,
    }

def _quality_material_inputs(
    per_material: list[dict[str, Any]],
    material_names: dict[str, str],
) -> list[dict[str, Any]]:
    """文件质量检查的材料归属摘要。

    文件检查没有字段输入，但报告与风险要按**实际涉及的材料**筛选结果；
    归属因此取自逐份材料的判定结果，与 `_result_material_keys` 读取的键结构一致。
    未上传的材料位（NOT_APPLICABLE）不计入归属：这次判定没有用到它。

    这里的条目是**材料级**事实，不是字段取值：`kind=MATERIAL` 显式标明，并带上该份材料
    自己的判定状态与文件名。页面据此逐份说明「查的是哪份文件、那份判成了什么」，
    不会退化成一条没有字段名、没有取值的残缺输入行。
    """
    summary: list[dict[str, Any]] = []
    for item in per_material:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "NOT_APPLICABLE":
            continue
        material_key = item.get("material_key")
        if not material_key:
            continue
        summary.append(
            {
                # 材料级输入的显式标记：页面与报告据此区分「字段取值」与「逐份材料的判定」。
                "kind": "MATERIAL",
                "material_key": material_key,
                "material_name": item.get("material_name") or material_names.get(material_key, material_key),
                "filename": item.get("filename"),
                # 该份材料在本条检查里的判定，不是整个任务的结论。
                "status": item.get("status"),
                "message": item.get("message"),
                "value_type": None,
                "unit": None,
            }
        )
    return summary

def _quality_standard_view(
    rule: dict[str, Any],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """文件质量检查的审核标准：检查项目、合格要求、标准值与检查口径。

    这些取值全部来自任务绑定的已确认配置（「检查项目」「合格要求」「标准值 / 单位」
    「检查口径」四列），系统不补默认条件。缺少检查口径时如实标 `clear=false`，
    页面据此说明缺的是什么，而不是拿检查名称当标准。
    """
    check_item = parameters.get("check_item")
    requirement = parameters.get("requirement")
    criterion = str(parameters.get("criterion") or "").strip()
    check_label = file_quality.label_of(check_item) if check_item else None
    requirement_label = file_quality.QUALITY_REQUIREMENT_LABELS.get(requirement) if requirement else None
    standard: dict[str, Any] = {
        "operator_label": OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
        # 审核要求原文就是「检查口径」列的业务原文；没有填写时明确标缺失。
        "requirement": criterion or None,
        "check_item": check_item,
        "check_item_label": check_label,
        "check_requirement": requirement,
        "check_requirement_label": requirement_label,
        "comparison_label": (
            f"{check_label or '文件检查'}：{requirement_label}" if check_label and requirement_label
            else (check_label or requirement_label)
        ),
        "standard_value": parameters.get("standard_value"),
        "threshold": parameters.get("standard_value"),
        "threshold_unit": parameters.get("unit"),
        "check_level": rule.get("check_level"),
        "risk_level": parameters.get("risk_level"),
    }
    standard["clear"] = bool(criterion)
    if not criterion:
        standard["missing"] = "该检查没有填写「检查口径」，判定标准缺少业务依据"
    return standard

def _quality_comparison_view(
    parameters: dict[str, Any],
    per_material: list[dict[str, Any]],
    material_names: dict[str, str],
) -> dict[str, Any] | None:
    """文件质量检查的实际比较：由**决定本次结论的那一份材料**给出。

    汇总结论（不合格优先、其次不可判定、全部合格才通过）出自某一份材料，
    比较过程就必须是那一份的真实取值。只给一个汇总结论会让工作人员无法核对
    「到底是哪份文件、拿什么和什么比」。

    已实现的三项检查各有真实的比较对象：
      文件大小  实际上传字节数 与 标准值换算出的字节上限；
      文件格式  实际上传的后缀 与 允许的格式集合；
      文件内容  同任务内当前上传文件的内容哈希是否重复。
    不可判定的检查没有比较对象，返回 None，页面只展示标准与原因。
    """
    check_item = parameters.get("check_item")
    applicable = [item for item in per_material if isinstance(item, dict) and item.get("status") != "NOT_APPLICABLE"]
    if not applicable:
        return None
    # 决定结论的那一份：不合格优先，其次不可判定，最后才是全部合格时的第一份。
    decisive = next(
        (item for item in applicable if item.get("status") in {"ABNORMAL", "EVIDENCE_INSUFFICIENT"}),
        applicable[0],
    )
    material_name = decisive.get("material_name") or material_names.get(decisive.get("material_key"), "未知材料")
    filename = decisive.get("filename")

    def label_of(field: str) -> str:
        return f"{material_name}·{field}"

    if check_item == CHECK_FILE_SIZE and decisive.get("limit_bytes") is not None:
        limit = parameters.get("standard_value")
        unit = parameters.get("unit") or ""
        actual = decisive.get("actual_bytes")
        return {
            "kind": "FILE_SIZE",
            "left_label": label_of("实际上传文件大小"),
            "left_value": f"{actual} 字节" if actual is not None else None,
            "right_label": label_of(f"允许上限（{limit}{unit}）"),
            "right_value": f"{decisive['limit_bytes']} 字节",
            "expression": (
                f"{actual} 字节 {'≤' if decisive.get('status') == 'PASSED' else '>'} {decisive['limit_bytes']} 字节"
                if actual is not None
                else None
            ),
            "threshold": f"{limit}{unit}",
            "mode_label": "文件大小不超过上限",
            "basis": f"判定文件：{filename}" if filename else None,
        }
    if check_item == CHECK_FILE_FORMAT:
        declared = decisive.get("declared_content_type")
        return {
            "kind": "FILE_FORMAT",
            "left_label": label_of("实际上传文件"),
            "left_value": filename,
            "right_label": label_of("允许的格式"),
            "right_value": parameters.get("standard_value"),
            "expression": decisive.get("message"),
            "mode_label": "上传文件格式属于允许集合",
            "basis": (
                f"声明的内容类型：{declared}；系统按上传声明的后缀与内容类型判定，不读取文件字节做内容鉴定"
                if declared
                else "上传未声明可核对的内容类型；系统按上传声明的后缀判定，不读取文件字节做内容鉴定"
            ),
        }
    if check_item == CHECK_FILE_DUPLICATE:
        duplicates = decisive.get("duplicates") or []
        return {
            "kind": "FILE_DUPLICATE",
            "left_label": label_of("文件内容"),
            "left_value": filename,
            "right_label": "同任务内其他当前上传文件",
            "right_value": "、".join(str(item) for item in duplicates) if duplicates else "无重复",
            "expression": "内容哈希重复" if duplicates else "内容哈希不重复",
            "mode_label": "同任务内当前上传文件内容不重复",
            "basis": "比对范围仅限同一任务内当前上传版本，不回看历史版本、不跨任务",
        }
    return None

def _execute_file_quality_rules(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    execution: NodeExecution,
) -> dict[str, Any]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    pairs = _current_uploads(db, task)
    documents_by_key = {document.material_key: document for document, _ in pairs}
    uploads_by_key = {document.material_key: upload for document, upload in pairs}
    uploaded_keys = {key for key, upload in uploads_by_key.items() if upload is not None}
    material_names = _material_names(manifest)
    gap_by_rule_key = {item.get("rule_key"): item for item in manifest.get("capability_gaps") or []}
    status_counts: dict[str, int] = {}
    rules = [
        rule
        for rule in manifest["rules"]
        if rule["node"] == execution.node_code
        and rule_node_type_of(rule) == "DOCUMENT_PREPARE"
        and rule["operator"] == "FILE_QUALITY_CHECK"
        and rule["rule_key"] in enabled_keys
    ]
    for rule in rules:
        # 每条规则先取自己的参数：下面有多个「不适用 / 缺输入 / 材料类型待复核」的提前分支，
        # 它们同样要把本条规则的检查项目与判据写进结果。沿用上一条规则的参数会把
        # 两条不同检查的条件混在一起，首条规则时更是直接读取未绑定的名字。
        params = rule.get("parameters") or {}
        check_item = params.get("check_item")
        standard_value = params.get("standard_value")
        unit = params.get("unit")
        artifact_type = params.get("artifact_type")
        if not isinstance(artifact_type, str) or not artifact_type:
            raise BusinessError("RULE_PARAMETER_MISSING", "文件质量规则必须明确声明处理产物类型", details={"rule": rule["name"]})
        state = _applicability_state(rule, uploaded_keys)
        scoped_uploads = [uploads_by_key.get(material_key) for material_key in rule["materials"]]
        # 只有**人工已确认与材料位不符**的材料位才是「依据不可用」。类型待复核来自已下线为
        # 仅供参考的自动识别（或人工的“证据不足”），不是对文件内容的否定结论：
        # 文件格式/大小/重复判定依据的是真实上传记录，照常判定；类型本身作为待确认事项另行说明。
        rejected_material_keys = [
            material_key
            for material_key in rule["materials"]
            if documents_by_key.get(material_key) is not None
            and read_material_type_status(documents_by_key[material_key].status) == MATERIAL_TYPE_REJECTED
        ]
        review_material_keys = [
            material_key
            for material_key in rule["materials"]
            if documents_by_key.get(material_key) is not None
            and read_material_type_status(documents_by_key[material_key].status) == MATERIAL_TYPE_REVIEW
        ]
        material_type_notice = (
            "本次检查所依据的材料（"
            + "、".join(material_names.get(key, key) for key in review_material_keys)
            + "）类型待复核；检查结论按真实上传记录得出，材料类型本身仍需人工确认。"
            if review_material_keys
            else None
        )
        quality_artifacts: list[StoredArtifact] = []
        if not state["applicable"]:
            result = {"status": "NOT_APPLICABLE", "message": _not_applicable_message(state, material_names)}
        elif rejected_material_keys:
            rejected = "、".join(material_names.get(key, key) for key in rejected_material_keys)
            result = {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": f"材料类型已被人工确认与材料位不符（{rejected}），文件质量检查缺少有效依据，不能判定通过",
            }
        elif not any(scoped_uploads):
            # 始终适用的质量检查缺少输入材料时是缺输入，不是通过。
            result = {"status": "EVIDENCE_INSUFFICIENT", "message": "适用材料尚未上传，文件质量检查缺少输入，不能判定通过"}
        else:
            # 每个适用材料分别判定：一条检查涉及多份材料时保留逐份结果，
            # 存在不合格即汇总为不合格；无不合格但有不可判定则汇总为不可判定。
            # 去重比对对象取同任务全部当前上传，按上传 ID 排除自身。
            peers = [
                {
                    "id": item.id,
                    "sha256": item.sha256,
                    "material_key": key,
                    "label": material_names.get(key, key),
                }
                for key, item in uploads_by_key.items()
                if item is not None
            ]
            per_material: list[dict[str, Any]] = []
            for material_key, upload in zip(rule["materials"], scoped_uploads):
                if not upload:
                    # 非必传材料缺失由材料检查负责，这里如实记为不适用，不当作通过。
                    per_material.append(
                        {
                            "material_key": material_key,
                            "material_name": material_names.get(material_key, material_key),
                            "status": "NOT_APPLICABLE",
                            "message": "该材料未上传，本检查不适用",
                        }
                    )
                    continue
                # 判定走共享领域服务：Agent 工具调的是同一个函数，
                # 节点与工具不可能对同一份文件得出不同结论。
                recorded = quality_judgement.judge_quality_item(
                    check_item,
                    upload=upload,
                    standard_value=standard_value,
                    unit=unit,
                    peers=peers,
                    gap=gap_by_rule_key.get(rule["rule_key"]) or {},
                )
                per_material.append(
                    {
                        "material_key": material_key,
                        "material_name": material_names.get(material_key, material_key),
                        "filename": upload.original_filename,
                        **recorded,
                    }
                )
                artifact = db.scalar(
                    select(StoredArtifact).where(
                        StoredArtifact.document_upload_id == upload.id,
                        StoredArtifact.artifact_type == artifact_type,
                        StoredArtifact.upload_sha256 == upload.sha256,
                        StoredArtifact.status == "READY",
                    )
                )
                if artifact:
                    quality_artifacts.append(artifact)
            # 汇总口径与填写规范一致：不合格优先，其次不可判定，全部适用结果合格才通过。
            # 走共享领域服务，节点与工具共用同一份汇总口径。
            statuses = [item["status"] for item in per_material]
            result = quality_judgement.summarize_per_material(per_material)
            result["per_material"] = per_material
            result["status_counts"] = {value: statuses.count(value) for value in sorted(set(statuses))}
        per_material_rows = result.get("per_material", [])
        # 不可判定的检查没有比较对象：缺键即「本次没有比较过程」，
        # 不写 None，避免页面把「无比较过程」和「漏记」混为一谈。
        quality_comparison = _quality_comparison_view(params, per_material_rows, material_names)
        record = RuleResult(
            audit_task_id=task.id,
            node_execution_id=execution.id,
            rule_key=rule["rule_key"],
            rule_name=rule["name"],
            status=result["status"],
            message=result["message"],
            result_json={
                "artifact_type": artifact_type,
                "artifact_nos": [artifact.artifact_no for artifact in quality_artifacts],
                "check_item": check_item,
                "standard_value": standard_value,
                "unit": unit,
                # 逐份材料的判定结果：多材料规则必须保留每份的结果，不能只留一个汇总结论。
                "per_material": per_material_rows,
                "material_status_counts": result.get("status_counts", {}),
                # 材料归属：报告按实际涉及的材料筛选检查结果，文件检查没有字段输入，
                # 归属只能来自逐份材料的判定结果，不能留空。
                "inputs": _quality_material_inputs(per_material_rows, material_names),
                # 审核标准：与字段类规则同一闭环。检查项目、合格要求、标准值/单位与检查口径
                # 全部取自已确认配置；缺「检查口径」时如实标 clear=false，不代填默认条件。
                "standard": _quality_standard_view(rule, params),
                # 实际比较：由决定本次结论的那一份材料给出，说明查了哪份文件、比了什么。
                **({"comparison": quality_comparison} if quality_comparison else {}),
                "applicability": _applicability_json(state),
                # 材料类型待复核时的事实说明：结论照真实上传记录得出，类型本身仍需人工确认。
                **({"material_type_notice": material_type_notice} if material_type_notice else {}),
                **{key: value for key, value in result.items() if key in {"capability_gap", "capability_message"}},
            },
        )
        db.add(record)
        db.flush()
        evidence_numbers = _copy_artifact_evidence_to_rule(db, task, record, quality_artifacts)
        record.result_json = {**(record.result_json or {}), "evidence_nos": evidence_numbers}
        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1
    return {"file_quality_rule_count": len(rules), "file_quality_status_counts": status_counts}
