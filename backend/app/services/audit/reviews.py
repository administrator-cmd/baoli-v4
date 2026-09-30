"""人工复核裁决与依赖结果的重算。

复核分批（不可打乱）：存在字段确认待办时本批**只**包含字段事实，否则包含全部待办。
判卷、提交与裁决必须用同一集合，否则用户会先裁决一条即将被字段纠错失效的规则。
材料类型待复核不参与这个分批——它是独立事项，捆进字段批次会让两条互不相干的业务问题
互相决定对方批次的哈希。

裁决后按「事实变化」的范围重算，不做无差别重跑：

- 字段事实变化 → 重算抽取规则结果，只失效引用该字段的节点及其 DAG 后继，退回重判；
- 材料类型定案 → 只就地重算文件质量结论（判定依据变了），**不**退回校验链路、
  不作废排队中的复核。

失效一律保留历史：旧批次标 INVALIDATED，旧事实与旧复核仍可追溯，不原地清空。"""

import hashlib
from ...domain import fact_snapshot, workflow_dag
from ...domain.audit_vocab import MATERIAL_TYPE_REJECTED, MATERIAL_TYPE_REVIEW
from ...common.exceptions import BusinessError
from ...entity import AuditTask, BackgroundOperation, DocumentUpload, ManualReview, NodeExecution, RuleResult, TaskDocument, TaskFact, TaskFieldValue
from ...schema.dto import ManualReviewRequest
from ..operation_types import OBJECT_AUDIT_TASK
from ..task_lookup import task_or_404
from .facts import _current_field_fact_pairs, _refresh_extraction_rule_results, _write_human_field_fact
from .quality import _execute_file_quality_rules, _sync_type_review_pending
from .shared import REVIEW_DECISION_LABELS, _normalize_field_value, _task_field_definitions, _task_manifest
from .state import _node_codes_of_type, _save_flow_checkpoint, _set_task_checkpoint, _single_node_code_of_type, _task_field_value_of_current_upload, _validate_graph_namespace
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

def _invalidate_file_quality_results(
    db: Session, task: AuditTask, manifest: dict[str, Any]
) -> dict[str, Any]:
    """材料类型经人工确认变化后，失效文件质量检查的旧结论与待办。

    质量检查的判定依赖材料类型（类型待复核时它会明确记“缺少有效依据”）；
    类型一旦定案，旧结论的依据已经不再成立，必须重算，不能沿用到新批次。
    失效已执行的文件处理实例的旧结果：材料识别与正文抽取产物仍有效，不重调供应商。
    重算由 `_recompute_file_quality_results` 在同一事务里紧接着完成——只失效不重算，
    下游报告与风险就会拿到“依据已不成立”的旧结论。
    """
    counts: dict[str, Any] = {
        "invalidated_executions": 0,
        "invalidated_rule_results": 0,
        "invalidated_reviews": 0,
        "node_codes": [],
    }
    executions = list(
        db.scalars(
            select(NodeExecution).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(_node_codes_of_type(manifest, "DOCUMENT_PREPARE")),
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if not executions:
        return counts
    execution_ids = [item.id for item in executions]
    executed_codes = {item.node_code for item in executions}
    counts["node_codes"] = [
        item["node_code"]
        for item in sorted(manifest["nodes"], key=lambda node: node["sequence_no"])
        if item["node_code"] in executed_codes
    ]
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

def _recompute_file_quality_results(
    db: Session, task: AuditTask, manifest: dict[str, Any], node_codes: list[str]
) -> dict[str, Any]:
    """按当前材料状态就地重算文件质量检查结论，产出新的有效结果批次。

    只重算质量规则本身：8018/8010/印章/OCR 的产物已按上传身份持久化为 READY，
    本函数不重新识别、不重新提交 OCR，也不接触任何外部服务。
    旧批次由 `_invalidate_file_quality_results` 标为 INVALIDATED 保留为历史；
    每个已执行实例各建一条新执行记录（rule_result 的唯一键是「执行批次 + 规则键」，
    不能复用已失效批次的执行行），规则仍只挂在自身归属实例，供报告与风险读取。
    """
    sequence_no = db.scalar(select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task.id)) or 0
    status_counts: dict[str, int] = {}
    per_node: list[dict[str, Any]] = []
    for node_code in node_codes:
        sequence_no += 1
        now = datetime.now(UTC)
        execution = NodeExecution(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            node_code=node_code,
            sequence_no=sequence_no,
            status="SUCCEEDED",
            started_at=now,
            finished_at=now,
            # 本批次只重算质量结论，已经持久化的外部产物不重新请求。
            result_json={
                "recomputed": True,
                "recompute_reason": "材料类型经人工复核确认，文件质量检查的判定依据已改变，本次按当前材料状态重算质量结论",
                "recompute_scope": "FILE_QUALITY_CHECK",
                "external_services_called": False,
            },
        )
        db.add(execution)
        # 会话未启用 autoflush：先落盘调用方刚写入的材料状态与本次执行标识。
        db.flush()
        summary = _execute_file_quality_rules(db, task, manifest, execution)
        execution.result_json = {**(execution.result_json or {}), **summary}
        per_node.append({"node_code": node_code, "sequence_no": sequence_no, **summary})
        for status, count in summary["file_quality_status_counts"].items():
            status_counts[status] = status_counts.get(status, 0) + count
    db.flush()
    return {
        "file_quality_rule_count": sum(item["file_quality_rule_count"] for item in per_node),
        "file_quality_status_counts": status_counts,
        "nodes": per_node,
    }

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


def _field_change_affected_node_codes(
    manifest: dict[str, Any],
    changed_field_keys: set[str],
    enabled_rule_keys: set[str],
) -> set[str]:
    """解析字段的真实消费者，再沿节点依赖图取得全部下游节点。"""
    if not changed_field_keys:
        return set()
    plans = workflow_dag.plans_of(manifest)
    enabled_node_codes = {plan.node_instance_code for plan in plans if plan.enabled}
    affected: set[str] = set()

    for rule in manifest.get("rules") or []:
        if (
            not rule.get("enabled", True)
            or rule.get("rule_key") not in enabled_rule_keys
            or rule.get("node") not in enabled_node_codes
        ):
            continue
        referenced_fields = {
            str(item.get("field_key"))
            for item in (rule.get("inputs") or [])
            if isinstance(item, dict) and item.get("field_key")
        }
        if referenced_fields & changed_field_keys or rule.get("operator") == "REPORT_RENDER":
            affected.add(str(rule["node"]))

    changed_field_codes = {
        str(field.get("source_ref"))
        for field in (manifest.get("fields") or [])
        if field.get("field_key") in changed_field_keys and field.get("source_ref")
    }
    for calculation in manifest.get("calculations") or []:
        node_code = str(calculation.get("node_instance_code") or "")
        if (
            calculation.get("status") != "DISABLED"
            and node_code in enabled_node_codes
            and changed_field_codes
            & {str(code) for code in (calculation.get("depends_on_field_codes") or [])}
        ):
            affected.add(node_code)

    while True:
        descendants = {
            plan.node_instance_code
            for plan in plans
            if plan.enabled and any(dependency in affected for dependency in plan.depends_on)
        }
        expanded = affected | descendants
        if expanded == affected:
            return affected
        affected = expanded


def _next_node_for_recalculation(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    nodes: list[dict[str, Any]],
    *,
    completed_node_code: str | None = None,
) -> dict[str, Any] | None:
    """按失效后的有效执行记录和 DAG 选择重算起点。"""
    plans = workflow_dag.plans_of(manifest)
    # 会话关闭 autoflush；本事务内刚失效的执行记录不能继续算作已完成。
    db.flush()
    completed = set(
        db.scalars(
            select(NodeExecution.node_code).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.status.in_(("SUCCEEDED", "SKIPPED")),
            )
        ).all()
    )
    if completed_node_code is not None:
        completed.add(completed_node_code)
    pending_reviews = list(
        db.scalars(
            select(ManualReview.review_no).where(
                ManualReview.audit_task_id == task.id,
                ManualReview.status == "PENDING",
            )
        ).all()
    )
    ready = (
        workflow_dag.ready_node_for_pending_review(plans, completed=completed)
        if pending_reviews
        else workflow_dag.ready_node(plans, completed=completed)
    )
    if pending_reviews and ready is None:
        raise BusinessError(
            "AUDIT_REVIEW_NODE_UNAVAILABLE",
            "仍有待人工复核项，但当前流程无法到达人工复核节点",
            status_code=409,
            details={"review_nos": pending_reviews},
        )
    if ready is not None:
        return next(item for item in nodes if item["node_code"] == ready.node_instance_code)
    blocked = workflow_dag.blocked_nodes(plans, completed=completed)
    if blocked:
        raise BusinessError(
            "AUDIT_WORKFLOW_BLOCKED",
            "字段更正后，审核流程的节点依赖未满足",
            status_code=409,
            details={"blocked_nodes": [item.node_instance_code for item in blocked]},
        )
    return None

def _invalidate_retry_results(
    db: Session,
    task: AuditTask,
    nodes: list[dict[str, Any]],
    current_index: int,
) -> None:
    """重试前只失效被重试节点及其逻辑后继，不动更早的节点。"""
    manifest = _task_manifest(task)
    plans = workflow_dag.plans_of(manifest)
    affected_nodes = {nodes[current_index]["node_code"]}
    while True:
        descendants = {
            plan.node_instance_code
            for plan in plans
            if any(dependency in affected_nodes for dependency in plan.depends_on)
        }
        expanded = affected_nodes | descendants
        if expanded == affected_nodes:
            break
        affected_nodes = expanded
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

    # `affected_nodes` 保存用户配置的节点实例码（如 EXTRACT1），先从 manifest
    # 解析 FIELD_EXTRACTION 对应的实例码，再判断本次重试是否影响抽取字段。
    extraction_codes = set(_node_codes_of_type(manifest, "FIELD_EXTRACTION"))
    affected_extraction_codes = extraction_codes & affected_nodes
    if not affected_extraction_codes:
        return
    affected_field_keys: set[str] = set()
    for node in manifest.get("nodes") or []:
        if str(node.get("node_code") or "") not in affected_extraction_codes:
            continue
        contract = node.get("execution_contract")
        if isinstance(contract, dict) and isinstance(contract.get("field_keys"), list):
            affected_field_keys.update(
                str(field_key) for field_key in contract["field_keys"] if field_key
            )
    # 历史 manifest 没有节点执行契约，字段归属由抽取规则的 outputs 表达。
    for rule in manifest.get("rules") or []:
        if (
            str(rule.get("node") or "") in affected_extraction_codes
            and rule.get("operator") == "LLM_EXTRACT"
        ):
            affected_field_keys.update(
                str(field_key) for field_key in (rule.get("outputs") or []) if field_key
            )
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

    只有**裁决落库**这一件事在这里，不含批次划分与流程推进：Agent 复核批与
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
    # 字段事实或材料类型任一变化都会改变本任务的有效输入。两类变化共用同一个
    # input_revision，报告新鲜度、Agent 快照和并发提交据此识别旧输入。
    if field_values_confirmed or material_state_changed:
        task.input_revision += 1
    return field_values_confirmed, material_state_changed, field_changes


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
    # 等待点按**能力类型**解析实例码：`task.current_node` 是实例码，
    # 与能力类型比较在用户改了实例码之后恒为假，复核会被永远挡在门外。
    review_node_code = _single_node_code_of_type(
        _task_manifest(task), "MANUAL_REVIEW", purpose="人工复核等待点"
    )
    if task.status != "WAITING_REVIEW" or task.current_node != review_node_code:
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
    field_values_confirmed, material_state_changed, field_changes = _apply_review_decisions(
        db,
        task,
        reviews,
        requested,
        normalized_values,
        command_no,
    )
    db.flush()
    manifest = _task_manifest(task)
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    review_node_code = _single_node_code_of_type(manifest, "MANUAL_REVIEW", purpose="人工复核等待点")
    current_index = next(index for index, item in enumerate(nodes) if item["node_code"] == review_node_code)
    # 只有**字段事实本身变化**（确认新值 / 判为证据不足）才需要把校验链路退回重判：
    # 那条取值的含义变了，按它算出来的结论不再成立。
    # 材料类型定案不在此列：它不再是依据可用性的开关，普通审核不受它影响，
    # 唯一受影响的是「以材料类型为前提」的文件检查，由下面就地重算处理。
    validation_reexecution_required = field_values_confirmed
    invalidated_reviews = 0
    file_quality_recompute: dict[str, Any] | None = None
    if material_state_changed:
        # 材料类型定案后，文件质量结论的依据已经变化，必须就地重算；
        # 只失效不重算会让下游读取依据已不成立的旧结论。
        # 重跑该节点会重新调用外部服务并重建产物，代价与风险都高于就地重算，
        # 因此只重算质量结论：旧批次标 INVALIDATED 保留为历史，新批次挂在新执行记录上。
        # 人工结论不会被改回去：该节点现在只按 MATERIAL_CLASSIFY 产物**记录**识别结论，
        # 不再据它写材料状态或挂待办。
        invalidated_quality = _invalidate_file_quality_results(db, task, manifest)
        invalidated_reviews += invalidated_quality["invalidated_reviews"]
        file_quality_recompute = _recompute_file_quality_results(
            db, task, manifest, invalidated_quality["node_codes"]
        )
    if validation_reexecution_required:
        _refresh_extraction_rule_results(db, task, manifest)
        # 字段有效值已改变：计算与校验的旧结果、派生事实及其后继一并失效。
        changed_field_keys = {str(item["field_key"]) for item in field_changes}
        invalidation = _invalidate_validation_results(db, task, manifest, changed_field_keys)
        invalidated_reviews += invalidation["invalidated_reviews"]
        review_still_valid = review_node_code not in set(invalidation["affected_node_codes"])
    else:
        review_still_valid = True
    # 字段更正可能失效规则待办，材料类型待办也可能在本批裁决后新建。
    # 先确定真正剩余的集合，再决定继续复核或沿 DAG 重算。
    _sync_type_review_pending(db, task)
    remaining = _current_review_batch(db, task)
    stay_in_review = bool(remaining) and review_still_valid
    next_node = None if stay_in_review else _next_node_for_recalculation(
        db,
        task,
        manifest,
        nodes,
        completed_node_code=review_node_code if review_still_valid else None,
    )
    next_node_index = (
        next(index for index, item in enumerate(nodes) if item["node_code"] == next_node["node_code"])
        if next_node else current_index
    )
    next_hash = _review_hash(remaining) if stay_in_review else None
    sequence_no = (db.scalar(select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task.id)) or 0) + 1
    db.add(
        NodeExecution(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            node_code=review_node_code,
            sequence_no=sequence_no,
            status="INVALIDATED" if validation_reexecution_required and not review_still_valid else "SUCCEEDED",
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
        task.current_node = review_node_code
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
