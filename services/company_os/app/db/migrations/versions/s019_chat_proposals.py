"""Durable actor-bound chat confirmation proposals."""
import sqlalchemy as sa
from alembic import op
revision = "s019chatproposals"
down_revision = "s018venturewaitlist"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("chat_proposals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("receipt_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_chat_proposals_workspace_id", "chat_proposals", ["workspace_id"])
    op.create_index("ix_chat_proposals_venture_id", "chat_proposals", ["venture_id"])


def downgrade():
    op.drop_table("chat_proposals")
