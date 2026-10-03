"""Version the meeting policy without rewriting historical requirements."""
from alembic import op
import sqlalchemy as sa

revision = "a923f1"
down_revision = "9a0a83a88817"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("customer_files", "media_type", type_=sa.String(100), existing_type=sa.String(50))
    op.add_column("requirement_jobs", sa.Column(
        "business_rules_version", sa.String(30), nullable=False, server_default="legacy"))
    op.alter_column("requirement_jobs", "business_rules_version", server_default=None)


def downgrade():
    op.drop_column("requirement_jobs", "business_rules_version")
