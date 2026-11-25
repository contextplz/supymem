"""Privacy-preserving shared company patterns and public research cache."""
import sqlalchemy as sa
from alembic import op

revision = "s050gtmlearning"
down_revision = "s049refreshuse"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("gtm_shared_email_patterns",
        sa.Column("domain", sa.Text(), primary_key=True), sa.Column("pattern", sa.Text(), primary_key=True),
        sa.Column("confidence", sa.Integer(), nullable=False), sa.Column("workspace_count", sa.Integer(), nullable=False),
        sa.Column("published_pattern", sa.Boolean(), nullable=False),
        *[sa.Column(name, sa.Float(), nullable=False) for name in ("sends", "deliveries", "replies", "hard_bounces")],
        sa.Column("retired_at", sa.DateTime()), sa.Column("last_seen", sa.DateTime(), nullable=False))
    op.create_table("gtm_shared_address_validity",
        sa.Column("address_hash", sa.Text(), primary_key=True), sa.Column("status", sa.Text(), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in ("workspace_count", "delivery_workspaces", "reply_workspaces", "hard_bounce_workspaces")],
        *[sa.Column(name, sa.Float(), nullable=False) for name in ("deliveries", "replies", "hard_bounces")],
        sa.Column("suppressed", sa.Boolean(), nullable=False), sa.Column("retired_at", sa.DateTime()),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('unknown','deliverable','retired')", name="ck_gtm_shared_address_status"))
    op.create_table("gtm_shared_email_outcomes",
        sa.Column("event_key", sa.Text(), primary_key=True), sa.Column("address_hash", sa.Text(), nullable=False),
        sa.Column("workspace_hash", sa.Text(), nullable=False), sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("pattern", sa.Text()), sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False))
    for field in ("address_hash", "domain", "observed_at"):
        op.create_index("ix_gtm_shared_email_outcomes_" + field, "gtm_shared_email_outcomes", [field])
    op.create_table("gtm_public_research_cache", sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("response_json", sa.Text(), nullable=False), sa.Column("saved_at", sa.DateTime(), nullable=False))


def downgrade():
    for name in ("gtm_public_research_cache", "gtm_shared_email_outcomes", "gtm_shared_address_validity", "gtm_shared_email_patterns"):
        op.drop_table(name)
