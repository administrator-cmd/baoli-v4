"""Store immutable field values for each manual-review confirmation."""

import sqlalchemy as sa
from alembic import op

revision = "0012_field_review_snapshot"
down_revision = "0011_generation_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "field_review_confirmation",
        sa.Column("snapshot_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("field_review_confirmation", "snapshot_json")
