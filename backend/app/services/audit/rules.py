"""规则执行：按能力标识分发，产出带标准、输入、比较过程与证据的规则结果。

分发依据是**能力目录**（`domain.capabilities`）的 `execution` 字段，不是算子码分支：

- 能力不在目录中 → 明确报错；
- 能力 `implemented=false` → 返回「能力未实现」并附缺口，**不**默认通过、**不**挂人工待办
  （人工没有可核对的依据，判成正常会给一条从未执行的检查伪造通过结论）；
- 执行方式未知 → 明确报错，不落到别的分支。

风险规则会把**全部**来源规则的当前有效结果带入，只传第一条会让「全部来源均不合格」
被静默放宽成任一。主体/签章一致性不做程序判定：检索只返回候选，结论由人工确认。"""

from ...domain import capabilities
from ...domain.identity import public_no
from ...domain.manifest import EVIDENCE_RETRIEVAL, EVIDENCE_SEAL_SUBJECT, rule_node_type_of
from ...common.exceptions import BusinessError
from ...entity import AuditTask, Evidence, ManualReview, NodeExecution, RuleResult, StoredArtifact
from ...operators import execute_operator
from ..llm_extraction import validate_fields_with_llm
from ..rag_service import build_task_index, retrieve_evidence
from .artifacts import _artifact_payload
from .evidence import _copy_evidence_to_rule, _rule_evidence_snapshots
from .facts import _comparison_view, _field_inputs, _input_summary, _rule_standard_view
from .shared import SUBJECT_ARTIFACT_SOURCE, _applicability_json, _applicability_state, _current_uploads, _is_subject_rule, _material_input_problem, _material_names, _material_type_review_notice, _not_applicable_message, _subject_query, _subject_type_review_notice, _uploaded_material_keys
from .state import _current_execution_ids, _result_material_keys
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


def _retrieval_summary(retrieval: dict[str, Any] | None) -> dict[str, Any] | None:
    """检索结果的业务摘要：范围、状态、候选与被采用的证据。

    相似度只作为排序依据展示；不设置“超过某相似度即通过”的判断，
    也不把候选条数当成证据完整性。

    候选的完整身份（index_no / chunk_no / source_entry_no）必须一并保留：
    人工复核要按身份选择候选，快照里缺身份就无法说明“人工依据的是哪一条”。
    """
    if not isinstance(retrieval, dict):
        return None
    adopted = retrieval.get("adopted") if isinstance(retrieval.get("adopted"), list) else []
    candidates = retrieval.get("candidates") if isinstance(retrieval.get("candidates"), list) else []
    return {
        "retrieval_no": retrieval.get("retrieval_no"),
        "status": retrieval.get("status"),
        "message": retrieval.get("message"),
        "candidate_count": len(candidates),
        "uncovered_materials": retrieval.get("uncovered_materials") or [],
        "excluded_indexes": retrieval.get("excluded_indexes") or [],
        "candidates": [_candidate_summary(item) for item in candidates],
        "adopted": [_candidate_summary(item) for item in adopted],
    }

def _candidate_summary(item: Any) -> dict[str, Any]:
    """一条候选的业务身份与内容摘要，供人工复核快照与页面展示复用。"""
    if not isinstance(item, dict):
        return {}
    return {
        "index_no": item.get("index_no"),
        "chunk_no": item.get("chunk_no"),
        "chunk_id": item.get("chunk_id"),
        "source_entry_no": item.get("source_entry_no"),
        "subject_name": item.get("subject_name"),
        "artifact_type": item.get("artifact_type"),
        "text": item.get("text"),
        "page_no": item.get("page_no"),
        "page_no_source": item.get("page_no_source"),
        "heading_path": item.get("heading_path"),
        "char_start": item.get("char_start"),
        "char_end": item.get("char_end"),
        "material_key": item.get("material_key"),
        "material_name": item.get("material_name"),
        "filename": item.get("filename"),
        "similarity": item.get("similarity"),
    }

def _retrieval_material_keys(
    rule: dict[str, Any], *, subject_retrieval: bool, artifact_source_by_field: dict[str, str | None]
) -> list[str]:
    inputs = list(rule.get("inputs") or [])
    if subject_retrieval:
        seal_inputs = [
            item for item in inputs
            if artifact_source_by_field.get(item["field_key"]) == SUBJECT_ARTIFACT_SOURCE
        ]
        # 历史主体规则没有字段产物来源声明时，沿用其已冻结的输入材料范围。
        inputs = seal_inputs or inputs
    return list(dict.fromkeys([
        *(rule.get("materials") or []),
        *(item["material_key"] for item in inputs),
    ]))

def _build_index_if_needed(db: Session, task: AuditTask, manifest: dict[str, Any]) -> dict[str, Any]:
    """只有本任务启用了**需要检索取证**的规则时才建立证据索引。

    明确的字段算术规则（金额加总、日期先后、字段比较）走直接取数，不需要向量检索：
    它们不应因为 Embedding 不可用而增加依赖，也不应让文件处理节点整体失败。
    确实需要检索的规则在运行时若缺模型或索引，会得到明确错误，而不是空结果。
    """
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    subject_rules = [
        rule for rule in manifest["rules"]
        if rule["rule_key"] in enabled_keys and _is_subject_rule(rule)
    ]
    text_rules = [
        rule for rule in manifest["rules"]
        if rule["rule_key"] in enabled_keys
        and isinstance(rule.get("route"), dict)
        and rule["route"].get("evidence") == EVIDENCE_RETRIEVAL
    ]
    selected_rules = [*subject_rules, *text_rules]
    if not selected_rules:
        return {
            "enabled": False,
            "reason": "本任务没有需要检索取证的规则，本次未建立证据索引",
        }
    indexes = []
    artifact_source_by_field = {
        item["field_key"]: item.get("artifact_source") for item in manifest["fields"]
    }
    for artifact_type, rules in (("SEAL", subject_rules), ("OCR", text_rules)):
        if not rules:
            continue
        material_keys = list(dict.fromkeys(
            material_key
            for rule in rules
            for material_key in _retrieval_material_keys(
                rule,
                subject_retrieval=artifact_type == "SEAL",
                artifact_source_by_field=artifact_source_by_field,
            )
        ))
        indexes.append(build_task_index(db, task, material_keys=material_keys, artifact_types=(artifact_type,)))
    return {
        "enabled": True,
        "triggered_by_rules": [rule["rule_key"] for rule in selected_rules],
        "index_version": indexes[0]["index_version"],
        "model_id": indexes[0]["model_id"],
        "model_revision": indexes[0]["model_revision"],
        "built": [item for index in indexes for item in index["built"]],
        "reused": [item for index in indexes for item in index["reused"]],
        "skipped": [item for index in indexes for item in index["skipped"]],
    }

def _seal_records(
    db: Session, task: AuditTask, rule: dict[str, Any], artifact_source_by_field: dict[str, str | None]
) -> list[dict[str, Any]]:
    """规则材料范围内**全部**印章识别记录：不隐藏不相似或冲突的条目。

    N 条识别主体与 M 条合同主体在库里各自独立保留，本函数只做展示，
    不合并成一个确定主体，也不按相似度挑选“最匹配”的那条。

    每条记录带完整身份（产物号 + 条目序号）：人工判断要能指回具体是哪一枚印章，
    只给主体名称无法核对其原始出处。
    """
    material_keys = set(_retrieval_material_keys(
        rule, subject_retrieval=True, artifact_source_by_field=artifact_source_by_field
    ))
    records: list[dict[str, Any]] = []
    for document, upload in _current_uploads(db, task):
        if upload is None or document.material_key not in material_keys:
            continue
        artifacts = db.scalars(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id == upload.id,
                StoredArtifact.upload_sha256 == upload.sha256,
                StoredArtifact.artifact_type == "SEAL",
                StoredArtifact.status == "READY",
            )
        ).all()
        for artifact in artifacts:
            content = _artifact_payload(artifact).content
            for entry in _seal_subject_entries(content):
                records.append(
                    {
                        "material_key": document.material_key,
                        "material_name": document.material_name,
                        "filename": upload.original_filename,
                        "artifact_no": artifact.artifact_no,
                        **entry,
                    }
                )
    return records

def _seal_subject_entries(content: Any) -> list[dict[str, Any]]:
    """印章产物的主体条目（新结构优先，旧结构按下标读取）。

    只取主体名称与归属定位；识别类型、坐标与置信度只作为原始事实附带，
    不参与主体是否一致的判断。
    """
    if not isinstance(content, dict):
        return []
    items = content.get("seal_subjects")
    if isinstance(items, list):
        source = [
            item
            for item in items
            if isinstance(item, dict)
        ]
    else:
        pages = content.get("pages")
        if not isinstance(pages, list):
            return []
        source = [
            {
                "entry_no": entry_no,
                "page_no": page.get("page_no"),
                "seal_type": seal.get("seal_type") or seal.get("class_name"),
                "subject_name": seal.get("subject_name") or seal.get("text"),
                "confidence": seal.get("confidence"),
            }
            for page in pages
            if isinstance(page, dict)
            for entry_no, seal in enumerate(page.get("seals") or [], start=1)
            if isinstance(seal, dict)
        ]
    return [
        {
            "page_no": item.get("page_no"),
            "entry_no": item.get("entry_no"),
            # 识别到的主体名称原样保留：识别名称不等于已确认的主体。
            "subject_name": item.get("subject_name"),
            "seal_type": item.get("seal_type"),
            "confidence": item.get("confidence"),
        }
        for item in source
    ]

def _rule_retrieval(
    db: Session,
    task: AuditTask,
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    artifact_source_by_field: dict[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """为一条语义规则在**本任务、本规则材料范围**内检索证据。

    取证方式与材料范围在编译期已冻结在规则的 route 与 materials 里：运行时只按它们分派，
    不让大模型临时决定路由或扩大材料范围。检索失败（模型缺失、索引损坏等）抛出明确错误，
    不返回空结果或默认成功。

    `inputs` 必须是**本次执行实际取得**的字段输入：待核对主体的当前有效值只能从这里取，
    不能另取一份或用空输入兜底——那会让检索查的对象与这条规则无关。
    """
    route = rule.get("route") if isinstance(rule.get("route"), dict) else {}
    subject_retrieval = route.get("evidence") == EVIDENCE_SEAL_SUBJECT or _is_subject_rule(rule)
    text_retrieval = route.get("evidence") == EVIDENCE_RETRIEVAL
    if not subject_retrieval and not text_retrieval:
        return None
    material_keys = _retrieval_material_keys(
        rule,
        subject_retrieval=subject_retrieval,
        artifact_source_by_field=artifact_source_by_field or {},
    )
    if not material_keys:
        raise BusinessError(
            "RETRIEVAL_SCOPE_EMPTY",
            f"规则“{rule['name']}”需要检索取证，但没有可用的材料范围，本次检索未执行",
            details={"rule": rule["name"]},
        )
    if subject_retrieval:
        query_text = _subject_query(rule, inputs, artifact_source_by_field=artifact_source_by_field)
    else:
        query_text = str((rule.get("parameters") or {}).get("instruction") or "").strip()
        if not query_text:
            raise BusinessError("RETRIEVAL_QUERY_EMPTY", "语义规则缺少可用于正文检索的审核要求", details={"rule": rule["name"]})
    return retrieve_evidence(
        db,
        task,
        query_text=query_text,
        material_keys=material_keys,
        rule_key=rule["rule_key"],
        node_code=rule.get("node"),
        artifact_type="SEAL" if subject_retrieval else "OCR",
    )

def _capability_result_payload(rule: dict[str, Any]) -> dict[str, Any]:
    """本条结果由哪项能力判出：标识、版本、已实现状态与未实现原因。

    它进入每条规则结果的 `capability` 键，与 `route`（取证/判定方式）一起构成
    「这条结论是怎么来的」。未知能力如实记为 known=False，不伪装成已实现。
    """
    capability_id = capabilities.capability_id_of_rule(rule)
    capability = capabilities.capability_of(capability_id)
    if capability is None:
        return {
            "capability_id": capability_id,
            "label": None,
            "version": None,
            "implemented": False,
            "known": False,
            "unimplemented_reason": "该能力不在能力目录中",
        }
    return {
        "capability_id": capability.capability_id,
        "label": capability.label,
        "version": capability.version,
        "implemented": capability.implemented,
        "known": True,
        "unimplemented_reason": capability.unimplemented_reason or None,
    }

def _unimplemented_capability_result(
    capability: capabilities.Capability,
    rule: dict[str, Any],
) -> dict[str, Any]:
    """引用的能力当前没有判定实现时的统一结果。

    这不是「证据不足」，也不是「通过」：它是「能力未实现」这一独立事实。
    状态取 EVIDENCE_INSUFFICIENT 是因为规则确实没有形成结论，但 `capability_gap`
    与 `blocked_reason=CAPABILITY_UNIMPLEMENTED` 让页面与报告能把它与普通的证据不足
    分开显示，不会读成「系统查过了但材料里没有」。

    输入里已有的原文如实带出：缺的是判定实现，不是内容。
    """
    return {
        "status": "EVIDENCE_INSUFFICIENT",
        "message": (
            f"本项引用的能力“{capability.label}”当前没有判定实现，系统不能给出结论："
            f"{capability.unimplemented_reason or '未说明原因'}"
        ),
        "capability_gap": capability.capability_id,
        "capability_message": capability.unimplemented_reason,
        # 无法判断的具体原因：能力未实现，与「标准不明确」「缺少原文」是三种不同的缺什么。
        "blocked_reason": "CAPABILITY_UNIMPLEMENTED",
        "unimplemented_capability": _capability_result_payload(rule),
    }

def _risk_source_result_keys(rule: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    keys = list(dict.fromkeys(rule.get("source_rule_keys") or []))
    if rule.get("source_rule_key") and rule["source_rule_key"] not in keys:
        keys.insert(0, rule["source_rule_key"])
    for code in rule.get("source_rule_codes") or []:
        calculations = [
            item for item in manifest.get("calculations") or []
            if item.get("result_code") == code and item.get("status") != "DISABLED"
        ]
        fields = [item for item in manifest["fields"] if item.get("source_ref") == code]
        if len(calculations) + len(fields) != 1:
            raise BusinessError(
                "AUDIT_RISK_SOURCE_INVALID",
                "风险来源编号无法唯一定位计算结果或字段",
                details={"rule": rule["name"], "source_code": code},
            )
        if calculations:
            source_key = calculations[0]["rule_code"]
        else:
            raise BusinessError("AUDIT_RISK_FIELD_SOURCE_UNJUDGED", "字段抽取结果没有合格/不合格判定，不能作为风险来源", details={"rule": rule["name"], "source_code": code})
        if source_key not in keys:
            keys.append(source_key)
    if not keys:
        raise BusinessError("AUDIT_RISK_SOURCE_INVALID", "风险规则没有可执行的来源", details={"rule": rule["name"]})
    return keys


def _execute_rules(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    execution: NodeExecution,
    node_code: str,
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行一组规则并写规则结果。

    `rules` 为空表示「按节点取本任务启用的全部规则」——这是确定性路径的用法。
    显式传入规则清单时按传入的清单执行：Agent 路径用它执行**本节点里没有授权工具、
    因而仍由确定性实现负责**的那部分规则，两类规则因此不会重复写同一份结果。
    """
    if rules is None:
        enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
        rules = [rule for rule in manifest["rules"] if rule["node"] == node_code and rule["rule_key"] in enabled_keys]
    if not rules:
        # 模板允许不配置某类检查（例如单合同场景没有交叉校验、或未配置风险评级）。
        # 这里显式跳过并说明原因，绝不等价于该检查通过。
        return {
            "rule_count": 0,
            "status_counts": {},
            "check_configured": False,
            "skipped": True,
            "skip_reason": "本模板未配置此类检查",
        }
    counts: dict[str, int] = {}
    field_definitions = {item["field_key"]: item for item in manifest["fields"]}
    # 字段的「产物来源」声明：主体核对据此确定待核对主体取自哪个字段。
    artifact_source_by_field = {
        item["field_key"]: item.get("artifact_source") for item in manifest["fields"]
    }
    material_names = _material_names(manifest)
    uploaded_keys = _uploaded_material_keys(db, task)
    current_execution_ids = _current_execution_ids(db, task)
    # 来源规则只取当前有效执行批次：失效批次与旧上传版本的结果不能成为风险结论的依据。
    prior_results = (
        {
            item.rule_key: item
            for item in db.scalars(
                select(RuleResult)
                .where(
                    RuleResult.audit_task_id == task.id,
                    RuleResult.node_execution_id.in_(current_execution_ids),
                    RuleResult.status != "NOT_EXECUTED",
                )
                .order_by(RuleResult.id)
            ).all()
        }
        if current_execution_ids
        else {}
    )
    abnormal = 0
    for rule in rules:
        state = _applicability_state(rule, uploaded_keys)
        if not state["applicable"]:
            # 已配置规则但适用条件明确不成立：记录不适用并保留原因，不执行、也不标成通过。
            db.add(
                RuleResult(
                    audit_task_id=task.id,
                    node_execution_id=execution.id,
                    rule_key=rule["rule_key"],
                    rule_name=rule["name"],
                    status="NOT_APPLICABLE",
                    message=_not_applicable_message(state, material_names),
                    result_json={"applicability": _applicability_json(state)},
                )
            )
            counts["NOT_APPLICABLE"] = counts.get("NOT_APPLICABLE", 0) + 1
            continue
        inputs = _field_inputs(db, task, rule)
        parameters = dict(rule.get("parameters", {}))
        risk_source_keys = _risk_source_result_keys(rule, manifest) if rule["operator"] == "RISK_TRIGGER" else []
        retrieval: dict[str, Any] | None = None
        # 材料类型待复核不是「依据不可用」：真实原文照常参与判定，但类型这件事尚未由人定案，
        # 结果里要带着这条事实说明，不能让人把结论读成“材料类型已经没问题”。
        # 输入为空（例如风险规则）时按规则的材料范围另取一次，说明该材料位的类型状态。
        type_review_notice = _material_type_review_notice(inputs, manifest) or (
            _subject_type_review_notice(db, task, rule) if not inputs else None
        )
        # 执行分发按**能力标识**进行，不按算子码逐个分支：
        #   算子是否存在、当前是否已实现，都由能力目录回答；未实现的能力在这里明确返回
        #   「不可判定」并附能力缺口，绝不落到别的分支、也绝不静默跳过。
        #   能力参数与输入数量已在编译期校验，运行时只按能力声明的输入形态取输入。
        capability = capabilities.capability_of(capabilities.capability_id_of_rule(rule))
        if capability is None:
            raise BusinessError(
                "CAPABILITY_NOT_FOUND",
                f"规则“{rule['name']}”引用的能力当前不在能力目录中，无法执行",
                details={"rule": rule["name"], "operator": rule.get("operator")},
            )
        if not capability.implemented:
            # 配置合法但能力未实现：这是「能力未实现」，不是「证据不足」，也不是通过。
            result = _unimplemented_capability_result(capability, rule)
        elif capability.execution == capabilities.EXEC_SUBJECT_RETRIEVAL:
            # 主体/签章一致性：用待核对主体的当前有效值检索相似印章主体，返回候选，
            # 由工作人员确认。相似度不构成结论，因此这里**不判合格/不合格**，
            # 而是形成一条人工复核待办并把候选一并附上。
            material_problem = _material_input_problem(inputs)
            if material_problem is not None:
                result = material_problem
            elif not any(item.get("status") in {"PRESENT", "REVIEWED"} for item in inputs):
                result = {
                    "status": "EVIDENCE_INSUFFICIENT",
                    "message": "待核对主体尚无有效取值，本次未执行主体核对",
                    "blocked_reason": "NO_SUBJECT_VALUE",
                }
            else:
                retrieval = _rule_retrieval(
                    db,
                    task,
                    rule,
                    inputs,
                    artifact_source_by_field=artifact_source_by_field,
                )
                candidates = retrieval.get("candidates") or []
                if retrieval.get("status") != "OK" or not candidates:
                    # 没有可用候选（尚未识别主体、尚未建索引或索引失效）时不得判合格：
                    # 明确记录无法判断，并说明具体原因，交人工处理。
                    result = {
                        "status": "EVIDENCE_INSUFFICIENT",
                        "message": retrieval.get("message") or "没有可用的印章主体候选，无法判断主体是否一致",
                        "blocked_reason": "SUBJECT_CANDIDATE_MISSING",
                    }
                else:
                    result = {
                        "status": "PENDING_REVIEW",
                        "message": (
                            f"已返回 {len(candidates)} 条印章主体候选，相似度只用于排序；"
                            "请确认一致、不一致或证据不足"
                        ),
                    }
        elif capability.execution == capabilities.EXEC_LLM_VALIDATE:
            # 语义判断同样受材料依据约束：材料被人工确认不符或尚未处理时不能交给模型判通过。
            # 判定口径与比较算子保持一致（算子层由 _unavailable_status 负责，这里补语义判断这条路径）。
            material_problem = _material_input_problem(inputs)
            if material_problem is not None:
                result = material_problem
            elif any(
                item.get("status") not in {"PRESENT", "REVIEWED"}
                or item.get("normalized_value") is None
                for item in inputs
            ):
                # 语义判断的每个声明输入都必须是当前有效事实。不能因为其中一个字段
                # 有值就把其它 EVIDENCE_UNCLEAR / NORMALIZATION_FAILED 候选一并交给 LLM。
                result = {
                    "status": "EVIDENCE_INSUFFICIENT",
                    "message": (
                        "规则输入没有全部形成有效字段事实，本次未执行语义判断："
                        + "、".join(
                            f"{item['field_key']}@{item['material_key']}（{item.get('status')}）" for item in inputs
                        )
                    ),
                    "blocked_reason": "INCOMPLETE_FIELD_FACTS",
                    "missing_inputs": [
                        f"{item['field_key']}@{item['material_key']}"
                        for item in inputs
                        if item.get("status") not in {"PRESENT", "REVIEWED"}
                        or item.get("normalized_value") is None
                    ],
                }
            else:
                # 正文检索只在规则声明的材料范围内使用当前 OCR 产物；候选会连同
                # 字段事实交给模型。空候选不等于原文不存在。
                retrieval = _rule_retrieval(db, task, rule, inputs)
                if retrieval is None:
                    raise BusinessError("RETRIEVAL_ROUTE_MISSING", "语义判断规则缺少正文检索路由", details={"rule": rule["name"]})
                if retrieval.get("status") != "OK" or not retrieval.get("adopted"):
                    result = {
                        "status": "EVIDENCE_INSUFFICIENT",
                        "message": retrieval.get("message") or "本次材料范围内没有可引用的 OCR 原文，无法执行语义判断",
                        "blocked_reason": "OCR_EVIDENCE_MISSING",
                    }
                else:
                    result = validate_fields_with_llm(rule, inputs, retrieval=retrieval)
        elif capability.execution == capabilities.EXEC_RISK:
            # 风险可能关联多条来源检查（任一 / 全部）：把**全部**来源的当前有效结果交给算子，
            # 只传第一条会让「全部关联检查不合格」被静默放宽成任一。
            parameters["source_statuses"] = [prior_results[key].status if key in prior_results else None for key in risk_source_keys]
            parameters["risk_condition"] = rule.get("risk_condition")
            result = execute_operator(rule["operator"], parameters, inputs)
        elif capability.execution == capabilities.EXEC_OPERATOR:
            # 比较、非空、条款存在性、大小写金额一致性：统一走算子注册表。
            # 未实现的能力已由上面的 implemented 判定拦下，不会走到这里。
            result = execute_operator(rule["operator"], parameters, inputs)
        else:
            raise BusinessError(
                "CAPABILITY_EXECUTION_UNKNOWN",
                f"能力“{capability.label}”声明的执行方式当前没有对应的执行实现",
                details={
                    "rule": rule["name"],
                    "capability_id": capability.capability_id,
                    "execution": capability.execution,
                },
            )
        # 结果必须带上是哪项能力判出来的。上方按能力标识分发的那些分支在这里统一补上；
        # 下面按 execution 分发的分支（主体检索、语义判断、风险）各自补自己的。
        if isinstance(result, dict) and "capability" not in result:
            result["capability"] = _capability_result_payload(rule)
        # 风险结果附带来源规则与处理建议：风险列表要显示“来源规则、依据、处理建议”，
        # 这里把执行时刻的真实来源名称与配置的建议原文一并保存，不靠前端再猜。
        risk_context: dict[str, Any] = {}
        if rule["operator"] == "RISK_TRIGGER":
            # 写入**全部**来源规则的实际结果：风险列表与报告要能说明每条来源当时判成了什么。
            risk_context["source_rules"] = [
                {
                    "rule_key": prior_results[key].rule_key,
                    "rule_name": prior_results[key].rule_name,
                    "status": prior_results[key].status,
                }
                for key in risk_source_keys
                if key in prior_results
            ]
            # 风险实际涉及的材料：沿当前有效来源检查实例取得，不直接套用规则声明的所有材料。
            # 报告要按这条风险真实发生的材料归属，而不是按配置里写得最全的范围。
            risk_context["source_instances"] = [
                {
                    "rule_key": prior_results[key].rule_key,
                    "rule_name": prior_results[key].rule_name,
                    "status": prior_results[key].status,
                    "material_key": material_key,
                }
                for key in risk_source_keys
                if key in prior_results
                for material_key in sorted(_result_material_keys(prior_results[key]))
            ]
            source_context = prior_results.get(risk_source_keys[0]) if risk_source_keys else None
            if source_context is not None:
                # 兼容旧读取路径：第一条来源规则同时以单值形式给出。
                risk_context["source_rule_key"] = source_context.rule_key
                risk_context["source_rule_name"] = source_context.rule_name
            if isinstance(parameters.get("action"), str) and parameters["action"]:
                risk_context["action"] = parameters["action"]
            if rule.get("risk_condition"):
                risk_context["risk_condition"] = rule["risk_condition"]
        # 本次执行的输入摘要：原文值与标准化值分开，供结果、对比过程与页面共用同一份事实。
        input_summary = _input_summary(inputs, field_definitions, material_names)
        record = RuleResult(
            audit_task_id=task.id,
            node_execution_id=execution.id,
            rule_key=rule["rule_key"],
            rule_name=rule["name"],
            status=result["status"],
            message=result["message"],
            result_json={
                **result,
                **risk_context,
                "applicability": _applicability_json(state),
                "inputs": input_summary,
                # 本次比较的实际对象、取值与算式：页面与报告据此直接呈现对比过程，
                # 不必自行拼接字段名或重新计算差额。
                **({"comparison": _comparison_view(result.get("comparison"), input_summary)} if result.get("comparison") else {}),
                # 规则本身的审核标准原文（业务配置事实）：只给“系统判断”看不到判的是什么标准。
                "standard": _rule_standard_view(rule, parameters),
                # 材料类型待复核时的事实说明：结论照真实原文得出，类型本身仍需人工确认。
                **({"material_type_notice": type_review_notice} if type_review_notice else {}),
                # 本次实际使用的检索范围与结果：页面据此说明证据是从哪份材料的哪个位置取到的。
                **({"retrieval": _retrieval_summary(retrieval)} if retrieval else {}),
                # 取证与判定方式在编译期已冻结：结果里带上它，报告与页面可以说明这条是怎么判的。
                "route": rule.get("route") if isinstance(rule.get("route"), dict) else None,
            },
        )
        db.add(record)
        db.flush()
        if rule["operator"] == "RISK_TRIGGER":
            # 把**全部**来源规则结果的证据都带到风险上：报告要保留完整证据链，
            # 只复制第一条来源的引文会让其余来源的判断无法追溯。
            source_result_ids = [
                prior_results[key].id for key in risk_source_keys if key in prior_results
            ]
            evidence_ids = [
                item.id
                for item in db.scalars(
                    select(Evidence).where(
                        Evidence.audit_task_id == task.id,
                        Evidence.rule_result_id.in_(source_result_ids),
                    )
                ).all()
            ] if source_result_ids else []
        else:
            relevant_fields = set(result.get("evidence_field_keys", [])) if rule["operator"] == "LLM_VALIDATE" else None
            evidence_ids = [
                evidence_id
                for item in inputs
                if relevant_fields is None or item["field_key"] in relevant_fields
                for evidence_id in (
                    item.get("evidence_ids")
                    or ([item["evidence_id"]] if item.get("evidence_id") else [])
                )
            ]
        evidence_numbers = _copy_evidence_to_rule(db, task, record, evidence_ids)
        record.result_json = {**(record.result_json or {}), "evidence_nos": evidence_numbers}
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"] == "ABNORMAL":
            abnormal += 1
        # 能力未实现不是「待人裁决的业务问题」：人工既没有可核对的依据，也不应把它判成正常
        # （那会给一条从未执行的检查伪造出通过结论）。它由发布阶段的逐条确认承担，
        # 结果如实记为不可判定，不挂人工复核待办。
        unimplemented = result.get("blocked_reason") == "CAPABILITY_UNIMPLEMENTED"
        requires_review = result["status"] == "PENDING_REVIEW" or (
            result["status"] == "ABNORMAL" and parameters.get("requires_manual_review") is True
        )
        # 证据不足必须进入可操作的人工复核，而不是只留一个状态标签：
        # 主体冲突、归属不清、条款证据不足都属于要由人判断的情况，不能包装成无风险。
        if (
            not unimplemented
            and result["status"] == "EVIDENCE_INSUFFICIENT"
            and rule["operator"] in {
                "LLM_VALIDATE",
                "DECIMAL_CN_EQUAL",
                "FIELD_EQUAL",
            }
        ):
            requires_review = True
        # 归属按**能力类型**判断：`node_code` 是用户配置的实例码，拿它与 RISK_RATING
        # 比较在用户改了实例码之后恒为假，风险节点上会静默冒出本该被拦住的待复核项。
        if requires_review and rule_node_type_of(rule) == "RISK_RATING":
            raise BusinessError(
                "RISK_REVIEW_NODE_ORDER_INVALID",
                "风险评定位于人工复核节点之后，不能创建新的待复核项",
                details={"rule": rule["name"]},
                status_code=500,
            )
        if requires_review:
            review_evidences = _rule_evidence_snapshots(db, record.id)
            db.add(
                ManualReview(
                    review_no=public_no("REV"),
                    audit_task_id=task.id,
                    rule_result_id=record.id,
                    review_type="RULE_RESULT",
                    status="PENDING",
                    system_judgment_json={
                        "status": result["status"],
                        "message": result["message"],
                        "rule_key": rule["rule_key"],
                        "rule_result": dict(result),
                        "evidence": review_evidences[0] if review_evidences else None,
                        "evidences": review_evidences,
                        # 证据不足时把本次检索的真实范围与候选一并快照，
                        # 人工可以据此看到“系统查过哪些材料、找到了什么”，
                        # 也能按候选身份（索引号 / 分块号 / 条目序号）指回具体依据。
                        **({"retrieval": _retrieval_summary(retrieval)} if retrieval else {}),
                        # 主体/签章一致性检查：把双方原文与完整印章记录一并快照，
                        # 人工不需要再去别处找，也不必重新检索。
                        **({"seal_records": _seal_records(db, task, rule, artifact_source_by_field)} if _is_subject_rule(rule) else {}),
                        **({"subject_query": _subject_query_of(inputs, manifest)} if _is_subject_rule(rule) else {}),
                    },
                    business_note=result["message"],
                )
            )
    return {
        "rule_count": len(rules),
        "status_counts": counts,
        "check_configured": True,
        # 技术执行完成与业务异常分开表达：本节点已执行完成，但存在业务异常。
        "node_execution_completed": True,
        "business_abnormal_count": abnormal,
        "business_status": "ABNORMAL" if abnormal else "NO_ABNORMAL",
    }

def _subject_query_of(inputs: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any] | None:
    """主体核对规则本次使用的待核对主体原文。

    人工要同时看到“待核对主体”与“候选印章主体”才能判断是否一致；只给候选，
    复核时只能凭印象。这里如实取本次实际使用的那个字段值，取不到时返回 None。
    """
    artifact_source_by_field = {
        item["field_key"]: item.get("artifact_source") for item in manifest["fields"]
    }
    sources = artifact_source_by_field
    seal_flags = [sources.get(str(item.get("field_key") or "")) == SUBJECT_ARTIFACT_SOURCE for item in inputs]
    candidates = [item for item, is_seal in zip(inputs, seal_flags) if not is_seal] if any(seal_flags) and not all(seal_flags) else list(inputs)
    for item in candidates:
        if item.get("status") in {"PRESENT", "REVIEWED"} and item.get("normalized_value") is not None:
            value = str(item["normalized_value"]).strip()
            if value:
                field = next(
                    (entry for entry in manifest["fields"] if entry["field_key"] == item.get("field_key")),
                    {},
                )
                return {
                    "field_key": item.get("field_key"),
                    "field_name": field.get("name"),
                    "material_key": item.get("material_key"),
                    "material_name": _material_names(manifest).get(item.get("material_key"), item.get("material_key")),
                    "value": value,
                    "status": item.get("status"),
                }
    return None
