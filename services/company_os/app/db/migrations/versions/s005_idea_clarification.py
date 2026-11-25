"""Persist metered idea clarification receipts across worker retries."""
from alembic import op
import sqlalchemy as sa

revision = "s005ideaclarify"
down_revision = "s004oauthstate"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("idea_clarification_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("session_id", sa.Text(), sa.ForeignKey("idea_sessions.id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("revision_id", sa.Text(), sa.ForeignKey("idea_revisions.id"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("response", sa.Text()),
        sa.Column("usage", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    for name in ("session_id", "workspace_id", "venture_id"):
        op.create_index("ix_idea_clarification_runs_" + name, "idea_clarification_runs", [name])


def downgrade():
    op.drop_table("idea_clarification_runs")
