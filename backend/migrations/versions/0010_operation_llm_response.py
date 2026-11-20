"""按后台操作记录每次导入解析响应。适用于已清空业务数据的验证环境。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0010_operation_llm_response"
down_revision = "0009_report_template_upload_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_llm_response",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("created_at", DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["operation_id"], ["background_operation.id"], name="fk_llm_response_operation", ondelete="CASCADE"),
        sa.UniqueConstraint("relative_path", name="uq_llm_response_path"),
    )
    op.create_index("idx_llm_response_operation", "operation_llm_response", ["operation_id", "id"])
    op.drop_column("background_operation", "raw_response_path")


def downgrade() -> None:
    op.add_column("background_operation", sa.Column("raw_response_path", sa.String(500)))
    op.execute("UPDATE background_operation o SET raw_response_path = "
               "(SELECT r.relative_path FROM operation_llm_response r WHERE r.operation_id = o.id ORDER BY r.id DESC LIMIT 1)")
    op.drop_table("operation_llm_response")
