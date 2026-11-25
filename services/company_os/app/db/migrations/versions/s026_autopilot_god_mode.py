"""God mode (beta): per-venture autopilot settings and the autopilot timeline."""

import sqlalchemy as sa
from alembic import op

revision = "s026autopilot"
down_revision = "s025analyticsfeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())  # ORM bootstrap may have created them
    if "autopilot_settings" not in have:
        op.create_table(
            "autopilot_settings",
            sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), primary_key=True),
            sa.Column("god_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("daily_cap_credits", sa.Integer(), nullable=False, server_default="200"),
            sa.Column("allow_send", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("send_max_per_day", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "autopilot_events" not in have:
        op.create_table(
            "autopilot_events",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False, index=True),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("what", sa.Text(), nullable=False),
            sa.Column("why", sa.Text(), nullable=False, server_default=""),
            sa.Column("credits_micro", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("outcome", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_autopilot_events_venture_created", "autopilot_events", ["venture_id", "created_at"])


def downgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("autopilot_events", "autopilot_settings"):
        if table in have:
            op.drop_table(table)
