"""Durable venture-scoped conversion and isolated probe receipts."""
import sqlalchemy as sa
from alembic import op
revision = "s018venturewaitlist"
down_revision = "s017contentventure"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("venture_waitlists",
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column("enabled_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("probe_hash", sa.Text()), sa.Column("probe_expires_at", sa.DateTime(timezone=True)),
        sa.Column("verified_entry_id", sa.Text()), sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.create_index("ix_venture_waitlists_workspace_id", "venture_waitlists", ["workspace_id"])
    op.create_table("venture_waitlist_entries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("venture_waitlists.venture_id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("venture_id", "email", name="uq_venture_waitlist_email"))
    op.create_index("ix_venture_waitlist_entries_workspace_id", "venture_waitlist_entries", ["workspace_id"])
    op.create_index("ix_venture_waitlist_entries_scope", "venture_waitlist_entries", ["workspace_id", "venture_id", "created_at"])


def downgrade():
    op.drop_table("venture_waitlist_entries")
    op.drop_table("venture_waitlists")
