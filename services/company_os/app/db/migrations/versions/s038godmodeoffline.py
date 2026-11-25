"""God mode: "Keep working while my Mac is off" switch."""

import sqlalchemy as sa
from alembic import op

revision = "s038godmodeoffline"
down_revision = "s037scopedmemory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("autopilot_settings")}
    if "continue_when_offline" not in cols:
        op.add_column("autopilot_settings", sa.Column("continue_when_offline", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("autopilot_settings", "continue_when_offline")
