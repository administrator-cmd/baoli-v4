"""Keep resumable initial generation separate from published revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0011_generation_checkpoint"
down_revision = "0010_operation_llm_response"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rulebook_generation_job", sa.Column("checkpoint_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rulebook_generation_job", "checkpoint_json")
