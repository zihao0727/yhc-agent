"""Persist PDF extraction batches separately from agent metadata."""
from alembic import op
import sqlalchemy as sa

revision = "b742d8_large_pdf_batches"
down_revision = "a923f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "extraction_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_key", sa.String(255), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "batch_key"),
    )
    op.create_index("ix_extraction_batches_run_id", "extraction_batches", ["run_id"])


def downgrade():
    op.drop_table("extraction_batches")
