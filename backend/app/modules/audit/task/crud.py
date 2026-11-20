"""审核任务查询与写入的 SQL 层。"""

from datetime import datetime

from sqlalchemy import delete, func, or_, select

from app.core.base_crud import CRUDBase
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.field.model import (
    FieldReviewConfirmation,
    ManualReview,
    TaskFact,
    TaskFieldValue,
)
from app.modules.audit.report.model import AuditReport
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.plan.plan.model import AuditPlan
from app.modules.scenario.template.model import ScenarioNode, ScenarioTemplate, ScenarioVersion
from app.modules.system.operation.model import BackgroundOperation


class AuditTaskCRUD(CRUDBase):
    model = AuditTask

    def by_no(self, task_no: str, *, lock: bool = False, values: bool = False) -> AuditTask | None:
        statement = select(AuditTask).where(AuditTask.task_no == task_no)
        if lock:
            statement = statement.with_for_update()
        if values:
            statement = statement.execution_options(populate_existing=True)
        return self.db.scalar(statement)

    def lock_by_no(self, task_no: str) -> AuditTask | None:
        return self.db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())

    def running(self) -> list[AuditTask]:
        return list(self.db.scalars(select(AuditTask).where(AuditTask.status == "RUNNING")).all())

    def lock_row_id(self, task_id: int) -> int | None:
        return self.db.scalar(select(AuditTask.id).where(AuditTask.id == task_id).with_for_update())

    def delete_task_data(self, task_id: int, task_no: str) -> None:
        """删除任务及其全部输入、执行与结果记录。"""
        for entity in (
            FieldReviewConfirmation, ManualReview, TaskFieldValue, TaskFact, AuditReport,
            RuleResult, StoredArtifact, DocumentUpload, TaskDocument, NodeExecution,
        ):
            self.db.execute(delete(entity).where(entity.audit_task_id == task_id))
        self.db.execute(delete(BackgroundOperation).where(
            BackgroundOperation.object_type == "AUDIT_TASK",
            BackgroundOperation.object_no == task_no,
        ))

    def page_rows(
        self,
        *,
        name: str | None,
        scenario_code: str | None,
        plan_code: str | None,
        status: str | None,
        current_node: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ):
        conditions = []
        if name:
            like = f"%{name.strip()}%"
            conditions.append(or_(AuditTask.task_name.like(like), AuditTask.task_no.like(like)))
        if scenario_code:
            conditions.append(ScenarioVersion.scenario_code == scenario_code)
        if plan_code:
            conditions.append(AuditPlan.plan_code == plan_code)
        if status:
            conditions.append(AuditTask.status == status)
        if current_node:
            conditions.append(AuditTask.current_node == current_node)
        if created_from:
            conditions.append(AuditTask.created_at >= created_from)
        if created_to:
            conditions.append(AuditTask.created_at <= created_to)

        # 排序阶段只取任务 ID，避免完整规则库、方案配置等 JSON 占用 MySQL 排序缓冲区。
        page_statement = (
            select(AuditTask.id)
            .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
            .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
            .where(*conditions)
        )
        total = self.db.scalar(
            select(func.count())
            .select_from(AuditTask)
            .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
            .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
            .where(*conditions)
        ) or 0
        page_ids = list(self.db.scalars(
            page_statement.order_by(AuditTask.created_at.desc(), AuditTask.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ))
        rows = self.db.execute(
            select(
                AuditTask.id,
                AuditTask.task_no,
                AuditTask.task_name,
                AuditTask.remark,
                AuditTask.current_node,
                AuditTask.status,
                select(NodeExecution.failure_message)
                .where(NodeExecution.audit_task_id == AuditTask.id,
                       NodeExecution.node_code == AuditTask.current_node,
                       NodeExecution.status != "INVALIDATED")
                .order_by(NodeExecution.id.desc()).limit(1).correlate(AuditTask)
                .scalar_subquery().label("block_reason"),
                AuditTask.created_at,
                AuditTask.updated_at,
                AuditTask.task_schema_json["plan_name"].label("frozen_plan_name"),
                ScenarioVersion.scenario_code,
                ScenarioVersion.scenario_name,
                ScenarioVersion.version_no.label("scenario_version_no"),
                ScenarioNode.name.label("current_node_name"),
                AuditPlan.plan_code,
            )
            .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
            .outerjoin(
                ScenarioNode,
                (ScenarioNode.scenario_version_id == AuditTask.scenario_version_id)
                & (ScenarioNode.node_code == AuditTask.current_node),
            )
            .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
            .where(AuditTask.id.in_(page_ids))
        ).all()
        return page_ids, {row.id: row for row in rows}, int(total)

    def option_rows(self, *, keyword: str | None, limit: int = 50):
        conditions = []
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(or_(AuditTask.task_name.like(like), AuditTask.task_no.like(like)))
        return list(self.db.execute(
            select(AuditTask.task_no, AuditTask.task_name, AuditTask.status, ScenarioVersion.scenario_name)
            .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
            .where(*conditions)
            .order_by(AuditTask.created_at.desc(), AuditTask.id.desc())
            .limit(limit)
        ).all())

    def scenario_version(self, version_id: int) -> ScenarioVersion | None:
        return self.db.get(ScenarioVersion, version_id)

    def plan_by_id(self, plan_id: int) -> AuditPlan | None:
        return self.db.get(AuditPlan, plan_id)

    def by_id(self, task_id: int) -> AuditTask | None:
        return self.db.get(AuditTask, task_id)

    def lock_plan(self, plan_id: int) -> AuditPlan | None:
        """锁定方案行并刷新已加载属性。仅锁行而不刷新时，会话可能继续使用加锁前的旧状态。"""
        return self.db.scalar(
            select(AuditPlan)
            .where(AuditPlan.id == plan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def scenario_template(self, scenario_code: str) -> ScenarioTemplate | None:
        return self.db.scalar(
            select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code)
        )

    def rule_results(self, task_id: int, execution_ids: set[int]) -> list[RuleResult]:
        if not execution_ids:
            return []
        return list(self.db.scalars(
            select(RuleResult)
            .where(
                RuleResult.audit_task_id == task_id,
                RuleResult.node_execution_id.in_(execution_ids),
            )
            .order_by(RuleResult.id)
        ).all())

    def uploads_by_ids(self, upload_ids: set[int]) -> list[DocumentUpload]:
        if not upload_ids:
            return []
        return list(self.db.scalars(select(DocumentUpload).where(DocumentUpload.id.in_(upload_ids))).all())

    def artifacts_by_ids(self, artifact_ids: set[int]) -> list[StoredArtifact]:
        if not artifact_ids:
            return []
        return list(self.db.scalars(select(StoredArtifact).where(StoredArtifact.id.in_(artifact_ids))).all())
