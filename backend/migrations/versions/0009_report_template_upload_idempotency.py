"""报告模板上传请求幂等。"""

import sqlalchemy as sa
from alembic import op

revision = "0009_report_template_upload_key"
down_revision = "0008_report_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL DDL is not transactional: a previous attempt may have added the
    # column before failing to create its unique index. Inspect both pieces so
    # rerunning upgrade after that partial state safely completes the change.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("report_template")}
    if "upload_idempotency_key" not in columns:
        op.add_column("report_template", sa.Column("upload_idempotency_key", sa.String(length=120), nullable=True))
        inspector.clear_cache()

    unique_names = {
        item.get("name")
        for item in inspector.get_unique_constraints("report_template")
    }
    unique_names.update(
        item.get("name")
        for item in inspector.get_indexes("report_template")
        if item.get("unique")
    )
    constraint_name = "uq_report_template_upload_idempotency_key"
    if constraint_name not in unique_names:
        op.create_unique_constraint(
            constraint_name, "report_template", ["upload_idempotency_key"],
        )


def downgrade() -> None:
    op.drop_constraint("uq_report_template_upload_idempotency_key", "report_template", type_="unique")
    op.drop_column("report_template", "upload_idempotency_key")
