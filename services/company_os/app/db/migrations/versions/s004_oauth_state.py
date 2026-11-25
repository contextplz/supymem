"""Bind OAuth attempts to founder and provider; invalidate legacy short-lived state."""
from alembic import op
import sqlalchemy as sa

revision = "s004oauthstate"
down_revision = "s003mergecontent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("actor_subject", "provider_user_id", "auth_config_id", "connected_account_id", "outcome"):
        op.add_column("content_oauth_states", sa.Column(name, sa.Text(), nullable=True))
    op.add_column("content_oauth_states", sa.Column("auth_method", sa.Text(), server_default="native", nullable=False))
    # Old states have no signed founder binding and may contain plaintext PKCE.
    # They last fifteen minutes; restarting them is safer than guessing identity.
    op.execute("UPDATE content_oauth_states SET code_verifier = NULL, consumed_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.execute("DELETE FROM content_oauth_states")
    with op.batch_alter_table("content_oauth_states") as batch:
        for name in ("actor_subject", "provider_user_id", "auth_config_id", "connected_account_id", "outcome", "auth_method"):
            batch.drop_column(name)
