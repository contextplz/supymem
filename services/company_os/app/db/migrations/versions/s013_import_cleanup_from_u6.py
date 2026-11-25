"""Apply import-cleanup storage for databases already recorded at u6.

Live staging was last recorded at u6v7w8x9y0z1 using the main parent pair
(s010importintake, t5u6v7w8x9y0). That database never applied
s011importcleanup. This revision is a child of u6. It does not rewrite u6
parents and must not be stamped onto another lineage.

Fresh databases that already applied s011importcleanup skip the DDL.
s012 remains reserved for the parked strategy backend.

DDL below is the s011importcleanup table/trigger contract. Keep them in
lockstep; do not invent a third cleanup schema.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "s013cleanupfromu6"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None

IDENTITY = ("id", "workspace_id", "venture_id", "app_id", "import_run_id", "object_kind", "reason", "initiated_by", "created_at")


def _create_import_object_cleanups() -> None:
    op.create_table("import_object_cleanups",
        sa.Column("id", sa.Text(), primary_key=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("workspace_id", "venture_id", "app_id", "import_run_id", "object_kind", "reason", "initiated_by", "state")],
        sa.Column("job_id", sa.Text(), unique=True),
        *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in ("state_version", "recovery_count")],
        sa.Column("due_at", sa.DateTime(), nullable=False), sa.Column("retired_at", sa.DateTime()),
        sa.Column("operation_owner", sa.Text()),
        *[sa.Column(name, sa.Text(), nullable=False, server_default="{}") for name in ("target_json", "fence_json", "plan_json", "receipt_json")],
        sa.Column("released_bytes", sa.BigInteger(), nullable=False, server_default="0"), sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("import_run_id", "object_kind", name="uq_import_cleanup_object"),
        sa.ForeignKeyConstraint(["import_run_id", "workspace_id", "venture_id", "app_id"], ["import_runs.id", "import_runs.workspace_id", "import_runs.venture_id", "import_runs.app_id"], name="fk_import_cleanup_scope"),
        sa.ForeignKeyConstraint(["job_id", "workspace_id", "venture_id"], ["jobs.id", "jobs.workspace_id", "jobs.venture_id"], name="fk_import_cleanup_job_scope"),
        sa.CheckConstraint("object_kind IN ('raw','source')", name="ck_import_cleanup_kind"),
        sa.CheckConstraint("state IN ('scheduled','fencing','deleting_versions','verifying','complete','needs_attention')", name="ck_import_cleanup_state"),
        sa.CheckConstraint("state_version >= 0 AND recovery_count BETWEEN 0 AND 1 AND released_bytes >= 0", name="ck_import_cleanup_counters"))
    op.create_index("ix_import_cleanup_due", "import_object_cleanups", ["state", "due_at"])
    pg = op.get_bind().dialect.name == "postgresql"
    different = "IS DISTINCT FROM" if pg else "IS NOT"
    changes = " OR ".join(f"NEW.{c} {different} OLD.{c}" for c in IDENTITY)
    for field in ("job_id", "retired_at"):
        changes += f" OR (OLD.{field} IS NOT NULL AND NEW.{field} {different} OLD.{field})"
    for field in ("target_json", "fence_json", "plan_json", "receipt_json"):
        changes += f" OR (OLD.{field} <> '{{}}' AND NEW.{field} {different} OLD.{field})"
    changes += f" OR (OLD.state = 'complete' AND (NEW.state {different} OLD.state OR NEW.released_bytes {different} OLD.released_bytes OR NEW.completed_at {different} OLD.completed_at))"
    if pg:
        op.execute(f"CREATE FUNCTION import_cleanup_immutable() RETURNS trigger AS $$ BEGIN IF {changes} THEN RAISE EXCEPTION 'import cleanup identity is immutable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql")
        op.execute("CREATE TRIGGER import_cleanup_immutable BEFORE UPDATE ON import_object_cleanups FOR EACH ROW EXECUTE FUNCTION import_cleanup_immutable()")
    else:
        op.execute(f"CREATE TRIGGER import_cleanup_immutable BEFORE UPDATE ON import_object_cleanups WHEN {changes} BEGIN SELECT RAISE(ABORT, 'import cleanup identity is immutable'); END")


def upgrade() -> None:
    if inspect(op.get_bind()).has_table("import_object_cleanups"):
        return
    _create_import_object_cleanups()


def downgrade() -> None:
    # Additive. Service rollback keeps this schema. Do not drop a table that
    # s011importcleanup may still own on databases that walked the full graph.
    return
