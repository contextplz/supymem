"""Email outbox, per-person email preferences and the bounce/complaint suppression list."""
import sqlalchemy as sa
from alembic import op

revision = "s032emails"
down_revision = "s031suggestions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "email_messages",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("to_email", sa.Text(), nullable=False, server_default=""),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("context_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("provider_id", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime()),
    )
    op.create_index("ix_email_messages_person", "email_messages", ["person_id", "created_at"])
    op.create_table(
        "email_preferences",
        sa.Column("person_id", sa.Text(), primary_key=True),
        sa.Column("product_updates", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("engagement_reminders", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("digests", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("suggestions", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "email_suppressions",
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("email_suppressions")
    op.drop_table("email_preferences")
    op.drop_index("ix_email_messages_person", table_name="email_messages")
    op.drop_table("email_messages")
