"""字段事实与字段抽取。

事实不可变：每次抽取或人工裁决都新增一个版本，旧行退出当前位但保留可追溯。唯一键
(任务, 字段, 材料位) 上只有一条 `is_current`，且该范围不含上传版本——旧上传版本的当前事实
也必须让位，否则新事实写不进去。

规则模型产出由 rule_execution 保存；本模块负责实际事实读取、人工纠错与版本失效。
人工确认值不被模型产出覆盖。"""

from .. import task_csv_service
import json
from ...domain import fact_snapshot, field_strategy
from ...domain.identity import public_no
from ...domain.manifest import required_field_scope
from ...common.exceptions import BusinessError
from ...entity import AuditTask, DocumentUpload, Evidence, ManualReview, TaskDocument, TaskFact, TaskFieldValue
from .shared import _task_field_definitions
from datetime import date
from decimal import Decimal, InvalidOperation
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Any


def _task_field_facts(
    db: Session,
    task: AuditTask,
    field_key: str,
    material_key: str,
) -> list[TaskFieldValue]:
    """同一 (任务, 字段, 材料位) 的全部事实版本，含历史上传版本，按版本号升序。

    current_fact_scope 唯一键不含 document_upload_id，所以“当前事实”的范围是整个
    (任务, 字段, 材料位)：清理当前位必须按这个范围做，否则旧上传版本还占着当前位时新事实写不进去。
    """
    return list(
        db.scalars(
            select(TaskFieldValue)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.field_key == field_key,
                TaskFieldValue.material_key == material_key,
            )
            .order_by(TaskFieldValue.revision_no)
        ).all()
    )


def _current_field_fact_pairs(
    db: Session,
    task: AuditTask,
) -> dict[tuple[str, str], tuple[TaskFieldValue, TaskFact]]:
    """读取当前字段候选与统一事实，并核对两者是否仍表示同一份业务事实。

    `TaskFieldValue` 只负责原文、候选值和证据；`TaskFact` 决定有效状态与可执行值。
    页面和报告需要同时展示两类信息，因此必须在这里按任务冻结字段定义配对，不能让
    调用方单独读取候选表后把它的状态或规范值当成正式事实。
    """
    definitions = _task_field_definitions(task)
    candidate_rows = list(
        db.scalars(
            select(TaskFieldValue)
            .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.is_current.is_(True),
                DocumentUpload.audit_task_id == task.id,
                DocumentUpload.is_current.is_(True),
                TaskDocument.audit_task_id == task.id,
                TaskDocument.material_key == TaskFieldValue.material_key,
                TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
            )
            .order_by(TaskFieldValue.id)
        ).all()
    )
    candidate_rows = task_csv_service.from_index(db, task, TaskFieldValue, candidate_rows)
    candidates: dict[tuple[str, str], TaskFieldValue] = {}
    for row in candidate_rows:
        key = (row.field_key, row.material_key)
        if key in candidates:
            raise BusinessError(
                "TASK_FIELD_CURRENT_DUPLICATE",
                "同一字段与材料位存在多条当前字段版本，无法确定展示或执行依据",
                status_code=500,
                details={"field_key": row.field_key, "material_key": row.material_key},
            )
        candidates[key] = row

    task_fact_rows = list(
        db.scalars(
            select(TaskFact).where(
                TaskFact.audit_task_id == task.id,
                TaskFact.scenario_version_id == task.scenario_version_id,
                TaskFact.fact_kind == "EXTRACTED",
                TaskFact.is_current.is_(True),
            )
        ).all()
    )
    task_fact_rows = task_csv_service.from_index(db, task, TaskFact, task_fact_rows)
    task_facts: dict[tuple[str, str, str, str, str], TaskFact] = {}
    for row in task_fact_rows:
        identity = (
            row.fact_code,
            row.material_key or "",
            row.period_scope,
            row.statement_scope,
            row.entity_scope,
        )
        if identity in task_facts:
            raise BusinessError(
                "TASK_FACT_IDENTITY_DUPLICATE",
                "同一字段业务身份存在多条当前统一事实，无法确定有效值",
                status_code=500,
                details={
                    "fact_code": row.fact_code,
                    "material_key": row.material_key,
                    "period_scope": row.period_scope,
                    "statement_scope": row.statement_scope,
                    "entity_scope": row.entity_scope,
                },
            )
        task_facts[identity] = row

    expected_identities: set[tuple[str, str, str, str, str]] = set()
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, TaskFact]] = {}
    for field_key, field in definitions.items():
        material_key = str(field["material_key"])
        key = (field_key, material_key)
        identity = (
            str(field.get("source_ref") or field_key),
            material_key,
            required_field_scope(
                field, "period_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
            ),
            required_field_scope(
                field, "statement_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
            ),
            str(field.get("entity_scope") or "SELF"),
        )
        expected_identities.add(identity)
        candidate = candidates.get(key)
        canonical = task_facts.get(identity)
        if candidate is None and canonical is None:
            continue
        if candidate is None or canonical is None:
            raise BusinessError(
                "TASK_FIELD_FACT_INCOMPLETE",
                "当前字段版本与统一事实没有成对生成，不能展示或生成报告",
                status_code=500,
                details={
                    "field_key": field_key,
                    "material_key": material_key,
                    "field_value_exists": candidate is not None,
                    "task_fact_exists": canonical is not None,
                },
            )
        if candidate.status != canonical.status:
            raise BusinessError(
                "TASK_FACT_FIELD_STATUS_MISMATCH",
                "当前字段版本与统一事实的状态不一致，不能展示或生成报告",
                status_code=500,
                details={
                    "field_key": field_key,
                    "material_key": material_key,
                    "field_status": candidate.status,
                    "fact_status": canonical.status,
                },
            )
        expected_contract = {
            "value_type": field.get("value_type"),
            "unit": field.get("unit"),
            "currency": field.get("currency"),
        }
        actual_contract = {
            "value_type": canonical.value_type,
            "unit": canonical.unit,
            "currency": canonical.currency,
        }
        if actual_contract != expected_contract:
            raise BusinessError(
                "TASK_FACT_FIELD_CONTRACT_MISMATCH",
                "统一事实的类型、单位或币种与任务字段契约不一致",
                status_code=500,
                details={
                    "field_key": field_key,
                    "material_key": material_key,
                    "expected": expected_contract,
                    "actual": actual_contract,
                },
            )
        if canonical.status in {"PRESENT", "REVIEWED"}:
            canonical_value = fact_snapshot.task_fact_value(canonical, field_definition=field)
            if canonical_value is None:
                raise BusinessError(
                    "TASK_FACT_VALUE_MISSING",
                    "统一事实已标记为有效，但没有可展示或执行的值",
                    status_code=500,
                    details={"field_key": field_key, "material_key": material_key},
                )
            candidate_value = fact_snapshot.canonical_field_value(candidate.normalized_value_json, field)
            if candidate_value != fact_snapshot.canonical_field_value(canonical_value, field):
                raise BusinessError(
                    "TASK_FACT_FIELD_VALUE_MISMATCH",
                    "当前字段候选值与统一事实的有效值不一致，不能继续展示或生成报告",
                    status_code=500,
                    details={"field_key": field_key, "material_key": material_key},
                )
        pairs[key] = (candidate, canonical)

    unexpected_candidates = sorted(set(candidates) - set((key, str(item["material_key"])) for key, item in definitions.items()))
    if unexpected_candidates:
        raise BusinessError(
            "TASK_FIELD_OUT_OF_SCOPE",
            "当前字段版本不在任务冻结的字段范围内",
            status_code=500,
            details={"field_keys": [key for key, _ in unexpected_candidates]},
        )
    unexpected_facts = sorted(set(task_facts) - expected_identities)
    if unexpected_facts:
        raise BusinessError(
            "TASK_FACT_OUT_OF_SCOPE",
            "当前统一事实不在任务冻结的字段范围内",
            status_code=500,
            details={"fact_codes": [identity[0] for identity in unexpected_facts]},
        )
    return pairs


def _next_field_revision(facts: list[TaskFieldValue], document_upload_id: int) -> int:
    """同一上传版本内的下一个版本号（唯一键含 document_upload_id + revision_no）。"""
    return max((item.revision_no for item in facts if item.document_upload_id == document_upload_id), default=0) + 1

def _write_human_field_fact(
    db: Session,
    task: AuditTask,
    *,
    field_key: str,
    material_key: str,
    document_upload_id: int,
    status: str,
    raw_value: str | None,
    normalized_value: Any | None,
    preserved_review_ids: set[int] | None = None,
    inherit_evidence_from_id: int | None = None,
) -> tuple[TaskFieldValue, int | None, int]:
    """人工裁决产生的字段事实新版本。

    旧当前事实先退出当前位（唯一键 (任务, current_fact_scope) 要求同一时刻只有一个当前事实，
    且该范围不含上传版本），新行不带抽取执行批次。确认原值或判为证据不足时可复制
    原证据到新版本；人工改写取值时不自动沿用旧引文。旧行、旧复核与旧证据保持可追溯。
    返回 (新事实, 上一版版本号, 本次失效的旧待办数量)。正在本事务里裁决的待办
    通过 preserved_review_ids 排除；Session 关闭了 autoflush，不能依赖内存里已改成
    RESOLVED 的状态让 SQL 自动避开它。
    """
    facts = _task_field_facts(db, task, field_key, material_key)
    current = next((item for item in facts if item.is_current), None)
    previous_revision_no = current.revision_no if current is not None else None
    invalidated_reviews = 0
    if facts:
        fact_ids = [item.id for item in facts]
        # 新事实一旦占据当前位，依据旧版本创建的待办就不能继续提交。把失效与
        # 事实换版放在同一事务里，避免任务仍展示旧待办、提交时才发现它已过期。
        review_conditions = [
            ManualReview.field_value_id.in_(fact_ids),
            ManualReview.status == "PENDING",
        ]
        if preserved_review_ids:
            review_conditions.append(ManualReview.id.not_in(sorted(preserved_review_ids)))
        invalidated_reviews = db.execute(
            update(ManualReview)
            .where(*review_conditions)
            .values(status="INVALIDATED")
        ).rowcount or 0
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(fact_ids))
            .values(is_current=False)
        )
        db.flush()
    fact = TaskFieldValue(
        audit_task_id=task.id,
        scenario_version_id=task.scenario_version_id,
        field_key=field_key,
        material_key=material_key,
        document_upload_id=document_upload_id,
        revision_no=_next_field_revision(facts, document_upload_id),
        is_current=True,
        status=status,
        raw_value=raw_value,
        normalized_value_json=normalized_value,
        evidence_id=None,
        extraction_execution_id=None,
        origin="HUMAN_CORRECTION",
    )
    db.add(fact)
    db.flush()
    if inherit_evidence_from_id is not None:
        inherited_evidence_id: int | None = None
        first_inherited_evidence_id: int | None = None
        source_primary_evidence_id = next(
            (item.evidence_id for item in facts if item.id == inherit_evidence_from_id),
            None,
        )
        source_evidences = db.scalars(
            select(Evidence)
            .where(
                Evidence.audit_task_id == task.id,
                Evidence.field_value_id == inherit_evidence_from_id,
                Evidence.rule_result_id.is_(None),
            )
            .order_by(Evidence.id)
        ).all()
        for source in source_evidences:
            inherited = Evidence(
                evidence_no=public_no("EVD"),
                audit_task_id=task.id,
                document_upload_id=source.document_upload_id,
                field_value_id=fact.id,
                field_path=source.field_path,
                quote_text=source.quote_text,
                artifact_id=source.artifact_id,
            )
            db.add(inherited)
            db.flush()
            if first_inherited_evidence_id is None:
                first_inherited_evidence_id = inherited.id
            if source.id == source_primary_evidence_id:
                inherited_evidence_id = inherited.id
        fact.evidence_id = inherited_evidence_id or first_inherited_evidence_id
    field = _task_field_definitions(task).get(field_key)
    if field is None or field["material_key"] != material_key:
        raise BusinessError(
            "TASK_FIELD_DEFINITION_MISSING",
            "人工纠错的字段不在任务冻结的字段定义中",
            status_code=500,
            details={"field_key": field_key, "material_key": material_key},
        )
    _write_field_task_fact(
        db,
        task,
        source_execution_id=None,
        field=field,
        material_key=material_key,
        status=status,
        normalized_value=normalized_value if status == "REVIEWED" else None,
    )
    return fact, previous_revision_no, int(invalidated_reviews)


def _write_field_task_fact(
    db: Session,
    task: AuditTask,
    *,
    source_execution_id: int | None,
    field: dict[str, Any],
    material_key: str,
    status: str,
    normalized_value: Any,
) -> None:
    """把抽取或人工纠错写进统一事实表。

    期间与报表口径取自当前场景的字段配置；不涉及这些口径时配置显式声明「不适用」，
    本函数不代填任何具体口径。

    有效状态必须同时写入与字段类型匹配的唯一取值列。类型不匹配时在这里明确
    失败，不产生「状态有效、值为空」或把错误金额塞入文本列的统一事实。
    """
    # 取数期间与报表口径来自字段自己的声明：计算参数按这两项定位事实，
    # 缺了就无法建立定位键。这里不代填默认值——默认成「本期 / 合并」会让一个
    # 声明了上期的计算参数静默取到本期的数，而结果看起来完全正常。
    period_scope = required_field_scope(
        field, "period_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
    )
    statement_scope = required_field_scope(
        field, "statement_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
    )
    entity_scope = str(field.get("entity_scope") or "SELF")
    value_type = str(field.get("value_type") or "DECIMAL")
    # task_field_value 保存抽取候选，供人工复核；task_fact 是计算与 Agent 使用的
    # 统一事实。证据或标准化尚未通过的候选不能在统一事实中携带可执行值。
    executable_value = normalized_value if status in {"PRESENT", "REVIEWED"} else None
    decimal_value = None
    text_value = None
    date_value = None
    boolean_value = None
    if executable_value is not None:
        strategy = field_strategy.strategy_of(field)
        try:
            if strategy == field_strategy.STRUCTURED:
                schema = field.get("structure_schema")
                if not isinstance(schema, dict):
                    raise ValueError("结构化字段缺少结构 Schema")
                structured = field_strategy.normalize_structured_value(executable_value, schema)
                text_value = json.dumps(
                    structured,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            elif value_type in {"DECIMAL", "AMOUNT"}:
                if isinstance(executable_value, bool):
                    raise ValueError("数值不能是布尔值")
                decimal_value = Decimal(str(executable_value).strip())
                if not decimal_value.is_finite():
                    raise ValueError("数值必须是有限数值")
            elif value_type == "DATE":
                date_value = (
                    executable_value
                    if type(executable_value) is date
                    else date.fromisoformat(str(executable_value).strip())
                )
            elif value_type == "BOOLEAN":
                if not isinstance(executable_value, bool):
                    raise ValueError("布尔字段的规范值必须是布尔值")
                boolean_value = executable_value
            elif value_type in {"STRING", "CLAUSE", "DECIMAL_CN"}:
                if not isinstance(executable_value, str) or not executable_value.strip():
                    raise ValueError("文本字段的规范值必须是非空文本")
                text_value = executable_value.strip()
            else:
                raise ValueError(f"字段类型不受支持：{value_type}")
        except (InvalidOperation, ValueError) as exc:
            raise BusinessError(
                "TASK_FACT_VALUE_INVALID",
                "字段规范值无法按字段契约写入统一事实",
                status_code=500,
                details={
                    "field_key": field.get("field_key"),
                    "material_key": material_key,
                    "value_type": value_type,
                    "status": status,
                    "reason": str(exc),
                },
            ) from exc
    elif status in {"PRESENT", "REVIEWED"}:
        raise BusinessError(
            "TASK_FACT_VALUE_MISSING",
            "字段已标记为有效，但没有可写入统一事实的规范值",
            status_code=500,
            details={"field_key": field.get("field_key"), "material_key": material_key, "status": status},
        )

    # 同一 (字段, 材料位, 期间, 口径) 已有当前事实时，把它置为非当前再插入新版本。
    # 事实不可变：绝不原地改写（改写会被数据库触发器拒绝）。
    previous = db.scalars(
        select(TaskFact).where(
            TaskFact.audit_task_id == task.id,
            TaskFact.fact_code == str(field.get("source_ref") or field["field_key"]),
            TaskFact.material_key == material_key,
            TaskFact.period_scope == period_scope,
            TaskFact.statement_scope == statement_scope,
            TaskFact.entity_scope == entity_scope,
        )
    ).all()
    revision_no = max((item.revision_no for item in previous), default=0) + 1
    current = [item for item in previous if item.is_current]
    if current:
        db.execute(
            update(TaskFact)
            .where(TaskFact.id.in_([item.id for item in current]))
            .values(is_current=False)
        )
        db.flush()
    db.add(
        TaskFact(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            # 事实编号用业务字段编号（FEX-xxx）：计算参数按它引用，
            # 与字段身份（field_key）是两回事，转换在编译期完成。
            fact_code=str(field.get("source_ref") or field["field_key"]),
            fact_kind="EXTRACTED",
            material_key=material_key,
            entity_scope=entity_scope,
            period_scope=period_scope,
            statement_scope=statement_scope,
            value_type=value_type,
            decimal_value=decimal_value,
            text_value=text_value,
            date_value=date_value,
            boolean_value=boolean_value,
            unit=field.get("unit"),
            currency=field.get("currency"),
            source_execution_id=source_execution_id,
            revision_no=revision_no,
            is_current=True,
            status=status,
        )
    )
    db.flush()
