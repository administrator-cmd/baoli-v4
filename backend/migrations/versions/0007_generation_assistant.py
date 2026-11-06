"""规则库对话与不可变修订。只建立新表，不重写历史文档或冻结版本。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0007_generation_assistant"
down_revision = "0006_rulebook_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rulebook_generation_revision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("generation_job_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("operation_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_job_id", "revision", name="uq_generation_revision_job"),
        sa.UniqueConstraint("operation_id", name="uq_generation_revision_operation"),
        sa.CheckConstraint("revision >= 1", name="ck_generation_revision_positive"),
        sa.CheckConstraint("origin IN ('INITIAL','MANUAL','AI','RESTORE','BASELINE')", name="ck_generation_revision_origin"),
        sa.ForeignKeyConstraint(["generation_job_id"], ["rulebook_generation_job.id"], name="fk_generation_revision_job", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["background_operation.id"], name="fk_generation_revision_operation", ondelete="RESTRICT"),
    )
    op.create_table(
        "rulebook_generation_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("generation_job_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", "role", name="uq_generation_message_operation_role"),
        sa.CheckConstraint("role IN ('USER','ASSISTANT')", name="ck_generation_message_role"),
        sa.ForeignKeyConstraint(["operation_id"], ["background_operation.id"], name="fk_generation_message_operation", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_job_id", "revision"],
            ["rulebook_generation_revision.generation_job_id", "rulebook_generation_revision.revision"],
            name="fk_generation_message_revision", ondelete="RESTRICT",
        ),
    )
    op.create_index("idx_generation_message_job_id", "rulebook_generation_message", ["generation_job_id", "id"])


def downgrade() -> None:
    op.drop_table("rulebook_generation_message")
    op.drop_table("rulebook_generation_revision")
