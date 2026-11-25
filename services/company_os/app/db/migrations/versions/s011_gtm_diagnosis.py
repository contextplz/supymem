"""Venture-scoped GTM diagnosis intents; legacy plans remain unmodified."""
from alembic import op
import sqlalchemy as sa
revision = "s011gtmdiagnosis"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("gtm_god_generation_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("plan_id", sa.Text(), sa.ForeignKey("gtm_god_plans.id"), nullable=False, unique=True),
        sa.Column("job_id", sa.Text(), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        *[sa.Column(n, sa.Text(), nullable=False) for n in ("actor_id", "kind", "idempotency_hash", "request_digest", "state", "document_json")],
        sa.Column("error_code", sa.Text()),
        sa.Column("ceiling_micro", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("id", "workspace_id", "venture_id", name="uq_gtm_god_generation_scope_identity"),
        sa.UniqueConstraint("workspace_id", "venture_id", "actor_id", "idempotency_hash", name="uq_gtm_god_generation_intent"),
        sa.CheckConstraint("kind IN ('diagnose')", name="ck_gtm_god_generation_kind"),
        sa.CheckConstraint("state IN ('queued', 'running', 'completed', 'needs_input', 'failed', 'cancelled')", name="ck_gtm_god_generation_state"),
        sa.CheckConstraint("ceiling_micro >= 0", name="ck_gtm_god_generation_ceiling"))
    op.create_index("ix_gtm_god_generation_scope", "gtm_god_generation_runs", ["workspace_id", "venture_id", "created_at"])

def downgrade():
    op.drop_table("gtm_god_generation_runs")
