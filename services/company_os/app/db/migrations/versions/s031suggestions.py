"""Suggestions: tasks Supymem offers to take over, and the founder's decision on each."""

import sqlalchemy as sa
from alembic import op

revision = "s031suggestions"
down_revision = "s030godmodemac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "suggestions" in set(sa.inspect(op.get_bind()).get_table_names()):  # ORM bootstrap may have created it
        return
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False, index=True),
        sa.Column("person_id", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("est_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("requires_mac", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("result_ref", sa.Text()),
    )
    op.create_index("ix_suggestions_venture_status", "suggestions", ["venture_id", "status"])


def downgrade() -> None:
    if "suggestions" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("suggestions")
