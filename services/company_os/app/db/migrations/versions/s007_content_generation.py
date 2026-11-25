"""Company-scoped content authoring and durable metering; leave legacy output unassigned."""
from alembic import op
import sqlalchemy as sa
revision = "s007contentgen"
down_revision = "s006buildruntime"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("content_generation_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("brief_id", sa.Text(), sa.ForeignKey("content_briefs.id"), nullable=False),
        sa.Column("job_id", sa.Text(), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        *[sa.Column(n, sa.Text(), nullable=False) for n in ("actor_id", "kind", "idempotency_hash", "request_digest", "state", "document_json")],
        sa.Column("topic_id", sa.Text()), sa.Column("error_code", sa.Text()),
        sa.Column("ceiling_micro", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("workspace_id", "venture_id", "actor_id", "idempotency_hash", name="uq_content_generation_intent"),
        sa.CheckConstraint("kind IN ('topics', 'draft')", name="ck_content_generation_kind"),
        sa.CheckConstraint("state IN ('queued', 'running', 'completed', 'refused', 'needs_input', 'failed', 'cancelled')", name="ck_content_generation_state"),
        sa.CheckConstraint("ceiling_micro >= 0", name="ck_content_generation_ceiling"))
    op.create_index("ix_content_generation_scope", "content_generation_runs", ["workspace_id", "venture_id", "created_at"])
    for table in ("content_topics", "content_posts"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("generation_run_id", sa.Text()))
            batch.create_foreign_key("fk_" + table + "_generation", "content_generation_runs", ["generation_run_id"], ["id"])
            batch.create_index("ix_" + table + "_generation_run_id", ["generation_run_id"])

def downgrade():
    for table in ("content_posts", "content_topics"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index("ix_" + table + "_generation_run_id")
            batch.drop_constraint("fk_" + table + "_generation", type_="foreignkey")
            batch.drop_column("generation_run_id")
    op.drop_table("content_generation_runs")
