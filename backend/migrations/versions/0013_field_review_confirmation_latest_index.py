"""Index confirmation history by task and confirmation id."""

import sqlalchemy as sa
from alembic import op


revision = "0013_field_review_latest_idx"
down_revision = "0012_field_review_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("field_review_confirmation")
    }
    if "idx_field_review_task_id" not in existing_indexes:
        op.create_index(
            "idx_field_review_task_id",
            "field_review_confirmation",
            ["audit_task_id", "id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing_indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("field_review_confirmation")
    }
    if "idx_field_review_task_id" in existing_indexes:
        op.drop_index("idx_field_review_task_id", table_name="field_review_confirmation")
