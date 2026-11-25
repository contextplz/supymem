"""God mode early-access requests (beta interest capture and per-venture allowlist)."""

import sqlalchemy as sa
from alembic import op

revision = "s039godmoderequests"
down_revision = "s038godmodeoffline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "god_mode_requests" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "god_mode_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.Text()),
        sa.CheckConstraint("status IN ('requested','approved','declined')", name="ck_god_mode_requests_status"),
    )
    for col in ("person_id", "workspace_id", "venture_id"):
        op.create_index(f"ix_god_mode_requests_{col}", "god_mode_requests", [col])
    where = sa.text("status IN ('requested','approved')")
    op.create_index("uq_god_mode_requests_active", "god_mode_requests", ["person_id", "venture_id"], unique=True,
                    postgresql_where=where, sqlite_where=where)


def downgrade() -> None:
    op.drop_table("god_mode_requests")
