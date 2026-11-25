"""needs_you_requests: things only the founder can do (CAPTCHA, login, consent, payment...)."""
import sqlalchemy as sa
from alembic import op

revision = "s033needsyou"
down_revision = "s032emails"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "needs_you_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("what_we_need", sa.Text(), nullable=False, server_default=""),
        sa.Column("how_to_resume", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocking_ref", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="normal"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("open_key", sa.Text(), unique=True),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.Text()),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_needs_you_requests_workspace_id", "needs_you_requests", ["workspace_id"])
    op.create_index("ix_needs_you_requests_venture_id", "needs_you_requests", ["venture_id"])


def downgrade():
    op.drop_table("needs_you_requests")
