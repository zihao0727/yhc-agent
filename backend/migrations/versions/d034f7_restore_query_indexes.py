"""Restore indexes missing from some previously provisioned local databases."""
from alembic import op

revision = "d034f7_restore_query_indexes"
down_revision = "c816e2_quote_workflow"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_agent_runs_job_id", "agent_runs", "job_id"),
    ("ix_change_logs_entity", "change_logs", "entity"),
    ("ix_customer_files_job_id", "customer_files", "job_id"),
    ("ix_extraction_runs_job_id", "extraction_runs", "job_id"),
    ("ix_price_items_product_id", "price_items", "product_id"),
    ("ix_price_items_status", "price_items", "status"),
    ("ix_price_revisions_price_item_id", "price_revisions", "price_item_id"),
    ("ix_pricing_rules_status", "pricing_rules", "status"),
    ("ix_quote_drafts_job_id", "quote_drafts", "job_id"),
    ("ix_requirement_jobs_status", "requirement_jobs", "status"),
)


def upgrade():
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], if_not_exists=True)


def downgrade():
    # These indexes belong to the initial schema and must survive this repair's rollback.
    pass
