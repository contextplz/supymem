"""Scoped real import intents and immutable source snapshots, no BuildRun fabrication."""
from alembic import op
import sqlalchemy as sa

revision = "s010importintake"
down_revision = "s009contentpub"
branch_labels = None
depends_on = None

IDENTITY = ("id", "workspace_id", "venture_id", "app_id", "actor_subject", "request_id",
    "source_kind", "idempotency_hash", "input_digest", "policy_version", "requested_source_json", "created_at")
SNAPSHOT = ("id", "workspace_id", "venture_id", "app_id", "import_run_id", "normalized_archive_sha256",
    "size_bytes", "manifest_digest", "manifest_json", "storage_receipt_json", "source_provenance_json", "analysis_policy_version", "created_at")


def _immutable(table, columns):
    name = table + "_identity_immutable"
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        changes = " OR ".join(f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in columns)
        op.execute(f"""CREATE FUNCTION {name}() RETURNS trigger AS $$
          BEGIN IF {changes} THEN RAISE EXCEPTION 'import identity is immutable'; END IF;
          RETURN NEW; END; $$ LANGUAGE plpgsql""")
        op.execute(f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()")
    elif dialect == "sqlite":
        changes = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
        op.execute(f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {changes} BEGIN SELECT RAISE(ABORT, 'import identity is immutable'); END")


def upgrade():
    with op.batch_alter_table("apps") as batch:
        batch.create_unique_constraint("uq_app_import_scope", ["id", "workspace_id", "venture_id"])
    with op.batch_alter_table("jobs") as batch:
        batch.create_unique_constraint("uq_job_import_scope", ["id", "workspace_id", "venture_id"])
    op.create_table("import_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("app_id", "actor_subject", "source_kind",
            "idempotency_hash", "input_digest", "policy_version", "state", "requested_source_json")],
        sa.Column("request_id", sa.Text(), sa.ForeignKey("founder_requests.id"), nullable=False, unique=True),
        sa.Column("job_id", sa.Text(), unique=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acquisition_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source_snapshot_id", sa.Text()),
        sa.Column("report_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("report_digest", sa.Text()), sa.Column("error_code", sa.Text()),
        *[sa.Column(name, sa.BigInteger(), nullable=False, server_default="0") for name in ("reserved_bytes", "acquired_bytes", "retained_bytes")],
        *[sa.Column(name, sa.DateTime(), nullable=False) for name in ("created_at", "updated_at", "expires_at")],
        sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("id", "workspace_id", "venture_id", "app_id", name="uq_import_run_scope"),
        sa.UniqueConstraint("workspace_id", "venture_id", "app_id", "actor_subject", "idempotency_hash", name="uq_import_run_intent"),
        sa.ForeignKeyConstraint(["app_id", "workspace_id", "venture_id"], ["apps.id", "apps.workspace_id", "apps.venture_id"], name="fk_import_app_scope"),
        sa.ForeignKeyConstraint(["job_id", "workspace_id", "venture_id"], ["jobs.id", "jobs.workspace_id", "jobs.venture_id"], name="fk_import_job_scope"),
        sa.CheckConstraint("state IN ('awaiting_upload','uploading','queued','acquiring','validating','analyzing','report_complete','partial_report','needs_reconciliation','cancel_requested','cancelled','blocked','failed_definitive','expired')", name="ck_import_run_state"),
        sa.CheckConstraint("source_kind IN ('github_public','upload_zip')", name="ck_import_run_source"),
        sa.CheckConstraint("state_version >= 0 AND reserved_bytes >= 0 AND acquired_bytes >= 0 AND retained_bytes >= 0", name="ck_import_run_counters"))
    op.create_index("ix_import_run_scope_created", "import_runs", ["workspace_id", "venture_id", "app_id", "created_at"])
    op.create_table("source_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("workspace_id", "venture_id", "app_id", "normalized_archive_sha256",
            "manifest_digest", "manifest_json", "storage_receipt_json", "source_provenance_json", "analysis_policy_version")],
        sa.Column("import_run_id", sa.Text(), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("id", "import_run_id", "workspace_id", "venture_id", "app_id", name="uq_source_snapshot_scope"),
        sa.ForeignKeyConstraint(["import_run_id", "workspace_id", "venture_id", "app_id"], ["import_runs.id", "import_runs.workspace_id", "import_runs.venture_id", "import_runs.app_id"], name="fk_source_snapshot_import_scope"),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 33554432", name="ck_source_snapshot_size"))
    with op.batch_alter_table("import_runs") as batch:
        batch.create_foreign_key("fk_import_snapshot_scope", "source_snapshots",
            ["source_snapshot_id", "id", "workspace_id", "venture_id", "app_id"], ["id", "import_run_id", "workspace_id", "venture_id", "app_id"])
    _immutable("import_runs", IDENTITY)
    _immutable("source_snapshots", SNAPSHOT)


def downgrade():
    with op.batch_alter_table("import_runs") as batch:
        batch.drop_constraint("fk_import_snapshot_scope", type_="foreignkey")
    for table in ("source_snapshots", "import_runs"):
        op.drop_table(table)
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION IF EXISTS {table}_identity_immutable()")
    for table, name in (("jobs", "uq_job_import_scope"), ("apps", "uq_app_import_scope")):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="unique")
