"""审核执行记录的 SQL 层。"""

from datetime import datetime

from sqlalchemy import case, func, select

from app.core.base_crud import CRUDBase
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.task.model import AuditTask
from app.modules.scenario.template.model import ScenarioNode, ScenarioVersion
from app.modules.system.operation.model import BackgroundOperation


def json_text(column, path: str):
    """取 JSON 里的标量或数组文本；JSON_EXTRACT 会保留引号，必须再 UNQUOTE。"""
    return func.json_unquote(func.json_extract(column, path))


class AuditExecutionCRUD(CRUDBase):
    model = NodeExecution

    def page_rows(
        self,
        *,
        task_no: str | None,
        node_code: str | None,
        status: str | None,
        started_from: datetime | None,
        started_to: datetime | None,
        page: int,
        page_size: int,
    ):
        conditions = []
        if task_no:
            conditions.append(AuditTask.task_no == task_no)
        if node_code:
            conditions.append(NodeExecution.node_code == node_code)
        if status:
            if status == "INVALIDATED":
                conditions.append(NodeExecution.status == status)
            else:
                displayed_status = case((NodeExecution.status == "INVALIDATED", func.coalesce(
                    NodeExecution.invalidated_from, NodeExecution.status,
                )), else_=NodeExecution.status)
                conditions.append(displayed_status == status)
        if started_from:
            conditions.append(NodeExecution.started_at >= started_from)
        if started_to:
            conditions.append(NodeExecution.started_at <= started_to)

        total = self.db.scalar(
            select(func.count())
            .select_from(NodeExecution)
            .join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
            .where(*conditions)
        ) or 0
        rows = self.db.execute(
            select(
                NodeExecution.id,
                NodeExecution.audit_task_id,
                NodeExecution.node_code,
                NodeExecution.sequence_no,
                NodeExecution.status,
                NodeExecution.started_at,
                NodeExecution.finished_at,
                NodeExecution.failure_code,
                NodeExecution.failure_message,
                NodeExecution.invalidated_from,
                NodeExecution.invalidated_at,
                NodeExecution.invalidation_reason,
                NodeExecution.result_json,
                AuditTask.task_no,
                AuditTask.task_name,
                ScenarioNode.name.label("node_name"),
                ScenarioVersion.scenario_name,
                ScenarioVersion.version_no,
            )
            .join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
            .join(ScenarioVersion, ScenarioVersion.id == NodeExecution.scenario_version_id)
            .outerjoin(
                ScenarioNode,
                (ScenarioNode.scenario_version_id == NodeExecution.scenario_version_id)
                & (ScenarioNode.node_code == NodeExecution.node_code),
            )
            .where(*conditions)
            .order_by(NodeExecution.created_at.desc(), NodeExecution.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return list(rows), int(total)

    def node_by_code(self, version_id: int, node_code: str) -> ScenarioNode | None:
        return self.db.scalar(
            select(ScenarioNode).where(
                ScenarioNode.scenario_version_id == version_id,
                ScenarioNode.node_code == node_code,
            )
        )

    def latest_operation_for_node(self, task_no: str, node_code: str) -> BackgroundOperation | None:
        return self.db.scalar(
            select(BackgroundOperation)
            .where(
                BackgroundOperation.object_no == task_no,
                BackgroundOperation.target_node == node_code,
            )
            .order_by(BackgroundOperation.id.desc())
            .limit(1)
        )

    def by_id(self, execution_id: int) -> NodeExecution | None:
        return self.db.get(NodeExecution, execution_id)

    def running_for_task(self, task_id: int) -> NodeExecution | None:
        return self.db.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.status == "RUNNING",
            )
            .order_by(NodeExecution.id.desc())
        )

    def running_id_for_task(self, task_id: int) -> int | None:
        return self.db.scalar(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.status == "RUNNING",
            ).limit(1)
        )

    def ids_for_nodes(self, task_id: int, node_codes) -> list[int]:
        return list(self.db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.node_code.in_(node_codes),
                NodeExecution.status != "INVALIDATED",
            )
        ).all())

    def succeeded_node_exists(self, task_id: int, node_code: str) -> bool:
        return self.db.scalar(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.node_code == node_code,
                NodeExecution.status == "SUCCEEDED",
            )
        ) is not None

    def invalidate_for_task(self, task_id: int, reason: str) -> int:
        return self._invalidate(
            select(NodeExecution).where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.status != "INVALIDATED",
            ),
            reason,
        )

    def invalidate_by_ids(self, execution_ids: list[int], reason: str) -> int:
        return self._invalidate(
            select(NodeExecution).where(
                NodeExecution.id.in_(execution_ids),
                NodeExecution.status != "INVALIDATED",
            ),
            reason,
        )

    def invalidate_for_node(self, task_id: int, node_code: str, statuses: list[str], reason: str) -> int:
        return self._invalidate(
            select(NodeExecution).where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.node_code == node_code,
                NodeExecution.status.in_(statuses),
                NodeExecution.status != "INVALIDATED",
            ),
            reason,
        )

    def next_sequence(self, task_id: int) -> int:
        return (self.db.scalar(
            select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task_id)
        ) or 0) + 1

    def current_ids(self, task_ids: set[int], node_codes: list[str] | None = None) -> set[int]:
        if not task_ids:
            return set()
        statement = (
            select(func.max(NodeExecution.id))
            .where(
                NodeExecution.audit_task_id.in_(task_ids),
                NodeExecution.status != "INVALIDATED",
            )
            .group_by(NodeExecution.audit_task_id, NodeExecution.node_code)
        )
        if node_codes is not None:
            statement = statement.where(NodeExecution.node_code.in_(node_codes))
        return {int(item) for item in self.db.scalars(statement) if item is not None}

    def executions_by_ids(self, execution_ids: set[int]) -> list[NodeExecution]:
        if not execution_ids:
            return []
        return list(self.db.scalars(select(NodeExecution).where(NodeExecution.id.in_(execution_ids))).all())

    def latest_for_task(self, task_id: int, node_code: str | None = None) -> NodeExecution | None:
        statement = select(NodeExecution).where(
            NodeExecution.audit_task_id == task_id,
            NodeExecution.status != "INVALIDATED",
        )
        if node_code is not None:
            statement = statement.where(NodeExecution.node_code == node_code)
        return self.db.scalar(statement.order_by(NodeExecution.id.desc()).limit(1))

    def pending_rerun_rows(self, execution_ids: set[int]):
        if not execution_ids:
            return []
        return list(self.db.execute(
            select(
                NodeExecution.id,
                json_text(NodeExecution.result_json, "$.pending_rerun_keys"),
            ).where(NodeExecution.id.in_(execution_ids))
        ).all())

    @staticmethod
    def current_execution_subquery(task_id_column, node_code_column):
        from sqlalchemy.orm import aliased

        later = aliased(NodeExecution)
        return (
            select(func.max(later.id))
            .where(
                later.audit_task_id == task_id_column,
                later.node_code == node_code_column,
                later.status != "INVALIDATED",
            )
            .correlate(NodeExecution)
            .scalar_subquery()
        )

    @staticmethod
    def pending_rerun_predicate(rule_key_column):
        return func.coalesce(
            func.json_contains(
                NodeExecution.result_json,
                func.json_quote(rule_key_column),
                "$.pending_rerun_keys",
            ),
            0,
        ) == 1

    def _invalidate(self, statement, reason: str) -> int:
        from datetime import UTC, datetime

        records = list(self.db.scalars(statement))
        now = datetime.now(UTC)
        for execution in records:
            execution.invalidated_from = execution.status
            execution.invalidated_at = now
            execution.invalidation_reason = reason
            execution.status = "INVALIDATED"
        self.db.flush()
        return len(records)

    def waiting_for_node(self, task_id: int, node_code: str) -> NodeExecution | None:
        return self.db.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.audit_task_id == task_id,
                NodeExecution.node_code == node_code,
                NodeExecution.status == "WAITING_FIELD_REVIEW",
            )
            .order_by(NodeExecution.id.desc())
        )

    def interrupted_running(self) -> list[NodeExecution]:
        return list(self.db.scalars(
            select(NodeExecution).join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
            .where(
                NodeExecution.status == "RUNNING",
                AuditTask.status.not_in(["WAITING_REVIEW", "WAITING_FIELD_REVIEW"]),
            )
        ).all())

    def count_for_task(self, task_id: int, *, exclude_invalidated: bool = False) -> int:
        statement = select(func.count()).select_from(NodeExecution).where(NodeExecution.audit_task_id == task_id)
        if exclude_invalidated:
            statement = statement.where(NodeExecution.status != "INVALIDATED")
        return int(self.db.scalar(statement) or 0)

    def count_rule_results_for_task(self, task_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(RuleResult)
            .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
            .where(NodeExecution.audit_task_id == task_id, NodeExecution.status != "INVALIDATED")
        )
        return int(self.db.scalar(statement) or 0)
