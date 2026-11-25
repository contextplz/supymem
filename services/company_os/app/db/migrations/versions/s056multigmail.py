"""Nullable Gmail sender defaults and new-message attribution, without history rewrites."""

from alembic import op
import sqlalchemy as sa

revision = "s056multigmail"
down_revision = "s055composioproject"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (
        ("workspaces", "default_gmail_mailbox_id"),
        ("ventures", "default_gmail_mailbox_id"),
        ("gtm_touches", "email_account_id"),
        ("content_oauth_states", "gmail_intent"),
        ("content_oauth_states", "gmail_mailbox_id"),
    ):
        op.add_column(table, sa.Column(column, sa.Text(), nullable=True))
    op.drop_index("uq_gtm_touch_venture_reply", table_name="gtm_touches")
    for name, columns, qualifier in (
        ("uq_gtm_touch_venture_reply", ["workspace_id", "venture_id", "contact_id", "message_id"], "IS NULL"),
        ("uq_gtm_touch_mailbox_reply", ["workspace_id", "venture_id", "email_account_id", "contact_id", "message_id"], "IS NOT NULL"),
    ):
        predicate = sa.text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL AND email_account_id " + qualifier)
        op.create_index(name, "gtm_touches", columns, unique=True, sqlite_where=predicate, postgresql_where=predicate)


def downgrade():
    columns = (
        ("content_oauth_states", "gmail_mailbox_id"),
        ("content_oauth_states", "gmail_intent"),
        ("gtm_touches", "email_account_id"),
        ("ventures", "default_gmail_mailbox_id"),
        ("workspaces", "default_gmail_mailbox_id"),
    )
    for table, column in columns:
        if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1")):
            raise RuntimeError("Gmail mailbox bindings must not be discarded; keep the expanded schema.")
    op.drop_index("uq_gtm_touch_mailbox_reply", table_name="gtm_touches")
    op.drop_index("uq_gtm_touch_venture_reply", table_name="gtm_touches")
    predicate = sa.text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL")
    op.create_index("uq_gtm_touch_venture_reply", "gtm_touches", ["workspace_id", "venture_id", "contact_id", "message_id"],
                    unique=True, sqlite_where=predicate, postgresql_where=predicate)
    for table, column in columns:
        op.drop_column(table, column)
