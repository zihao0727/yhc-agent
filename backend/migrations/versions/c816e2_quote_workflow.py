"""Persist quote line lifecycle and dependency-based recovery."""
from alembic import op
import sqlalchemy as sa

revision = "c816e2_quote_workflow"
down_revision = "b742d8_large_pdf_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pricing_work_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("requirement_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("line_id", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("engine_version", sa.String(50), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.Column("blocker", sa.JSON(), nullable=False),
        sa.Column("recovery", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "line_id"),
    )
    op.create_index("ix_pricing_work_items_job_id", "pricing_work_items", ["job_id"])
    op.create_index("ix_pricing_work_items_status", "pricing_work_items", ["status"])


def downgrade():
    op.drop_table("pricing_work_items")
