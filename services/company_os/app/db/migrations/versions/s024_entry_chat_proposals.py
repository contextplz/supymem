"""Pre-venture chat route proposals and durable confirmation receipts."""

import sqlalchemy as sa
from alembic import op

revision = "s024entrychat"
down_revision = "s023sourcepreflight"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Local boot and several PostgreSQL fixtures create ORM tables before
    # Alembic advances the stamp. Keep the migration safe for that path.
    if "entry_chat_proposals" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "entry_chat_proposals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("target_venture_id", sa.Text()),
        sa.Column("skipped_steps_json", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("next_step", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("receipt_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "request_id", name="uq_entry_chat_request"),
    )
    op.create_index("ix_entry_chat_actor_created", "entry_chat_proposals", ["actor_id", "created_at"])


def downgrade() -> None:
    if "entry_chat_proposals" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("entry_chat_proposals")
