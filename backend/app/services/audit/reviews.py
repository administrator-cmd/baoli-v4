"""人工复核裁决与依赖结果的重算。

复核分批（不可打乱）：存在字段确认待办时本批**只**包含字段事实，否则包含全部待办。
判卷、提交与裁决必须用同一集合，否则用户会先裁决一条即将被字段纠错失效的规则。
材料类型待复核不参与这个分批——它是独立事项，捆进字段批次会让两条互不相干的业务问题
互相决定对方批次的哈希。

裁决后按「事实变化」的范围重算，不做无差别重跑：

- 字段事实变化 → 重算抽取规则结果，并失效内部校验及其后的结果与待办，退回重判；
- 材料类型定案 → 只就地重算文件质量结论（判定依据变了），**不**退回校验链路、
  不作废排队中的复核。

失效一律保留历史：旧批次标 INVALIDATED，旧事实与旧复核仍可追溯，不原地清空。"""

import hashlib
from ...domain.audit_vocab import MATERIAL_TYPE_REJECTED, MATERIAL_TYPE_REVIEW
from ...errors import BusinessError
from ...models import AuditTask, BackgroundOperation, DocumentUpload, ManualReview, NodeExecution, RuleResult, TaskDocument, TaskFieldValue
from ...schemas import ManualReviewRequest
from ..operation_types import OBJECT_AUDIT_TASK
from ..task_lookup import task_or_404
from .facts import _refresh_extraction_rule_results, _write_human_field_fact
from .quality import _execute_file_quality_rules, _sync_type_review_pending
from .shared import REVIEW_DECISION_LABELS, _normalize_field_value, _scenario, _task_field_definitions
from .state import _save_flow_checkpoint, _set_task_checkpoint, _task_field_value_of_current_upload, _validate_graph_namespace
from datetime import UTC, datetime
from decimal import InvalidOperation
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from typing import Any


def _review_hash(reviews: list[ManualReview]) -> str:
    value = ":".join(sorted(item.review_no for item in reviews))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _current_review_batch(db: Session, task: AuditTask) -> list[ManualReview]:
    """本批待办复核集合：先事实、后规则。

    存在字段确认（FIELD_CONFIRMATION）待办时，本批只包含字段事实，否则本批包含全部待办。
    判卷与完整性校验（prepare / submit / apply）必须用同一集合，否则用户会先裁决一条即将
    被字段纠错失效的规则。

    **材料类型待复核不参与这个分批**：它不再是「依据不可用」的阻断来源，而是一条独立的
    待确认事项。把它捆进字段批次会让两条互不相干的业务问题互相决定对方批次的哈希，
    也解释不了「为什么改材料类型会让我正在填的字段复核被作废」。

    这是**分批**口径而非一次性口径：一批提交后仍可能剩下规则结论待办，
    提交方据此重新取集合决定「继续复核本节点」还是「推进下一节点」——见
    `_apply_manual_reviews_direct` 的 remaining 判断。规则待办未处理完时不得推进。
    """
    pending = list(
        db.scalars(
            select(ManualReview)
            .where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
            .order_by(ManualReview.id)
        ).all()
    )
    if any(item.review_type == "FIELD_CONFIRMATION" for item in pending):
        return [item for item in pending if item.review_type == "FIELD_CONFIRMATION"]
    return pending

def _decision_command_no(db: Session, task: AuditTask) -> str | None:
    """本次裁决对应的后台命令编号。

    裁决由后台操作驱动：取该任务当前 RUNNING 的操作号用于对账；取不到就写 None，
    既不编造命令号，也不把最近一次已完成的操作号当成本次命令。
    """
    operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_type == OBJECT_AUDIT_TASK,
            BackgroundOperation.object_no == task.task_no,
            BackgroundOperation.status == "RUNNING",
        )
        .order_by(BackgroundOperation.id.desc())
    )
    return operation.operation_no if operation else None

def _allowed_review_decisions(review_type: str) -> set[str]:
    if review_type == "FIELD_CONFIRMATION":
        return {"CONFIRM_FIELD", "MODIFY_FIELD", "EVIDENCE_INSUFFICIENT"}
    if review_type in {"RULE_RESULT", "MATERIAL_TYPE_MISMATCH"}:
        return {"CONFIRM_ABNORMAL", "MARK_NORMAL", "EVIDENCE_INSUFFICIENT"}
    raise BusinessError("REVIEW_TYPE_INVALID", "人工复核类型不受支持", details={"review_type": review_type})

def _validate_review_payloads(
    db: Session,
    task: AuditTask,
    reviews: list[ManualReview],
    requested: dict[str, Any],
) -> dict[int, tuple[str, str | bool]]:
    field_definitions = _task_field_definitions(task)
    normalized_values: dict[int, tuple[str, str | bool]] = {}
    for review in reviews:
        decision = requested[review.review_no]
        if decision.decision not in _allowed_review_decisions(review.review_type):
            raise BusinessError("REVIEW_DECISION_INVALID", "当前复核项不能使用所选结论", details={"review_no": review.review_no})
        target_count = sum(
            value is not None
            for value in (review.document_upload_id, review.rule_result_id, review.field_value_id)
        )
        if target_count != 1:
            raise BusinessError("REVIEW_TARGET_INVALID", "人工复核缺少唯一的关联业务对象", status_code=500)
        if not review.field_value_id and decision.field_value is not None:
            raise BusinessError("REVIEW_FIELD_VALUE_UNEXPECTED", "非字段复核不能填写字段值")
        if review.field_value_id:
            fact = _task_field_value_of_current_upload(db, task, review.field_value_id)
            if not fact:
                raise BusinessError("REVIEW_FIELD_MISSING", "复核关联字段不存在", status_code=500)
            field = field_definitions.get(fact.field_key)
            if not field or field["material_key"] != fact.material_key:
                raise BusinessError("REVIEW_FIELD_SCHEMA_MISSING", "复核字段不在任务字段清单中", status_code=500)
            if decision.decision in {"CONFIRM_FIELD", "MODIFY_FIELD"}:
                if decision.field_value is None or not decision.field_value.strip():
                    raise BusinessError("REVIEW_FIELD_VALUE_REQUIRED", "确认或修改字段时必须填写确认后的字段值")
                try:
                    # 与自动抽取同一套口径：金额按字段声明的单位与币种校验原文写法。
                    normalized_values[review.id] = (
                        decision.field_value.strip(),
                        _normalize_field_value(
                            field["value_type"],
                            decision.field_value,
                            unit=field.get("unit"),
                            currency=field.get("currency"),
                        ),
                    )
                except (InvalidOperation, ValueError) as exc:
                    raise BusinessError(
                        "REVIEW_FIELD_VALUE_INVALID",
                        f"确认后的字段值不符合字段类型：{exc}",
                        details={
                            "field_name": field["name"],
                            "value_type": field["value_type"],
                            "unit": field.get("unit"),
                            "currency": field.get("currency"),
                        },
                    ) from exc
            elif decision.field_value is not None:
                raise BusinessError("REVIEW_FIELD_VALUE_UNEXPECTED", "证据不足结论不能填写字段值")
        if review.rule_result_id:
            result = db.get(RuleResult, review.rule_result_id)
            if not result or result.audit_task_id != task.id:
                raise BusinessError("REVIEW_RULE_MISSING", "复核关联规则不存在", status_code=500)
        if review.document_upload_id:
            upload = db.get(DocumentUpload, review.document_upload_id)
            document = db.get(TaskDocument, upload.task_document_id) if upload else None
            if not document or document.audit_task_id != task.id:
                raise BusinessError("REVIEW_DOCUMENT_MISSING", "复核关联材料不存在", status_code=500)
    return normalized_values

def _invalidate_file_quality_results(db: Session, task: AuditTask) -> dict[str, int]:
    """材料类型经人工确认变化后，失效文件质量检查的旧结论与待办。

    质量检查的判定依赖材料类型（类型待复核时它会明确记“缺少有效依据”）；
    类型一旦定案，旧结论的依据已经不再成立，必须重算，不能沿用到新批次。
    只失效这一节点的结果：材料识别与正文抽取的产物仍然有效，不重调供应商。
    重算由 `_recompute_file_quality_results` 在同一事务里紧接着完成——只失效不重算，
    下游报告与风险就会拿到“依据已不成立”的旧结论。
    """
    counts = {"invalidated_executions": 0, "invalidated_rule_results": 0, "invalidated_reviews": 0}
    execution_ids = list(
        db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code == "DOCUMENT_PREPARE",
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if not execution_ids:
        return counts
    rule_result_ids = list(
        db.scalars(select(RuleResult.id).where(RuleResult.node_execution_id.in_(execution_ids))).all()
    )
    if rule_result_ids:
        counts["invalidated_reviews"] = db.execute(
            update(ManualReview)
            .where(ManualReview.rule_result_id.in_(rule_result_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        ).rowcount or 0
    counts["invalidated_rule_results"] = len(rule_result_ids)
    counts["invalidated_executions"] = db.execute(
        update(NodeExecution)
        .where(NodeExecution.id.in_(execution_ids))
        .values(status="INVALIDATED")
    ).rowcount or 0
    db.flush()
    return counts

def _recompute_file_quality_results(db: Session, task: AuditTask, manifest: dict[str, Any]) -> dict[str, Any]:
    """按当前材料状态就地重算文件质量检查结论，产出新的有效结果批次。

    只重算质量规则本身：8018/8010/印章/OCR 的产物已按上传身份持久化为 READY，
    本函数不重新识别、不重新提交 OCR，也不接触任何外部服务。
    旧批次由 `_invalidate_file_quality_results` 标为 INVALIDATED 保留为历史；
    新批次挂在新的执行记录上（rule_result 的唯一键是「执行批次 + 规则键」，
    不能复用已失效批次的执行行），报告与风险据此取到当前有效的质量结论。
    """
    sequence_no = (db.scalar(select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task.id)) or 0) + 1
    now = datetime.now(UTC)
    execution = NodeExecution(
        audit_task_id=task.id,
        scenario_version_id=task.scenario_version_id,
        node_code="DOCUMENT_PREPARE",
        sequence_no=sequence_no,
        status="SUCCEEDED",
        started_at=now,
        finished_at=now,
        # 本批次不处理文件：如实写明只做了质量结论重算，且没有调用外部服务。
        # 不写 processed_documents，避免把「本批次处理了 0 份」伪装成文件处理结果。
        result_json={
            "recomputed": True,
            "recompute_reason": "材料类型经人工复核确认，文件质量检查的判定依据已改变，本次按当前材料状态重算质量结论",
            "recompute_scope": "FILE_QUALITY_CHECK",
            "external_services_called": False,
        },
    )
    db.add(execution)
    # 会话未启用 autoflush：必须先落盘（含调用方刚写入的材料类型状态），
    # 否则下面的质量判定读到的仍是旧材料状态，重算会得出与人工结论不符的结果。
    db.flush()
    summary = _execute_file_quality_rules(db, task, manifest, execution)
    execution.result_json = {**(execution.result_json or {}), **summary}
    db.flush()
    return {**summary, "sequence_no": execution.sequence_no}

def _invalidate_validation_results(db: Session, task: AuditTask) -> dict[str, int]:
    """字段有效值或材料依据改变后失效内部校验、交叉校验、风险与报告。

    旧批次的待复核项同时失效：新结果批次是否需要人工判断由新结果自己决定，
    旧复核项不能继续挂在待办里，也不能自动贴到新结果上。
    返回真实失效数量，供纠错接口如实回报影响范围。
    """
    counts = {"invalidated_executions": 0, "invalidated_rule_results": 0, "invalidated_reviews": 0}
    execution_ids = list(
        db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(["INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK_RATING", "REPORT_OUTPUT"]),
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if not execution_ids:
        return counts
    rule_result_ids = list(
        db.scalars(select(RuleResult.id).where(RuleResult.node_execution_id.in_(execution_ids))).all()
    )
    if rule_result_ids:
        counts["invalidated_reviews"] = db.execute(
            update(ManualReview)
            .where(ManualReview.rule_result_id.in_(rule_result_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        ).rowcount or 0
    counts["invalidated_rule_results"] = len(rule_result_ids)
    counts["invalidated_executions"] = db.execute(
        update(NodeExecution)

        .where(NodeExecution.id.in_(execution_ids))
        .values(status="INVALIDATED")
    ).rowcount or 0
    return counts

def _invalidate_retry_results(
    db: Session,
    task: AuditTask,
    nodes: list[dict[str, Any]],
    current_index: int,
) -> None:
    """重试前只失效被重试节点及其逻辑后继，不动更早的节点。"""
    affected_nodes = {item["node_code"] for item in nodes[current_index:]}
    execution_ids = list(
        db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(affected_nodes),
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if execution_ids:
        rule_result_ids = list(
            db.scalars(select(RuleResult.id).where(RuleResult.node_execution_id.in_(execution_ids))).all()
        )
        if rule_result_ids:
            db.execute(
                update(ManualReview)
                .where(ManualReview.rule_result_id.in_(rule_result_ids), ManualReview.status == "PENDING")
                .values(status="INVALIDATED")
            )
        db.execute(
            update(NodeExecution)
            .where(NodeExecution.id.in_(execution_ids))
            .values(status="INVALIDATED")
        )

    if "FIELD_EXTRACTION" not in affected_nodes:
        return
    current_upload_ids = [
        document.current_upload_id
        for document in db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task.id)).all()
        if document.current_upload_id is not None
    ]
    if not current_upload_ids:
        return
    field_value_ids = list(
        db.scalars(
            select(TaskFieldValue.id).where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.document_upload_id.in_(current_upload_ids),
            )
        ).all()
    )
    if field_value_ids:
        # 重跑会新增事实版本；历史事实与历史证据不原地清空，只失效依据旧事实的待复核项。
        db.execute(
            update(ManualReview)
            .where(ManualReview.field_value_id.in_(field_value_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        )
    task.pending_review_hash = None

def _apply_manual_reviews_direct(
    db: Session,
    task_no: str,
    request: ManualReviewRequest,
    graph_namespace: str,
    expected_hash: str,
    expected_count: int,
) -> dict[str, Any]:
    task = task_or_404(db, task_no, lock=True)
    checkpoint_state = _validate_graph_namespace(task, graph_namespace)
    if task.status != "WAITING_REVIEW" or task.current_node != "MANUAL_REVIEW":
        raise BusinessError("MANUAL_REVIEW_NOT_WAITING", "任务当前不在人工复核等待状态", status_code=409)
    reviews = _current_review_batch(db, task)
    current_hash = _review_hash(reviews)
    if current_hash != task.pending_review_hash or current_hash != expected_hash or len(reviews) != expected_count:
        raise BusinessError("MANUAL_REVIEW_SET_CHANGED", "待复核集合已变化，请刷新后重新提交", status_code=409)
    if (
        checkpoint_state.get("phase") != "WAITING_REVIEW"
        or checkpoint_state.get("review_hash") != current_hash
        or checkpoint_state.get("review_count") != len(reviews)
    ):
        raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点不存在或与当前复核集合不一致", status_code=409)
    requested = {item.review_no: item for item in request.reviews}
    if len(requested) != len(request.reviews) or set(requested) != {item.review_no for item in reviews}:
        raise BusinessError("MANUAL_REVIEW_INCOMPLETE", "必须处理当前全部待复核项")
    normalized_values = _validate_review_payloads(db, task, reviews, requested)
    command_no = _decision_command_no(db, task)
    field_values_confirmed = False
    material_state_changed = False
    field_changes: list[dict[str, Any]] = []
    for review in reviews:
        decision = requested[review.review_no]
        review.decision = decision.decision
        review.business_note = decision.business_note
        review.basis_type = decision.basis_type
        review.decision_command_no = command_no
        review.status = "RESOLVED"
        if review.field_value_id:
            fact = _task_field_value_of_current_upload(db, task, review.field_value_id)
            if not fact:
                raise BusinessError("REVIEW_FIELD_MISSING", "复核关联字段不存在", status_code=500)
            if decision.decision in {"CONFIRM_FIELD", "MODIFY_FIELD"}:
                raw_text, normalized_value = normalized_values[review.id]
                new_status, raw_value, normalized_json = "REVIEWED", raw_text, normalized_value
                field_values_confirmed = True
            else:
                # 证据不足：标准化的原文原样保留（原文是事实本身，不因裁决被改写），
                # 但事实状态改为「证据不足」——这条取值当前不足以支撑下游判定。
                new_status, raw_value, normalized_json = "EVIDENCE_UNCLEAR", fact.raw_value, fact.normalized_value_json
                # 下游此前是按这条取值算出来的，现在依据不再成立：必须与「改了值」同样重算，
                # 否则规则会继续沿用一条已被判为证据不足的取值得出结论。
                field_values_confirmed = True
            # 人工写出新值必须新建事实版本，不原地改写抽取事实。
            new_fact, _ = _write_human_field_fact(
                db,
                task,
                field_key=fact.field_key,
                material_key=fact.material_key,
                document_upload_id=fact.document_upload_id,
                status=new_status,
                raw_value=raw_value,
                normalized_value=normalized_json,
            )
            review.field_value_id = new_fact.id
            review.before_value = fact.raw_value
            review.after_value = new_fact.raw_value
            field_changes.append(
                {
                    "field_key": fact.field_key,
                    "material_key": fact.material_key,
                    "before_value": fact.raw_value,
                    "after_value": new_fact.raw_value,
                    "revision_no": new_fact.revision_no,
                    "status": new_fact.status,
                }
            )
        if review.rule_result_id:
            result = db.get(RuleResult, review.rule_result_id)
            if not result:
                raise BusinessError("REVIEW_RULE_MISSING", "复核关联规则不存在", status_code=500)
            before_status = result.status
            # 人工结论成为后续风险使用的有效结论；系统原始 message 与判断保留在复核快照中，
            # 不被人工说明覆盖，报告仍能分别展示系统原判断与人工结论。
            result.status = {
                "CONFIRM_ABNORMAL": "ABNORMAL",
                "MARK_NORMAL": "PASSED",
                "EVIDENCE_INSUFFICIENT": "EVIDENCE_INSUFFICIENT",
            }.get(decision.decision, result.status)
            result.result_json = {
                **(result.result_json or {}),
                "manual_review_no": review.review_no,
                "manual_effective_status": result.status,
                "manual_business_note": decision.business_note,
                "manual_conclusion": REVIEW_DECISION_LABELS.get(decision.decision, decision.decision),
                "manual_basis_type": decision.basis_type,
            }
            review.before_value = before_status
            review.after_value = result.status
        if review.document_upload_id:
            upload = db.get(DocumentUpload, review.document_upload_id)
            document = db.get(TaskDocument, upload.task_document_id) if upload else None
            if not document:
                raise BusinessError("REVIEW_DOCUMENT_MISSING", "复核关联材料不存在", status_code=500)
            review.before_value = document.status
            if decision.decision == "MARK_NORMAL":
                # 人工明确认定材料放对了材料位：类型定案可用。
                document.status = "PREPARED"
            elif decision.decision == "CONFIRM_ABNORMAL":
                # 人工明确确认与材料位不符：这是确定的人工结论，与“尚未定案”必须分开表达。
                document.status = MATERIAL_TYPE_REJECTED
            else:
                # 证据不足：类型仍未定案，保持待复核，不冒充人工否定结论。
                document.status = MATERIAL_TYPE_REVIEW
            review.after_value = document.status
            # 材料类型定案只影响「以材料类型为前提」的那部分判定；普通审核（金额、日期、
            # 字段比较、条款语义）按材料中的真实原文判定，不受类型结论影响，因此不必重跑。
            # 因此这里不再把整条校验链路退回内部校验：见下面 validation_reexecution_required。
            material_state_changed = True
    scenario = _scenario(db, task)
    manifest = scenario.manifest_json
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    current_index = next(index for index, item in enumerate(nodes) if item["node_code"] == "MANUAL_REVIEW")
    # 只有**字段事实本身变化**（确认新值 / 判为证据不足）才需要把校验链路退回重判：
    # 那条取值的含义变了，按它算出来的结论不再成立。
    # 材料类型定案不在此列：它不再是依据可用性的开关，普通审核不受它影响，
    # 唯一受影响的是「以材料类型为前提」的文件检查，由下面就地重算处理。
    validation_reexecution_required = field_values_confirmed
    invalidated_reviews = 0
    file_quality_recompute: dict[str, Any] | None = None
    if material_state_changed:
        # 文件质量检查（DOCUMENT_PREPARE 节点）此前按「人工已确认与材料位不符」记过
        # “缺少有效依据”，或按“类型待复核”记过待确认说明；类型一旦定案，这条结论的依据
        # 已经变了，必须就地重算：只失效不重算会让下游拿到“依据已不成立”的旧结论。
        # 重跑该节点会重新调用外部服务并重建产物，代价与风险都高于就地重算，
        # 因此只重算质量结论：旧批次标 INVALIDATED 保留为历史，新批次挂在新执行记录上。
        # 人工结论不会被改回去：该节点现在只按 MATERIAL_CLASSIFY 产物**记录**识别结论，
        # 不再据它写材料状态或挂待办。
        invalidated_reviews += _invalidate_file_quality_results(db, task)["invalidated_reviews"]
        file_quality_recompute = _recompute_file_quality_results(db, task, manifest)
    if validation_reexecution_required:
        _refresh_extraction_rule_results(db, task, manifest)
        # 字段有效值已改变：失效内部校验及之后的结论与待办，回到内部校验重新判定。
        invalidated_reviews += _invalidate_validation_results(db, task)["invalidated_reviews"]
        next_node = next(item for item in nodes if item["node_code"] == "INTERNAL_VALIDATE")
        next_node_index = next(index for index, item in enumerate(nodes) if item["node_code"] == next_node["node_code"])
    else:
        next_node = nodes[current_index + 1] if current_index + 1 < len(nodes) else None
        next_node_index = current_index + 1 if next_node else current_index
    # 本批提交后仍有**有效的**待办时不能直接推进：本批先解决的是字段事实，
    # 规则结论待办（RULE_RESULT）此时仍是 PENDING，跳过它去风险评定会让规则复核被静默丢掉。
    # 剩余待办按同一口径（`_current_review_batch`）重新取，留在本节点形成下一批。
    #
    # 事实发生改变时仍按原语义退回内部校验重判：`_invalidate_validation_results` 会失效
    # 校验节点及之后的规则结果与它们的待办；活下来的待办挂在字段抽取结果上，
    # 任务重新走到人工复核节点时会被同一口径重新取到，不会丢失。
    #
    # 补挂材料类型待办放在这里**之前**会更自然，但那会把「类型待复核」这条独立事项插进
    # 正在排队的批里。`_sync_type_review_pending` 只在当前一条待办都没有时才补挂，
    # 因此这里的调用不会改变本批之后要处理的集合。
    _sync_type_review_pending(db, task)
    remaining = _current_review_batch(db, task)
    stay_in_review = bool(remaining) and not validation_reexecution_required
    if stay_in_review:
        next_node = None
        next_node_index = current_index
    next_hash = _review_hash(remaining) if stay_in_review else None
    sequence_no = (db.scalar(select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task.id)) or 0) + 1
    db.add(
        NodeExecution(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            node_code="MANUAL_REVIEW",
            sequence_no=sequence_no,
            status="SUCCEEDED",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            result_json={
                "resolved_count": len(reviews),
                "validation_reexecution_required": validation_reexecution_required,
                "material_state_changed": material_state_changed,
                "field_changes": field_changes,
                # 本次是否真的重算了文件质量结论，以及重算出的分布；没重算时为 None，
                # 不写“已重算”这种无法核对的结论。
                "file_quality_recompute": file_quality_recompute,
                # 本批之后仍未处理的有效待办。退回重判时它们在任务重新走到本节点后
                # 会被同一口径重新取到；留在本节点时就是下一批要处理的集合。
                "remaining_review_count": len(remaining),
                "remaining_review_nos": sorted(item.review_no for item in remaining),
                "stayed_in_manual_review": stay_in_review,
            },
        )
    )
    if stay_in_review:
        # 留在人工复核等待点：任务状态、复核集合哈希与流程图等待点必须一起更新，
        # 否则下一批提交会因集合哈希或检查点不一致被拒绝。
        task.pending_review_hash = next_hash
        task.status = "WAITING_REVIEW"
        task.current_node = "MANUAL_REVIEW"
        _set_task_checkpoint(
            task,
            node_index=current_index,
            phase="WAITING_REVIEW",
            review_hash=next_hash,
            review_count=len(remaining),
        )
    else:
        task.pending_review_hash = None
        task.status = "READY" if next_node else "COMPLETED"
        if next_node:
            task.current_node = next_node["node_code"]
        _set_task_checkpoint(
            task,
            node_index=next_node_index,
            phase="AWAITING_CONFIRMATION" if next_node else "COMPLETED",
        )
    _save_flow_checkpoint(db, task)
    db.commit()
    return {
        "task_no": task.task_no,
        "resolved_count": len(reviews),
        "status": task.status,
        "next_node_name": next_node["name"] if next_node else None,
        "field_changes": field_changes,
        "invalidated_reviews": invalidated_reviews,
        "file_quality_recompute": file_quality_recompute,
        # 仍有待办时明确回报本批之后要处理多少条，页面据此继续等待而不是以为已完成。
        "remaining_review_count": len(remaining) if stay_in_review else 0,
    }
