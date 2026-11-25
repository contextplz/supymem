"""Add scoped build receipts and explicit credit pools; preserve legacy run provenance."""
from alembic import op
import sqlalchemy as sa

revision = "s006buildruntime"
down_revision = "s005ideaclarify"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM build_runs r LEFT JOIN apps a ON a.id=r.app_id "
                            "WHERE a.id IS NULL OR r.workspace_id<>a.workspace_id OR r.venture_id<>a.venture_id LIMIT 1")).first():
        raise RuntimeError("build_scope_migration_requires_reconciliation")
    if conn.execute(sa.text("SELECT 1 FROM build_events GROUP BY build_run_id, seq HAVING count(*)>1 LIMIT 1")).first():
        raise RuntimeError("build_event_migration_requires_reconciliation")
    with op.batch_alter_table("credit_reservations") as batch:
        batch.add_column(sa.Column("is_pool", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("remaining_micro", sa.BigInteger()))
        batch.create_check_constraint("ck_credit_reservations_pool", "(is_pool AND remaining_micro IS NOT NULL AND remaining_micro >= 0 AND remaining_micro <= amount_micro) OR (NOT is_pool AND remaining_micro IS NULL)")
    with op.batch_alter_table("build_runs") as batch:
        batch.add_column(sa.Column("runtime_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("job_id", sa.Text()))
        batch.add_column(sa.Column("request_digest", sa.Text()))
        batch.add_column(sa.Column("idempotency_key_hash", sa.Text()))
        batch.add_column(sa.Column("created_at", sa.DateTime()))
        batch.add_column(sa.Column("closed_at", sa.DateTime()))
        batch.add_column(sa.Column("event_seq", sa.BigInteger(), nullable=False, server_default="0"))
        batch.create_foreign_key("fk_build_run_job", "jobs", ["job_id"], ["id"])
        batch.create_unique_constraint("uq_build_run_job", ["job_id"])
        batch.create_unique_constraint("uq_build_run_scope", ["id", "workspace_id", "venture_id"])
        batch.create_unique_constraint("uq_build_run_intent", ["app_id", "idempotency_key_hash"])
    op.execute("UPDATE build_runs SET event_seq=COALESCE((SELECT MAX(seq) FROM build_events WHERE build_run_id=build_runs.id),0)")
    predicate = sa.text("runtime_version = 1 AND closed_at IS NULL")
    op.create_index("uq_build_run_open_app", "build_runs", ["app_id"], unique=True,
                    postgresql_where=predicate, sqlite_where=predicate)
    with op.batch_alter_table("build_events") as batch:
        batch.create_unique_constraint("uq_build_event_sequence", ["build_run_id", "seq"])
    op.create_table("build_model_calls",
        sa.Column("id", sa.Text(), primary_key=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in
          ("build_run_id", "workspace_id", "venture_id", "invocation_key", "request_digest", "purpose", "state", "provider", "model")],
        sa.Column("input_event_id", sa.Text()),
        sa.Column("pricing_json", sa.Text(), nullable=False),
        sa.Column("consumed_event_seq", sa.BigInteger()),
        sa.Column("consumed_event_id", sa.Text()),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Text(), sa.ForeignKey("credit_reservations.id")),
        sa.Column("ceiling_micro", sa.BigInteger(), nullable=False),
        *[sa.Column(name, sa.BigInteger()) for name in ("actual_micro", "charged_micro", "prompt_tokens", "completion_tokens")],
        *[sa.Column(name, sa.Text()) for name in ("response_ref", "response_digest", "error_code")],
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("responded_at", sa.DateTime()),
        sa.Column("settled_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["build_run_id", "workspace_id", "venture_id"], ["build_runs.id", "build_runs.workspace_id", "build_runs.venture_id"], name="fk_build_call_scope"),
        sa.UniqueConstraint("build_run_id", "invocation_key", name="uq_build_call_invocation"),
        sa.UniqueConstraint("build_run_id", "sequence", name="uq_build_call_sequence"),
        sa.CheckConstraint("ceiling_micro > 0 AND generation > 0 AND sequence > 0 AND attempt >= 0", name="ck_build_call_bounds"),
        sa.CheckConstraint("actual_micro >= 0 AND charged_micro >= 0 AND charged_micro <= ceiling_micro AND prompt_tokens >= 0 AND completion_tokens >= 0", name="ck_build_call_usage"),
    )

    if conn.dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION credit_pool_identity_guard() RETURNS trigger AS $$
        BEGIN
          IF NEW.is_pool IS DISTINCT FROM OLD.is_pool OR
            ((OLD.is_pool OR EXISTS(SELECT 1 FROM credit_reservations p WHERE p.id=OLD.parent_id AND p.is_pool)) AND
             (NEW.amount_micro IS DISTINCT FROM OLD.amount_micro OR NEW.account_id IS DISTINCT FROM OLD.account_id OR
              NEW.parent_id IS DISTINCT FROM OLD.parent_id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR
              NEW.request_id IS DISTINCT FROM OLD.request_id)) THEN
            RAISE EXCEPTION 'credit_pool_identity_immutable';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute("CREATE TRIGGER trg_credit_pool_identity BEFORE UPDATE ON credit_reservations FOR EACH ROW EXECUTE FUNCTION credit_pool_identity_guard()")
    else:
        op.execute("""CREATE TRIGGER trg_credit_pool_identity BEFORE UPDATE ON credit_reservations
        WHEN NEW.is_pool IS NOT OLD.is_pool OR
             ((OLD.is_pool OR EXISTS(SELECT 1 FROM credit_reservations p WHERE p.id=OLD.parent_id AND p.is_pool)) AND
              (NEW.amount_micro IS NOT OLD.amount_micro OR NEW.account_id IS NOT OLD.account_id OR
               NEW.parent_id IS NOT OLD.parent_id OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.request_id IS NOT OLD.request_id))
        BEGIN SELECT RAISE(ABORT, 'credit_pool_identity_immutable'); END""")


def downgrade():
    # Financial holds and external-call receipts cannot safely be discarded by
    # a live rollback. An artifact rollback must keep this additive schema.
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM build_runs WHERE runtime_version=1 LIMIT 1")).first():
        raise RuntimeError("managed_build_history_requires_forward_migration")
    if conn.execute(sa.text("SELECT 1 FROM credit_reservations WHERE is_pool LIMIT 1")).first():
        raise RuntimeError("credit_pool_history_requires_forward_migration")
    if conn.dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_credit_pool_identity ON credit_reservations")
        op.execute("DROP FUNCTION credit_pool_identity_guard()")
    else:
        op.execute("DROP TRIGGER trg_credit_pool_identity")
    op.drop_table("build_model_calls")
    with op.batch_alter_table("build_events") as batch:
        batch.drop_constraint("uq_build_event_sequence", type_="unique")
    op.drop_index("uq_build_run_open_app", "build_runs")
    with op.batch_alter_table("build_runs") as batch:
        for name in ("uq_build_run_job", "uq_build_run_scope", "uq_build_run_intent"):
            batch.drop_constraint(name, type_="unique")
        batch.drop_constraint("fk_build_run_job", type_="foreignkey")
        for name in ("runtime_version", "job_id", "request_digest", "idempotency_key_hash", "created_at", "closed_at", "event_seq"):
            batch.drop_column(name)
    with op.batch_alter_table("credit_reservations") as batch:
        batch.drop_constraint("ck_credit_reservations_pool", type_="check")
        batch.drop_column("remaining_micro")
        batch.drop_column("is_pool")
