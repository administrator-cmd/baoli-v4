"""报告模板与风格档案。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0008_report_templates"
down_revision = "0007_generation_assistant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_template",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("template_no", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("source_size", sa.BigInteger(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'PROCESSING'"), nullable=False),
        sa.Column("style_profile_json", sa.JSON(), nullable=True),
        sa.Column("analysis_error", sa.Text(), nullable=True),
        sa.Column("created_at", DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.Column(
            "updated_at", DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_no", name="uq_report_template_no"),
        sa.CheckConstraint("status IN ('PROCESSING','READY','FAILED')", name="ck_report_template_status"),
    )
    op.create_index("idx_report_template_status_updated", "report_template", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("idx_report_template_status_updated", table_name="report_template")
    op.drop_table("report_template")
