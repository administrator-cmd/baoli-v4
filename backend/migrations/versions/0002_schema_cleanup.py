"""删除无依据冗余列、补齐关系约束，并合并材料当前上传指针。

迁移前提：先确认没有孤立业务编号与空字段值，否则外键或非空约束会失败。
降级只能恢复列结构，无法恢复已删除的数据。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0002_schema_cleanup"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _scalar(statement: str) -> int:
    return int(op.get_bind().execute(sa.text(statement)).scalar() or 0)


def _require_clean_data() -> None:
    if context.is_offline_mode():
        return
    checks = {
        "scenario_draft 的 scenario_code 不在 scenario_template": (
            "SELECT COUNT(*) FROM scenario_draft d LEFT JOIN scenario_template t "
            "ON t.scenario_code = d.scenario_code WHERE t.id IS NULL"
        ),
        "import_job 的 scenario_code 不在 scenario_template": (
            "SELECT COUNT(*) FROM import_job j LEFT JOIN scenario_template t "
            "ON t.scenario_code = j.scenario_code WHERE j.scenario_code IS NOT NULL AND t.id IS NULL"
        ),
        "audit_report 的 node_execution_id 不属于同一任务": (
            "SELECT COUNT(*) FROM audit_report r LEFT JOIN node_execution e "
            "ON e.id = r.node_execution_id AND e.audit_task_id = r.audit_task_id "
            "WHERE r.node_execution_id IS NOT NULL AND e.id IS NULL"
        ),
        "manual_review 的 field_value_id 为空": (
            "SELECT COUNT(*) FROM manual_review WHERE field_value_id IS NULL"
        ),
    }
    failed = []
    for name, statement in checks.items():
        count = _scalar(statement)
        if count:
            failed.append(f"{name}：{count} 行")
    if failed:
        raise RuntimeError("迁移前数据不满足约束，请先清理：" + "；".join(failed))


def upgrade() -> None:
    _require_clean_data()

    op.drop_index("idx_audit_report_status", table_name="audit_report")
    op.drop_column("audit_report", "status")
    op.drop_column("audit_report", "report_generated")
    op.create_index("idx_audit_report_execution", "audit_report", ["node_execution_id", "audit_task_id"])
    op.create_foreign_key(
        "fk_audit_report_execution_task",
        "audit_report", "node_execution",
        ["node_execution_id", "audit_task_id"], ["id", "audit_task_id"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    )

    op.drop_index("idx_audit_task_status_phase", table_name="audit_task")
    op.drop_column("audit_task", "current_phase")

    op.drop_column("import_job", "profile_display_version")
    op.create_index("idx_import_job_scenario", "import_job", ["scenario_code"])
    op.create_foreign_key(
        "fk_import_job_template",
        "import_job", "scenario_template",
        ["scenario_code"], ["scenario_code"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    )

    op.drop_index("idx_scenario_version_code_status", table_name="scenario_version")
    op.drop_column("scenario_version", "status")
    op.create_index("idx_scenario_version_code_published", "scenario_version", ["scenario_code", "published_at"])

    op.drop_column("task_fact", "calculation_status")

    op.drop_index("uq_stored_artifact_upload_type_hash", table_name="stored_artifact")
    op.create_unique_constraint(
        "uq_stored_artifact_upload_type", "stored_artifact", ["document_upload_id", "artifact_type"]
    )
    op.drop_column("stored_artifact", "upload_sha256")

    op.drop_constraint("fk_task_document_current_upload", "task_document", type_="foreignkey")
    op.drop_index("idx_task_document_current_upload", table_name="task_document")
    op.drop_column("task_document", "current_upload_id")

    op.drop_constraint("fk_manual_review_upload_task", "manual_review", type_="foreignkey")
    op.drop_constraint("fk_manual_review_rule_result_task", "manual_review", type_="foreignkey")
    op.drop_index("idx_manual_review_upload", table_name="manual_review")
    op.drop_index("idx_manual_review_rule_result", table_name="manual_review")
    op.drop_column("manual_review", "document_upload_id")
    op.drop_column("manual_review", "rule_result_id")
    op.drop_column("manual_review", "review_type")
    op.drop_column("manual_review", "proposed_value")
    op.alter_column("manual_review", "field_value_id", existing_type=sa.BigInteger(), nullable=False)

    op.create_foreign_key(
        "fk_scenario_draft_template",
        "scenario_draft", "scenario_template",
        ["scenario_code"], ["scenario_code"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_scenario_draft_template", "scenario_draft", type_="foreignkey")

    op.alter_column("manual_review", "field_value_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("manual_review", sa.Column("proposed_value", sa.Text(), nullable=True))
    op.add_column("manual_review", sa.Column("review_type", sa.String(length=40), nullable=False, server_default="FIELD_CONFIRMATION"))
    op.add_column("manual_review", sa.Column("rule_result_id", sa.BigInteger(), nullable=True))
    op.add_column("manual_review", sa.Column("document_upload_id", sa.BigInteger(), nullable=True))
    op.create_index("idx_manual_review_rule_result", "manual_review", ["rule_result_id"])
    op.create_index("idx_manual_review_upload", "manual_review", ["document_upload_id"])
    op.create_foreign_key(
        "fk_manual_review_rule_result_task",
        "manual_review", "rule_result",
        ["rule_result_id", "audit_task_id"], ["id", "audit_task_id"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    )
    op.create_foreign_key(
        "fk_manual_review_upload_task",
        "manual_review", "document_upload",
        ["document_upload_id", "audit_task_id"], ["id", "audit_task_id"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    )

    op.add_column("task_document", sa.Column("current_upload_id", sa.BigInteger(), nullable=True))
    op.create_index("idx_task_document_current_upload", "task_document", ["current_upload_id", "id"])
    op.create_foreign_key(
        "fk_task_document_current_upload",
        "task_document", "document_upload",
        ["current_upload_id", "id"], ["id", "task_document_id"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    )

    op.add_column("stored_artifact", sa.Column("upload_sha256", sa.CHAR(length=64), nullable=False, server_default=""))
    op.drop_constraint("uq_stored_artifact_upload_type", "stored_artifact", type_="unique")
    op.create_unique_constraint(
        "uq_stored_artifact_upload_type_hash",
        "stored_artifact", ["document_upload_id", "artifact_type", "upload_sha256"],
    )

    op.add_column("task_fact", sa.Column("calculation_status", sa.String(length=30), nullable=True))

    op.drop_index("idx_scenario_version_code_published", table_name="scenario_version")
    op.add_column("scenario_version", sa.Column("status", sa.String(length=30), nullable=False, server_default="PUBLISHED"))
    op.create_index("idx_scenario_version_code_status", "scenario_version", ["scenario_code", "status", "published_at"])

    op.drop_constraint("fk_import_job_template", "import_job", type_="foreignkey")
    op.drop_index("idx_import_job_scenario", table_name="import_job")
    op.add_column("import_job", sa.Column("profile_display_version", sa.String(length=40), nullable=True))

    op.add_column("audit_task", sa.Column("current_phase", sa.String(length=40), nullable=False, server_default="AWAITING_CONFIRMATION"))
    op.create_index("idx_audit_task_status_phase", "audit_task", ["status", "current_phase"])

    op.drop_constraint("fk_audit_report_execution_task", "audit_report", type_="foreignkey")
    op.drop_index("idx_audit_report_execution", table_name="audit_report")
    op.add_column("audit_report", sa.Column("report_generated", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("audit_report", sa.Column("status", sa.String(length=30), nullable=False, server_default="GENERATED"))
    op.create_index("idx_audit_report_status", "audit_report", ["status"])
