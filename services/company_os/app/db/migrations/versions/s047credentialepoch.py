"""Invalidate gateway cookies and desktop devices on password changes."""
import sqlalchemy as sa
from alembic import op

revision = "s047credentialepoch"
down_revision = "s046lootdrop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("gateway_users")}
    if "session_epoch" not in columns:
        op.add_column("gateway_users", sa.Column("session_epoch", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM gateway_users WHERE session_epoch > 0 LIMIT 1")).first():
        raise RuntimeError("credential_epoch_downgrade_would_restore_revoked_sessions")
    op.drop_column("gateway_users", "session_epoch")
