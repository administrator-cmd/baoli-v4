"""新增字段核对确认表。

字段核对是新的业务等待点：抽取完成后任务停在等待核对，人工确认后才继续后续规则。
确认必须绑定「哪一次执行批次、哪一版任务输入、哪一组字段版本」，否则材料换版或
再改一个字段之后，旧确认会继续代表已不存在的输入。

确认行不可变：每次确认新增一行，读取取该批次最新一行；字段或输入变化后旧行保留为历史。
降级只删除该表，不触碰任务、字段事实与人工记录。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime

revision: str = "0005_field_review_confirmation"
down_revision: Union[str, Sequence[str], None] = "0004_rule_result_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "field_review_confirmation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("confirmation_no", sa.String(length=40), nullable=False),
        sa.Column("audit_task_id", sa.BigInteger(), nullable=False),
        sa.Column("node_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("field_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("missing_field_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("missing_field_keys", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("operation_no", sa.String(length=40), nullable=True),
        sa.Column("created_at", MySQLDateTime(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.Column(
            "updated_at", MySQLDateTime(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_no", name="uq_field_review_no"),
        sa.ForeignKeyConstraint(
            ["node_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
            name="fk_field_review_execution_task",
            ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_task_id"], ["audit_task.id"],
            name="fk_field_review_task", ondelete="RESTRICT", onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "idx_field_review_task", "field_review_confirmation", ["audit_task_id", "node_execution_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_field_review_task", table_name="field_review_confirmation")
    op.drop_table("field_review_confirmation")
