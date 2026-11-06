"""新增 AI 辅助生成规则库的作业、冻结导出与导入关联。

生成作业保存原始 Excel、冻结的生成输入和当前可编辑文档；导出表按修订冻结只读快照，
导入关联表记录冻结导出与真实导入作业的关系及请求幂等。三张表与审核任务 CSV 无关。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime

revision: str = "0006_rulebook_generation"
down_revision: Union[str, Sequence[str], None] = "0005_field_review_confirmation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rulebook_generation_job",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_no", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("source_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("source_size", sa.BigInteger(), nullable=False),
        sa.Column("business_background", sa.Text(), nullable=False),
        sa.Column("generation_requirements", sa.Text(), nullable=False),
        sa.Column("document_schema_version", sa.String(length=40), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("document_json", sa.JSON(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", MySQLDateTime(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.Column(
            "updated_at", MySQLDateTime(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_no", name="uq_rulebook_generation_job_no"),
        sa.UniqueConstraint("idempotency_key", name="uq_rulebook_generation_job_idempotency"),
        sa.CheckConstraint("source_size >= 0", name="ck_rulebook_generation_source_size"),
    )
    op.create_index(
        "idx_rulebook_generation_created_at_id", "rulebook_generation_job", ["created_at", "id"],
    )

    op.create_table(
        "rulebook_generation_export",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("export_no", sa.String(length=40), nullable=False),
        sa.Column("generation_job_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("export_format_version", sa.String(length=40), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("relative_path", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", MySQLDateTime(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("export_no", name="uq_rulebook_generation_export_no"),
        sa.UniqueConstraint(
            "generation_job_id", "revision", name="uq_rulebook_generation_export_revision",
        ),
        sa.CheckConstraint("size >= 0", name="ck_rulebook_generation_export_size"),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["rulebook_generation_job.id"],
            name="fk_rulebook_generation_export_job", ondelete="RESTRICT", onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "idx_rulebook_generation_export_job", "rulebook_generation_export",
        ["generation_job_id", "created_at"],
    )

    op.create_table(
        "rulebook_generation_import",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("generation_export_id", sa.BigInteger(), nullable=False),
        sa.Column("import_job_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("created_at", MySQLDateTime(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "generation_export_id", "idempotency_key", name="uq_rulebook_generation_import_request",
        ),
        sa.UniqueConstraint("import_job_id", name="uq_rulebook_generation_import_job"),
        sa.ForeignKeyConstraint(
            ["generation_export_id"], ["rulebook_generation_export.id"],
            name="fk_rulebook_generation_import_export", ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id"], ["import_job.id"],
            name="fk_rulebook_generation_import_job", ondelete="SET NULL", onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "idx_rulebook_generation_import_export", "rulebook_generation_import", ["generation_export_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_rulebook_generation_import_export", table_name="rulebook_generation_import")
    op.drop_table("rulebook_generation_import")
    op.drop_index("idx_rulebook_generation_export_job", table_name="rulebook_generation_export")
    op.drop_table("rulebook_generation_export")
    op.drop_index("idx_rulebook_generation_created_at_id", table_name="rulebook_generation_job")
    op.drop_table("rulebook_generation_job")
