"""人工复核裁决与依赖结果的重算。

复核分批（不可打乱）：存在字段确认待办时本批只包含字段事实，否则包含全部待办。判卷、提交与
裁决必须用同一集合，否则用户会先裁决一条即将被字段纠错失效的规则。材料类型待复核不参与这个
分批——它是独立事项，捆进字段批次会让两条互不相干的业务问题互相决定对方批次的哈希。

裁决后按实际事实引用使受影响的规则执行批次失效，保留人工确认值，重新执行生效规则。

失效一律保留历史：旧批次标 INVALIDATED，旧事实与旧复核仍可追溯，不原地清空。"""

import hashlib
from ...domain import fact_snapshot
from ...domain.manifest import enabled_extraction_field_keys
from ...domain.audit_vocab import MATERIAL_TYPE_REJECTED, MATERIAL_TYPE_REVIEW
from ...common.exceptions import BusinessError
from ...entity import AuditTask, BackgroundOperation, DocumentUpload, ManualReview, NodeExecution, RuleResult, TaskDocument, TaskFact, TaskFieldValue
from .. import task_csv_service
from ..operation_types import OBJECT_AUDIT_TASK
from .facts import _current_field_fact_pairs, _write_human_field_fact
from .shared import REVIEW_DECISION_LABELS, _normalize_field_value, _task_field_definitions, _task_manifest
from .state import _task_field_value_of_current_upload
from ...domain.rule_library import execution_container_of
from decimal import InvalidOperation
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Any


def _review_hash(reviews: list[ManualReview]) -> str:
    value = ":".join(sorted(item.review_no for item in reviews))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _current_review_batch(db: Session, task: AuditTask) -> list[ManualReview]:
    """本批待办复核集合：先事实、后规则。

    存在字段确认（FIELD_CONFIRMATION）待办时，本批只包含字段事实，否则包含全部待办。判卷与
    完整性校验（prepare / submit / apply）必须用同一集合，否则用户会先裁决一条即将被字段纠错
    失效的规则。

    材料类型待复核不参与这个分批：它不再是「依据不可用」的阻断来源，而是一条独立的待确认事项。
    把它捆进字段批次会让两条互不相干的业务问题互相决定对方批次的哈希，也解释不了「为什么改
    材料类型会让我正在填的字段复核被作废」。

    这是分批口径而非一次性口径：一批提交后仍可能剩下规则结论待办，提交方据此重新取集合决定
    「继续复核本节点」还是「推进下一节点」。规则待办未处理完时不得推进。
    """
    pending = list(
        db.scalars(
            select(ManualReview)
            .where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
            .order_by(ManualReview.id)
        ).all()
    )
    targets: dict[tuple[str, int], str] = {}
    for review in pending:
        linked = [
            ("FIELD", review.field_value_id),
            ("RULE", review.rule_result_id),
            ("DOCUMENT", review.document_upload_id),
        ]
        actual = [(kind, int(target_id)) for kind, target_id in linked if target_id is not None]
        if len(actual) != 1:
            raise BusinessError(
                "REVIEW_TARGET_INVALID",
                "待复核项没有关联唯一的业务对象，无法形成提交批次",
                status_code=500,
                details={"review_no": review.review_no},
            )
        target = actual[0]
        previous = targets.get(target)
        if previous is not None:
            raise BusinessError(
                "REVIEW_TARGET_DUPLICATE",
                "同一业务对象存在多条待处理复核，无法确定应提交哪一条",
                status_code=500,
                details={
                    "target_type": target[0],
                    "target_id": target[1],
                    "review_nos": [previous, review.review_no],
                },
            )
        targets[target] = review.review_no
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
        return {"CONFIRM_FIELD", "MODIFY_FIELD", "MARK_NOT_PRESENT", "EVIDENCE_INSUFFICIENT"}
    if review_type in {"RULE_RESULT", "MATERIAL_TYPE_MISMATCH"}:
        return {"CONFIRM_ABNORMAL", "MARK_NORMAL", "EVIDENCE_INSUFFICIENT"}
    raise BusinessError("REVIEW_TYPE_INVALID", "人工复核类型不受支持", details={"review_type": review_type})

def _validate_review_payloads(
    db: Session,
    task: AuditTask,
    reviews: list[ManualReview],
    requested: dict[str, Any],
) -> dict[int, tuple[str, Any]]:
    field_definitions = _task_field_definitions(task)
    current_field_pairs = _current_field_fact_pairs(db, task)
    normalized_values: dict[int, tuple[str, Any]] = {}
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
            current_pair = current_field_pairs.get((fact.field_key, fact.material_key))
            if current_pair is None or current_pair[0].id != fact.id:
                raise BusinessError(
                    "REVIEW_FIELD_FACT_STALE",
                    "复核关联字段不是当前成对生成的字段版本与统一事实，不能提交裁决",
                    status_code=409,
                    details={"review_no": review.review_no, "field_value_id": review.field_value_id},
                )
            field = field_definitions.get(fact.field_key)
            if not field or field["material_key"] != fact.material_key:
                raise BusinessError("REVIEW_FIELD_SCHEMA_MISSING", "复核字段不在任务字段清单中", status_code=500)
            if decision.decision in {"CONFIRM_FIELD", "MODIFY_FIELD"}:
                if decision.field_value is None or not decision.field_value.strip():
                    raise BusinessError("REVIEW_FIELD_VALUE_REQUIRED", "确认或修改字段时必须填写确认后的字段值")
                try:
                    # 与自动抽取同一套口径：金额按字段声明的单位与币种校验原文写法。
                    submitted_normalized = _normalize_field_value(
                        field["value_type"],
                        decision.field_value,
                        unit=field.get("unit"),
                        currency=field.get("currency"),
                        extraction_strategy=field.get("extraction_strategy"),
                        structure_schema=field.get("structure_schema"),
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
                if decision.decision == "CONFIRM_FIELD":
                    if fact.raw_value is None or fact.normalized_value_json is None:
                        raise BusinessError(
                            "REVIEW_FIELD_CONFIRM_VALUE_MISSING",
                            "当前候选没有可直接确认的有效值，请选择修改字段并填写正确值",
                            status_code=409,
                            details={"review_no": review.review_no, "field_key": fact.field_key},
                        )
                    if fact_snapshot.canonical_field_value(
                        submitted_normalized, field
                    ) != fact_snapshot.canonical_field_value(fact.normalized_value_json, field):
                        raise BusinessError(
                            "REVIEW_FIELD_CONFIRM_VALUE_CHANGED",
                            "确认字段不能改变当前候选值；如需改值请选择修改字段",
                            status_code=409,
                            details={"review_no": review.review_no, "field_key": fact.field_key},
                        )
                    normalized_values[review.id] = (fact.raw_value, fact.normalized_value_json)
                else:
                    normalized_values[review.id] = (decision.field_value.strip(), submitted_normalized)
            elif decision.field_value is not None:
                raise BusinessError("REVIEW_FIELD_VALUE_UNEXPECTED", "当前字段复核结论不能填写字段值")
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


def _invalidate_validation_results(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    changed_field_keys: set[str],
) -> dict[str, Any]:
    """字段事实改变后只失效实际引用它的节点及 DAG 后继，并撤销派生事实。

    旧批次的待复核项同时失效：新结果批次是否需要人工判断由新结果自己决定，
    旧复核项不能继续挂在待办里，也不能自动贴到新结果上。
    返回真实失效数量，供纠错接口如实回报影响范围。

    普通规则按 field_key 建立引用，计算规则按字段 source_ref 建立引用；报告会展示
    任务字段，因此启用了报告规则时也是字段的直接消费者。之后只沿 DAG 向后传播。
    """
    affected_codes = _field_change_affected_node_codes(
        manifest,
        changed_field_keys,
        set(task.task_schema_json["enabled_rule_keys"]),
    )
    counts: dict[str, Any] = {
        "invalidated_executions": 0,
        "invalidated_rule_results": 0,
        "invalidated_reviews": 0,
        "affected_node_codes": sorted(affected_codes),
    }
    if not affected_codes:
        return counts
    execution_ids = list(
        db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(affected_codes),
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if not execution_ids:
        return counts
    counts.update(_invalidate_execution_records(db, task, execution_ids))
    return counts


def _invalidate_execution_records(
    db: Session,
    task: AuditTask,
    execution_ids: list[int],
) -> dict[str, int]:
    """失效指定执行及其规则、字段待办和该批次产生的全部当前事实。"""
    counts = {"invalidated_executions": 0, "invalidated_rule_results": 0, "invalidated_reviews": 0}
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
    field_value_ids = list(
        db.scalars(
            select(TaskFieldValue.id).where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.extraction_execution_id.in_(execution_ids),
                TaskFieldValue.is_current.is_(True),
            )
        ).all()
    )
    if field_value_ids:
        counts["invalidated_reviews"] += db.execute(
            update(ManualReview)
            .where(ManualReview.field_value_id.in_(field_value_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        ).rowcount or 0
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(field_value_ids), TaskFieldValue.is_current.is_(True))
            .values(is_current=False)
        )
    # 执行批次既可能产生计算派生事实，也可能产生抽取统一事实。只撤销 DERIVED 会让
    # 已失效抽取节点的旧字段继续留在 TaskFact 当前位，后续规则仍能读取到它。
    db.execute(
        update(TaskFact)
        .where(
            TaskFact.audit_task_id == task.id,
            TaskFact.source_execution_id.in_(execution_ids),
            TaskFact.is_current.is_(True),
        )
        .values(is_current=False)
    )
    counts["invalidated_executions"] = db.execute(
        update(NodeExecution)
        .where(NodeExecution.id.in_(execution_ids))
        .values(status="INVALIDATED")
    ).rowcount or 0
    return counts


def _field_change_affected_node_codes(manifest: dict[str, Any], changed_field_keys: set[str], enabled_rule_keys: set[str]) -> set[str]:
    """字段被本任务实际生效项引用时，重新遍历规则库执行批次。"""
    if not changed_field_keys:
        return set()
    for rule in manifest.get("rules") or []:
        if rule["rule_key"] not in enabled_rule_keys or not rule.get("enabled", True):
            continue
        referenced = {i["field_key"] for i in rule.get("inputs") or []} | set(rule.get("outputs") or [])
        if referenced & changed_field_keys or rule.get("kind") == "REPORT":
            return {"RULE_EXECUTION"}
    return set()


def _next_node_for_recalculation(db: Session, task: AuditTask, manifest: dict[str, Any], nodes: list[dict[str, Any]], *, completed_node_code: str | None = None) -> dict[str, Any] | None:
    """重新执行失效的规则批次，不寻找业务阶段或人工复核节点。"""
    db.flush()
    completed = db.scalar(select(NodeExecution.id).where(NodeExecution.audit_task_id == task.id,
                          NodeExecution.node_code == "RULE_EXECUTION", NodeExecution.status == "SUCCEEDED"))
    return None if completed is not None else next(item for item in nodes if item["node_code"] == "RULE_EXECUTION")


def _invalidate_retry_results(
    db: Session,
    task: AuditTask,
    nodes: list[dict[str, Any]],
    current_index: int,
) -> None:
    """重试前只失效被重试节点及其逻辑后继，不动更早的节点。"""
    manifest = _task_manifest(task)
    affected_nodes = {"RULE_EXECUTION"}
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
        db.execute(
            update(TaskFact)
            .where(
                TaskFact.audit_task_id == task.id,
                TaskFact.fact_kind == "DERIVED",
                TaskFact.source_execution_id.in_(execution_ids),
                TaskFact.is_current.is_(True),
            )
            .values(is_current=False)
        )

    # 节点重试已经改变了当前执行/待办集合，旧批次哈希无论重试的是哪类节点都不再有效。
    # 这一步不能放在字段抽取分支末尾，否则普通规则节点和无字段归属的历史节点会提前返回，
    # 页面继续携带旧哈希提交复核时只能得到过期批次冲突。
    task.pending_review_hash = None

    # 统一容器重跑只失效方案生效规则的字段产出。
    extraction_codes = {execution_container_of(manifest)["node_code"]}
    affected_extraction_codes = extraction_codes & affected_nodes
    if not affected_extraction_codes:
        return
    # 执行契约包含保留的停用定义，不能直接作为重试取数范围。
    # 与实际抽取共用启停口径，只失效受影响节点在本任务中启用的抽取输出。
    enabled_rule_keys = set(task.task_schema_json["enabled_rule_keys"])
    affected_rule_keys = [
        rule["rule_key"] for rule in manifest["rules"]
        if rule["rule_key"] in enabled_rule_keys
        and rule["node"] in affected_extraction_codes
    ]
    affected_field_keys = enabled_extraction_field_keys(manifest, affected_rule_keys) & {
        field["field_key"] for field in task.task_schema_json["fields"]
    }
    if not affected_field_keys:
        return
    current_upload_ids = [
        document.current_upload_id
        for document in db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task.id)).all()
        if document.current_upload_id is not None
    ]
    field_value_ids = (
        list(
            db.scalars(
                select(TaskFieldValue.id).where(
                    TaskFieldValue.audit_task_id == task.id,
                    TaskFieldValue.scenario_version_id == task.scenario_version_id,
                    TaskFieldValue.field_key.in_(affected_field_keys),
                    TaskFieldValue.document_upload_id.in_(current_upload_ids),
                    TaskFieldValue.is_current.is_(True),
                )
            ).all()
        )
        if current_upload_ids
        else []
    )
    if field_value_ids:
        # 重跑会新增事实版本；历史事实与历史证据不原地清空，当前候选和依据它的待复核项
        # 必须一起退出当前位。否则重试失败时，旧候选仍会被任务视图或后续节点读到。
        db.execute(
            update(ManualReview)
            .where(ManualReview.field_value_id.in_(field_value_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        )
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(field_value_ids), TaskFieldValue.is_current.is_(True))
            .values(is_current=False)
        )
    # 抽取型统一事实没有直接保存 TaskFieldValue 外键，因此按任务冻结字段定义把
    # 本次重试节点拥有的 field_key 转成 fact_code。两张事实表要独立失效：即使某张表
    # 因历史异常缺了当前行，另一张表里的旧值也不能继续作为规则或计算输入。
    affected_fact_codes = {
        str(field.get("source_ref") or field["field_key"])
        for field in (task.task_schema_json.get("fields") or [])
        if str(field.get("field_key") or "") in affected_field_keys
    }
    if affected_fact_codes:
        db.execute(
            update(TaskFact)
            .where(
                TaskFact.audit_task_id == task.id,
                TaskFact.fact_kind == "EXTRACTED",
                TaskFact.fact_code.in_(affected_fact_codes),
                TaskFact.is_current.is_(True),
            )
            .values(is_current=False)
        )

def _apply_review_decisions(
    db: Session,
    task: AuditTask,
    reviews: list[ManualReview],
    requested: dict[str, Any],
    normalized_values: dict[int, tuple[str, Any]],
    command_no: str | None,
) -> tuple[bool, bool, list[dict[str, Any]]]:
    """把一批裁决写到各自的业务对象上，返回（字段事实是否变化, 材料状态是否变化, 字段变化清单）。

    只有裁决落库这一件事在这里，不含批次划分与流程推进：Agent 复核批与
    人工复核节点批调用的是同一份实现，两者的差别只在「提交之后怎么推进」。
    复制一份裁决逻辑会让「复核一个待办」在两处产生不同的副作用。
    """
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
            elif decision.decision == "MARK_NOT_PRESENT":
                # 人工确认字段确实不存在：保留 NOT_PRESENT 作为业务事实，
                # 但待办已解决；报告据此可以说明“已核实缺失”，而规则仍不会把它当成有效输入。
                new_status, raw_value, normalized_json = "NOT_PRESENT", None, None
                field_values_confirmed = True
            else:
                # 证据不足：标准化的原文原样保留（原文是事实本身，不因裁决被改写），
                # 但事实状态改为「证据不足」——这条取值当前不足以支撑下游判定。
                new_status, raw_value, normalized_json = "EVIDENCE_UNCLEAR", fact.raw_value, fact.normalized_value_json
                # 下游结论以这条取值为依据；依据失效时必须与「改了值」同样重算，
                # 否则规则会继续沿用一条已被判为证据不足的取值得出结论。
                field_values_confirmed = True
            # 人工写出新值必须新建事实版本，不原地改写抽取事实。
            new_fact, _, _ = _write_human_field_fact(
                db,
                task,
                field_key=fact.field_key,
                material_key=fact.material_key,
                document_upload_id=fact.document_upload_id,
                status=new_status,
                raw_value=raw_value,
                normalized_value=normalized_json,
                preserved_review_ids={review.id},
                inherit_evidence_from_id=(
                    fact.id
                    if decision.decision in {"CONFIRM_FIELD", "EVIDENCE_INSUFFICIENT"}
                    else None
                ),
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
            result_csv = task_csv_service.from_index(db, task, RuleResult, [result])[0]
            before_status = result_csv.status
            # 人工结论成为后续风险使用的有效结论；系统原始 message 与判断保留在复核快照中，
            # 不被人工说明覆盖，报告仍能分别展示系统原判断与人工结论。
            new_status = {
                "CONFIRM_ABNORMAL": "ABNORMAL",
                "MARK_NORMAL": "PASSED",
                "EVIDENCE_INSUFFICIENT": "EVIDENCE_INSUFFICIENT",
            }.get(decision.decision, result_csv.status)
            result.status = new_status
            result.result_json = {
                **(result_csv.result_json or {}),
                "manual_review_no": review.review_no,
                "manual_effective_status": new_status,
                "manual_business_note": decision.business_note,
                "manual_conclusion": REVIEW_DECISION_LABELS.get(decision.decision, decision.decision),
                "manual_basis_type": decision.basis_type,
            }
            review.before_value = before_status
            review.after_value = new_status
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
    # 字段事实或材料类型任一变化都会改变本任务的有效输入。两类变化共用同一个
    # input_revision，报告新鲜度、Agent 快照和并发提交据此识别旧输入。
    if field_values_confirmed or material_state_changed:
        task.input_revision += 1
    return field_values_confirmed, material_state_changed, field_changes
