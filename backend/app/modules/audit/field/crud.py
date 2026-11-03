"""字段事实的 SQL 层。"""

from typing import Any

from sqlalchemy import func, select, update

from app.core.base_crud import CRUDBase
from app.modules.audit.field.model import (
    FieldReviewConfirmation,
    ManualReview,
    TaskFact,
    TaskFieldValue,
)
from app.modules.material.document.model import DocumentUpload, TaskDocument


class AuditFieldCRUD(CRUDBase):
    model = TaskFieldValue

    @staticmethod
    def extracted_identity_filter(definitions, *, owner_keys=None):
        """字段事实的身份过滤；给出 owner_keys 时只保留会形成字段事实的定义。"""
        from sqlalchemy import false, tuple_

        from app.constants.execution import FACT_KIND_EXTRACTED
        from app.domain import fact_projection

        if owner_keys is not None:
            definitions = {key: field for key, field in definitions.items() if key in owner_keys}
        identities = list(fact_projection.field_identity_index(definitions))
        if not identities:
            return false()
        return (TaskFact.fact_kind == FACT_KIND_EXTRACTED) & tuple_(
            TaskFact.fact_code, TaskFact.material_key, TaskFact.period_scope,
            TaskFact.statement_scope, TaskFact.entity_scope,
        ).in_(identities)

    def field_versions(
        self, task_id: int, version_id: int, field_key: str, material_key: str,
    ) -> list[TaskFieldValue]:
        return list(self.db.scalars(
            select(TaskFieldValue)
            .where(
                TaskFieldValue.audit_task_id == task_id,
                TaskFieldValue.scenario_version_id == version_id,
                TaskFieldValue.field_key == field_key,
                TaskFieldValue.material_key == material_key,
            )
            .order_by(TaskFieldValue.revision_no)
        ).all())

    def current_candidates(self, task_id: int, version_id: int) -> list[TaskFieldValue]:
        return list(self.db.scalars(
            select(TaskFieldValue)
            .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(
                TaskFieldValue.audit_task_id == task_id,
                TaskFieldValue.scenario_version_id == version_id,
                TaskFieldValue.is_current.is_(True),
                DocumentUpload.audit_task_id == task_id,
                DocumentUpload.is_current.is_(True),
                TaskDocument.audit_task_id == task_id,
                TaskDocument.material_key == TaskFieldValue.material_key,
            )
            .order_by(TaskFieldValue.id)
        ).all())

    def current_extracted_facts(self, task_id: int, version_id: int) -> list[TaskFact]:
        return list(self.db.scalars(
            select(TaskFact).where(
                TaskFact.audit_task_id == task_id,
                TaskFact.scenario_version_id == version_id,
                TaskFact.fact_kind == "EXTRACTED",
                TaskFact.is_current.is_(True),
            )
        ).all())

    def invalidate_pending_reviews_for_versions(
        self, fact_ids: list[int], preserved_review_ids: set[int],
    ) -> int:
        conditions = [
            ManualReview.field_value_id.in_(fact_ids),
            ManualReview.status == "PENDING",
        ]
        if preserved_review_ids:
            conditions.append(ManualReview.id.not_in(sorted(preserved_review_ids)))
        return self.db.execute(
            update(ManualReview)
            .where(*conditions)
            .values(status="INVALIDATED")
        ).rowcount or 0

    def clear_field_values_by_ids(self, fact_ids: list[int]) -> None:
        self.db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(fact_ids))
            .values(is_current=False)
        )
        self.db.flush()

    def facts_by_identity(
        self,
        *,
        task_id: int,
        fact_code: str,
        material_key: str | None,
        period_scope: str,
        statement_scope: str,
        entity_scope: str,
    ) -> list[TaskFact]:
        return list(self.db.scalars(
            select(TaskFact).where(
                TaskFact.audit_task_id == task_id,
                TaskFact.fact_code == fact_code,
                TaskFact.material_key == material_key,
                TaskFact.period_scope == period_scope,
                TaskFact.statement_scope == statement_scope,
                TaskFact.entity_scope == entity_scope,
            )
        ).all())

    def max_fact_revision(
        self,
        *,
        task_id: int,
        fact_code: str,
        material_key: str | None,
        period_scope: str,
        statement_scope: str,
        entity_scope: str,
    ) -> int:
        return int(self.db.scalar(
            select(func.max(TaskFact.revision_no)).where(
                TaskFact.audit_task_id == task_id,
                TaskFact.fact_code == fact_code,
                TaskFact.material_key == material_key,
                TaskFact.period_scope == period_scope,
                TaskFact.statement_scope == statement_scope,
                TaskFact.entity_scope == entity_scope,
            )
        ) or 0)

    def field_values_for_material(self, task_id: int, field_key: str, material_key: str) -> list[TaskFieldValue]:
        return list(self.db.scalars(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.field_key == field_key,
            TaskFieldValue.material_key == material_key,
            TaskFieldValue.is_current.is_(True),
        )).all())

    def max_field_revision(self, task_id: int, field_key: str, material_key: str) -> int:
        return int(self.db.scalar(
            select(func.max(TaskFieldValue.revision_no)).where(
                TaskFieldValue.audit_task_id == task_id,
                TaskFieldValue.field_key == field_key,
                TaskFieldValue.material_key == material_key,
            )
        ) or 0)

    def current_extracted_field_values(self, task_id: int, field_keys, origin: str) -> list[TaskFieldValue]:
        return list(self.db.scalars(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.field_key.in_(field_keys),
            TaskFieldValue.is_current.is_(True),
            TaskFieldValue.origin == origin,
        )).all())

    def clear_fact_ids(self, fact_ids: list[int]) -> None:
        self.db.execute(
            update(TaskFact)
            .where(TaskFact.id.in_(fact_ids))
            .values(is_current=False)
        )
        self.db.flush()

    def clear_current_facts(
        self,
        task_id: int,
        *,
        fact_kind: str | None = None,
        execution_ids: list[int] | None = None,
        fact_codes=None,
        exclude_status: str | None = None,
    ) -> None:
        conditions = [TaskFact.audit_task_id == task_id, TaskFact.is_current.is_(True)]
        if fact_kind is not None:
            conditions.append(TaskFact.fact_kind == fact_kind)
        if execution_ids is not None:
            conditions.append(TaskFact.source_execution_id.in_(execution_ids))
        if fact_codes is not None:
            conditions.append(TaskFact.fact_code.in_(fact_codes))
        if exclude_status is not None:
            conditions.append(TaskFact.status != exclude_status)
        self.db.execute(update(TaskFact).where(*conditions).values(is_current=False))

    def field_value_for_extraction(
        self, task_id: int, field_key: str, upload_id: int, execution_id: int,
    ) -> TaskFieldValue | None:
        return self.db.scalar(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.field_key == field_key,
            TaskFieldValue.document_upload_id == upload_id,
            TaskFieldValue.extraction_execution_id == execution_id,
            TaskFieldValue.is_current.is_(True),
        ))

    def human_field_value(
        self, task_id: int, field_key: str, material_key: str, origin: str,
    ) -> TaskFieldValue | None:
        return self.db.scalar(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.field_key == field_key,
            TaskFieldValue.material_key == material_key,
            TaskFieldValue.is_current.is_(True),
            TaskFieldValue.origin == origin,
        ))

    def human_correction_field_keys(self, task_id: int, origin: str) -> set[str]:
        return set(self.db.scalars(select(TaskFieldValue.field_key).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.is_current.is_(True),
            TaskFieldValue.origin == origin,
        )))

    def current_field_values_for_keys(self, task_id: int, field_keys) -> list[TaskFieldValue]:
        return list(self.db.scalars(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.is_current.is_(True),
            TaskFieldValue.field_key.in_(field_keys),
        )))

    def human_keys_for(self, task_id: int, field_keys, origin: str) -> set[str]:
        if not field_keys:
            return set()
        return set(self.db.scalars(select(TaskFieldValue.field_key).where(
            TaskFieldValue.audit_task_id == task_id,
            TaskFieldValue.is_current.is_(True),
            TaskFieldValue.field_key.in_(field_keys),
            TaskFieldValue.origin == origin,
        )))

    def rule_result_source_pairs(self, task_id: int, pairs):
        from sqlalchemy import tuple_

        from app.modules.audit.rule.model import RuleResult

        return list(self.db.execute(
            select(RuleResult.node_execution_id, RuleResult.rule_code, RuleResult.id).where(
                RuleResult.audit_task_id == task_id,
                tuple_(RuleResult.node_execution_id, RuleResult.rule_code).in_(pairs),
            )
        ).all())

    def extraction_versions(self, task_id: int, version_id: int, origin: str) -> list[TaskFieldValue]:
        return list(self.db.scalars(
            select(TaskFieldValue)
            .where(
                TaskFieldValue.audit_task_id == task_id,
                TaskFieldValue.scenario_version_id == version_id,
                TaskFieldValue.origin == origin,
            )
            .order_by(TaskFieldValue.revision_no, TaskFieldValue.id)
        ).all())

    def latest_reviews(self, task_id: int) -> list[ManualReview]:
        return list(self.db.scalars(
            select(ManualReview)
            .where(ManualReview.audit_task_id == task_id)
            .order_by(ManualReview.id)
        ).all())

    def latest_confirmation(self, task_id: int, execution_id: int) -> FieldReviewConfirmation | None:
        return self.db.scalar(
            select(FieldReviewConfirmation)
            .where(
                FieldReviewConfirmation.audit_task_id == task_id,
                FieldReviewConfirmation.node_execution_id == execution_id,
            )
            .order_by(FieldReviewConfirmation.id.desc())
            .limit(1)
        )

    def field_value_ids_for_executions(self, task_id: int, execution_ids: list[int]) -> list[int]:
        return list(self.db.scalars(
            select(TaskFieldValue.id).where(
                TaskFieldValue.audit_task_id == task_id,
                TaskFieldValue.extraction_execution_id.in_(execution_ids),
                TaskFieldValue.is_current.is_(True),
            )
        ).all())

    def invalidate_pending_reviews_by_field_values(self, field_value_ids: list[int]) -> int:
        return self.db.execute(
            update(ManualReview)
            .where(ManualReview.field_value_id.in_(field_value_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        ).rowcount or 0

    def field_value_ids_for_fields(
        self,
        *,
        task_id: int,
        version_id: int,
        field_keys,
        upload_ids: list[int],
        origin: str,
        exclude_status: str,
    ) -> list[int]:
        if not upload_ids:
            return []
        return list(self.db.scalars(
            select(TaskFieldValue.id).where(
                TaskFieldValue.audit_task_id == task_id,
                TaskFieldValue.scenario_version_id == version_id,
                TaskFieldValue.field_key.in_(field_keys),
                TaskFieldValue.document_upload_id.in_(upload_ids),
                TaskFieldValue.is_current.is_(True),
                TaskFieldValue.origin == origin,
                TaskFieldValue.status != exclude_status,
            )
        ).all())

    def count_current_field_values(self, task_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(TaskFieldValue)
            .where(TaskFieldValue.audit_task_id == task_id, TaskFieldValue.is_current.is_(True))
        )
        return int(self.db.scalar(statement) or 0)

    def count_current_facts(self, task_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(TaskFact)
            .where(TaskFact.audit_task_id == task_id, TaskFact.is_current.is_(True))
        )
        return int(self.db.scalar(statement) or 0)

    def clear_current_field_values(self, task_id: int) -> None:
        self.db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.audit_task_id == task_id, TaskFieldValue.is_current.is_(True))
            .values(is_current=False)
        )

    def invalidate_pending_reviews(self, task_id: int, model: Any) -> int:
        return self.db.execute(
            update(model)
            .where(model.audit_task_id == task_id, model.status == "PENDING")
            .values(status="INVALIDATED")
        ).rowcount
