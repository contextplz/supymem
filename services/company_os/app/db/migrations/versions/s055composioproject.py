"""Allow reconnect holds for accounts moved between Composio projects."""

from alembic import op

revision = "s055composioproject"
down_revision = "s054leadlibrary"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("content_accounts") as batch:
        batch.drop_constraint("ck_content_account_status", type_="check")
        batch.create_check_constraint("ck_content_account_status",
            "status IN ('pending', 'active', 'expired', 'revoked', 'error', 'needs_reconnect')")


def downgrade():
    op.execute("UPDATE content_accounts SET status = 'expired' WHERE status = 'needs_reconnect'")
    with op.batch_alter_table("content_accounts") as batch:
        batch.drop_constraint("ck_content_account_status", type_="check")
        batch.create_check_constraint("ck_content_account_status",
            "status IN ('pending', 'active', 'expired', 'revoked', 'error')")
