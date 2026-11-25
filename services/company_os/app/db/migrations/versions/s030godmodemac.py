"""God mode: the separate "Allow Mac actions" switch."""

import sqlalchemy as sa
from alembic import op

revision = "s030godmodemac"
down_revision = "s029betacodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("autopilot_settings")}
    if "allow_mac" not in cols:
        op.add_column("autopilot_settings", sa.Column("allow_mac", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("autopilot_settings", "allow_mac")
