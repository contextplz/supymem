"""Company-bound immutable publication intents; no send job or activation."""
from alembic import op
import sqlalchemy as sa

revision = "s009contentpub"
down_revision = "s008staticqa"
branch_labels = None
depends_on = None

SCOPE_KEYS = (
    ("credit_accounts", "uq_credit_account_publication_scope", ["id", "workspace_id", "venture_id"]),
    ("content_accounts", "uq_content_account_publication_scope", ["id", "workspace_id", "platform"]),
    ("content_generation_runs", "uq_content_generation_publication_scope", ["id", "workspace_id", "venture_id"]),
    ("content_posts", "uq_content_post_publication_scope", ["id", "workspace_id", "generation_run_id"]),
)
IMMUTABLE = ("id", "workspace_id", "venture_id", "post_id", "source_run_id", "review_run_id", "actor_subject",
    "account_id", "platform", "credit_account_id", "idempotency_hash", "request_digest", "revision_digest",
    "target_digest", "account_identity_digest", "price_digest", "ceiling_micro", "spec_json", "created_at")
UNRESOLVED = "state IN ('waiting_review','waiting_approval','approved','queued','running','receipt_saved','blocked','needs_reconciliation')"


def upgrade():
    for table, name, columns in SCOPE_KEYS:
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(name, columns)
    with op.batch_alter_table("content_accounts") as batch:
        batch.add_column(sa.Column("auth_generation", sa.Integer(), nullable=False, server_default="1"))
        batch.create_check_constraint("ck_content_account_auth_generation", "auth_generation >= 1")
    op.create_table("content_publication_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("post_id", "source_run_id", "review_run_id", "actor_subject",
            "account_id", "platform", "credit_account_id", "idempotency_hash", "request_digest", "revision_digest",
            "target_digest", "account_identity_digest", "price_digest", "state", "spec_json")],
        sa.Column("approval_id", sa.Text(), sa.ForeignKey("approvals.id"), unique=True),
        sa.Column("job_id", sa.Text(), sa.ForeignKey("jobs.id"), unique=True),
        sa.Column("ceiling_micro", sa.BigInteger(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"), sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["source_run_id", "workspace_id", "venture_id"], ["content_generation_runs.id", "content_generation_runs.workspace_id", "content_generation_runs.venture_id"], name="fk_publication_generation_scope"),
        sa.ForeignKeyConstraint(["review_run_id", "workspace_id", "venture_id"], ["content_generation_runs.id", "content_generation_runs.workspace_id", "content_generation_runs.venture_id"], name="fk_publication_review_scope"),
        sa.ForeignKeyConstraint(["post_id", "workspace_id", "source_run_id"], ["content_posts.id", "content_posts.workspace_id", "content_posts.generation_run_id"], name="fk_publication_post_scope"),
        sa.ForeignKeyConstraint(["account_id", "workspace_id", "platform"], ["content_accounts.id", "content_accounts.workspace_id", "content_accounts.platform"], name="fk_publication_account_scope"),
        sa.ForeignKeyConstraint(["credit_account_id", "workspace_id", "venture_id"], ["credit_accounts.id", "credit_accounts.workspace_id", "credit_accounts.venture_id"], name="fk_publication_credit_scope"),
        sa.UniqueConstraint("workspace_id", "venture_id", "actor_subject", "idempotency_hash", name="uq_content_publication_intent"),
        sa.CheckConstraint("state IN ('waiting_review','waiting_approval','approved','queued','running','receipt_saved','completed','rejected','stale','blocked','failed_definitive','needs_reconciliation','cancelled')", name="ck_content_publication_state"),
        sa.CheckConstraint("ceiling_micro >= 0", name="ck_content_publication_ceiling"))
    op.create_index("ix_content_publication_scope", "content_publication_runs", ["workspace_id", "venture_id", "created_at"])
    op.create_index("uq_content_publication_unresolved", "content_publication_runs", ["post_id"], unique=True,
        postgresql_where=sa.text(UNRESOLVED), sqlite_where=sa.text(UNRESOLVED))
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        changes = " OR ".join(f"NEW.{name} IS DISTINCT FROM OLD.{name}" for name in IMMUTABLE)
        op.execute(f"""CREATE FUNCTION content_publication_spec_immutable() RETURNS trigger AS $$
          BEGIN IF {changes} THEN RAISE EXCEPTION 'content publication specification is immutable'; END IF;
          RETURN NEW; END; $$ LANGUAGE plpgsql""")
        op.execute("CREATE TRIGGER content_publication_spec_immutable BEFORE UPDATE ON content_publication_runs FOR EACH ROW EXECUTE FUNCTION content_publication_spec_immutable()")
    elif dialect == "sqlite":
        changes = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in IMMUTABLE)
        op.execute(f"CREATE TRIGGER content_publication_spec_immutable BEFORE UPDATE ON content_publication_runs WHEN {changes} BEGIN SELECT RAISE(ABORT, 'content publication specification is immutable'); END")


def downgrade():
    op.drop_table("content_publication_runs")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS content_publication_spec_immutable()")
    with op.batch_alter_table("content_accounts") as batch:
        batch.drop_constraint("ck_content_account_auth_generation", type_="check")
        batch.drop_column("auth_generation")
    for table, name, _ in reversed(SCOPE_KEYS):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="unique")
