"""字段事实与字段抽取。

事实不可变：每次抽取或人工裁决都新增一个版本，旧行退出当前位但保留可追溯。唯一键
(任务, 字段, 材料位) 上只有一条 `is_current`，且该范围不含上传版本——旧上传版本的当前事实
也必须让位，否则新事实写不进去。

规则模型产出由 rule_execution 保存；本模块负责实际事实读取、人工纠错与版本失效。
人工确认值不被模型产出覆盖。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import (
    ENTITY_SCOPE_SELF,
    FACT_KIND_EXTRACTED,
    ORIGIN_HUMAN_CORRECTION,
)
from app.constants.status import EXECUTABLE_FIELD_STATUSES, FIELD_STATUS_REVIEWED
from app.domain.audit import fact_projection
from app.domain.configuration.field_strategy import normalize_field_scope
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.model import TaskFact, TaskFieldValue
from app.modules.audit.field.service import value_store as task_value_service
from app.modules.audit.shared import _task_field_definitions
from app.modules.audit.task.model import AuditTask


class AuditFieldFactsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _task_field_facts(self, task: AuditTask,
        field_key: str,
        material_key: str,
    ) -> list[TaskFieldValue]:
        """同一 (任务, 字段, 材料位) 的全部事实版本，含历史上传版本，按版本号升序。

        current_fact_scope 唯一键不含 document_upload_id，所以“当前事实”的范围是整个
        (任务, 字段, 材料位)：清理当前位必须按这个范围做，否则旧上传版本还占着当前位时新事实写不进去。
        """
        return AuditFieldCRUD(self.db).field_versions(
            task.id, task.scenario_version_id, field_key, material_key,
        )


    def current_field_fact_pairs(self, task: AuditTask,
    ) -> dict[tuple[str, str], tuple[TaskFieldValue, TaskFact]]:
        """读取当前字段候选与统一事实，并核对两者是否仍表示同一份业务事实。

        `TaskFieldValue` 只负责原文、候选值和证据；`TaskFact` 决定有效状态与可执行值。
        页面和报告需要同时展示两类信息，因此必须在这里按任务冻结字段定义配对，不能让
        调用方单独读取候选表后把它的状态或规范值当成正式事实。
        """
        definitions = _task_field_definitions(task)
        candidate_rows = AuditFieldCRUD(self.db).current_candidates(
            task.id, task.scenario_version_id,
        )
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

        task_fact_rows = AuditFieldCRUD(self.db).current_extracted_facts(
            task.id, task.scenario_version_id,
        )
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
                normalize_field_scope(
                    field, "period_scope", status_code=500
                ),
                normalize_field_scope(
                    field, "statement_scope", status_code=500
                ),
                str(field.get("entity_scope") or ENTITY_SCOPE_SELF),
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
            pairs[key] = (candidate, canonical)

        return pairs


    def _next_field_revision(self, facts: list[TaskFieldValue], document_upload_id: int) -> int:
        """同一上传版本内的下一个版本号（唯一键含 document_upload_id + revision_no）。"""
        return max((item.revision_no for item in facts if item.document_upload_id == document_upload_id), default=0) + 1


    def _write_human_field_fact(self, task: AuditTask,
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
        且该范围不含上传版本），新行不带抽取执行批次。确认原值时可引用旧版本原文；
        人工改写取值时不自动沿用旧引文。旧行、旧复核与旧依据保持可追溯。
        返回 (新事实, 上一版版本号, 本次失效的旧待办数量)。正在本事务里裁决的待办
        通过 preserved_review_ids 排除；Session 关闭了 autoflush，不能依赖内存里已改成
        RESOLVED 的状态让 SQL 自动避开它。
        """
        facts = self._task_field_facts( task, field_key, material_key)
        current = next((item for item in facts if item.is_current), None)
        previous_revision_no = current.revision_no if current is not None else None
        invalidated_reviews = 0
        if facts:
            fact_ids = [item.id for item in facts]
            # 新事实一旦占据当前位，依据旧版本创建的待办就不能继续提交。把失效与
            # 事实换版放在同一事务里，避免任务仍展示旧待办、提交时才发现它已过期。
            invalidated_reviews = AuditFieldCRUD(self.db).invalidate_pending_reviews_for_versions(
                fact_ids, preserved_review_ids,
            )
            AuditFieldCRUD(self.db).clear_field_values_by_ids(fact_ids)
        fact = TaskFieldValue(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            field_key=field_key,
            material_key=material_key,
            document_upload_id=document_upload_id,
            revision_no=self._next_field_revision(facts, document_upload_id),
            is_current=True,
            status=status,
            extraction_execution_id=None,
            origin=ORIGIN_HUMAN_CORRECTION,
        )
        AuditFieldCRUD(self.db).add(fact)
        field = _task_field_definitions(task).get(field_key)
        if field is None or field["material_key"] != material_key:
            raise BusinessError(
                "TASK_FIELD_DEFINITION_MISSING", "人工纠错的字段不在任务冻结的字段定义中",
                status_code=500, details={"field_key": field_key, "material_key": material_key},
            )
        # 候选与有效事实都明确保存声明的单位、币种。
        evidence = []
        if inherit_evidence_from_id is not None:
            from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
            source = next((item for item in facts if item.id == inherit_evidence_from_id), None)
            if source is None or source.document_upload_id != document_upload_id or status != FIELD_STATUS_REVIEWED:
                raise BusinessError("TASK_FIELD_EVIDENCE_INHERIT_INVALID", "只能在确认同一上传版本原值时保留原文依据")
            previous_value = task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_TASK_FIELD_VALUE, source.id,
            ).get("normalized_value_json")
            if (fact_projection.canonical_field_value(previous_value, field)
                    != fact_projection.canonical_field_value(normalized_value, field)):
                raise BusinessError("TASK_FIELD_EVIDENCE_INHERIT_INVALID", "人工改写取值不能沿用旧原文依据")
            AuditFieldEvidenceService(self.db).field_evidence( task, inherit_evidence_from_id)
            evidence = [{"source_field_value_id": inherit_evidence_from_id}]
        task_value_service.AuditFieldValueStoreService(self.db).append_values(task, [(task_value_service.RECORD_TASK_FIELD_VALUE, fact.id,
                        {"raw_value": raw_value, "normalized_value_json": normalized_value,
                         "unit": field.get("unit"), "currency": field.get("currency"), "evidence": evidence})]
        )
        self._write_field_task_fact(
        
            task,
            source_execution_id=None,
            field=field,
            material_key=material_key,
            status=status,
            normalized_value=normalized_value if status == FIELD_STATUS_REVIEWED else None,
        )
        return fact, previous_revision_no, int(invalidated_reviews)


    def _write_field_task_fact(self, task: AuditTask,
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

        有效值按声明的类型写入 CSV 业务列，无法转换时明确失败。
        """
        # 按字段实际声明的期间与报表口径区分事实；未声明时表示不适用，
        # 不推断本期、上期、合并或母公司。
        period_scope = normalize_field_scope(
            field, "period_scope", status_code=500
        )
        statement_scope = normalize_field_scope(
            field, "statement_scope", status_code=500
        )
        entity_scope = str(field.get("entity_scope") or ENTITY_SCOPE_SELF)
        value_type = str(field.get("value_type") or "DECIMAL")
        # task_field_value 保存抽取候选，供人工复核；task_fact 是计算与 Agent 使用的
        # 统一事实。证据或标准化尚未通过的候选不能在统一事实中携带可执行值。
        executable_value = normalized_value if status in EXECUTABLE_FIELD_STATUSES else None
        executable_fact_value = None
        if executable_value is not None:
            executable_fact_value = fact_projection.canonical_fact_value(executable_value, field)
        elif status in EXECUTABLE_FIELD_STATUSES:
            raise BusinessError(
                "TASK_FACT_VALUE_MISSING",
                "字段已标记为有效，但没有可写入统一事实的规范值",
                status_code=500,
                details={"field_key": field.get("field_key"), "material_key": material_key, "status": status},
            )

        # 同一 (字段, 材料位, 期间, 口径) 已有当前事实时，把它置为非当前再插入新版本。
        previous = AuditFieldCRUD(self.db).facts_by_identity(
            task_id=task.id,
            fact_code=str(field.get("source_ref") or field["field_key"]),
            material_key=material_key,
            period_scope=period_scope,
            statement_scope=statement_scope,
            entity_scope=entity_scope,
        )
        revision_no = max((item.revision_no for item in previous), default=0) + 1
        current = [item for item in previous if item.is_current]
        if current:
            AuditFieldCRUD(self.db).clear_fact_ids([item.id for item in current])
        task_fact = TaskFact(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            # 事实编号用业务字段编号（FEX-xxx）：计算参数按它引用，
            # 与字段身份（field_key）是两回事，转换在编译期完成。
            fact_code=str(field.get("source_ref") or field["field_key"]),
            fact_kind=FACT_KIND_EXTRACTED,
            material_key=material_key,
            entity_scope=entity_scope,
            period_scope=period_scope,
            statement_scope=statement_scope,
            value_type=value_type,
            source_execution_id=source_execution_id,
            revision_no=revision_no,
            is_current=True,
            status=status,
        )
        AuditFieldCRUD(self.db).add(task_fact)
        # 取值（含单位与币种）写进任务 CSV；表里只留身份、口径与当前位。
        task_value_service.AuditFieldValueStoreService(self.db).append_values(task, [(task_value_service.RECORD_TASK_FACT, task_fact.id,
                        {"value": executable_fact_value, "unit": field.get("unit"), "currency": field.get("currency")})]
        )
