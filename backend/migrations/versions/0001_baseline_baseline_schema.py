"""历史基线：2026-10-26 精简之前的初始结构。

本版本不创建表。已有数据库若停在此版本，执行 `alembic upgrade head`；
空数据库直接执行当前的 `backend/resource/schema.sql`，再 `alembic stamp head`，
不要 stamp 本版本后再升级。后续结构变更由 `alembic revision --autogenerate` 生成。
"""

from typing import Sequence, Union

from alembic import context, op
from sqlalchemy import inspect

revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 与 backend/app/runtime/schema_check.py 的必需表清单保持一致。
REQUIRED_TABLES = (
    "scenario_template",
    "background_operation",
    "import_job",
    "scenario_version",
    "scenario_node",
    "scenario_draft",
    "audit_plan",
    "audit_task",
    "audit_report",
    "operation_log",
    "task_document",
    "document_upload",
    "stored_artifact",
    "node_execution",
    "task_field_value",
    "task_fact",
    "rule_result",
    "manual_review",
)


def upgrade() -> None:
    """确认基线结构已由 schema.sql 建好；空库直接 upgrade 时明确报错。"""
    if context.is_offline_mode():
        return
    present = set(inspect(op.get_bind()).get_table_names())
    missing = sorted(set(REQUIRED_TABLES) - present)
    if missing:
        raise RuntimeError(
            "基线迁移要求数据库已有 backend/resource/schema.sql 的结构；缺少表："
            + "、".join(missing)
        )


def downgrade() -> None:
    """基线版本不删除任何表。"""
