"""Source-bound independent QA receipts; legacy QA remains unverified runtime 0."""
from alembic import op
import sqlalchemy as sa

revision = "s008staticqa"
down_revision = "s007contentgen"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("build_artifacts") as batch:
        batch.create_unique_constraint("uq_build_artifact_scope", ["id", "build_run_id", "app_id", "workspace_id"])
    with op.batch_alter_table("qa_runs") as batch:
        batch.add_column(sa.Column("runtime_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("generation", sa.Integer()))
        batch.add_column(sa.Column("unsupported", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("receipt_json", sa.Text(), nullable=False, server_default="{}"))
        for name in ("managed_build_id", "source_artifact_id", "source_sha256", "source_manifest_digest", "contract_digest", "input_digest", "report_digest"):
            batch.add_column(sa.Column(name, sa.Text()))
        # Nullable managed-only reference does not reinterpret existing legacy
        # rows or require unsafe provenance backfills to deploy the migration.
        batch.create_foreign_key("fk_qa_managed_build_scope", "build_runs", ["managed_build_id", "workspace_id", "venture_id"], ["id", "workspace_id", "venture_id"])
        batch.create_foreign_key("fk_qa_source_scope", "build_artifacts", ["source_artifact_id", "build_run_id", "app_id", "workspace_id"], ["id", "build_run_id", "app_id", "workspace_id"])
        batch.create_index("uq_qa_managed_input", ["build_run_id", "generation", "input_digest"], unique=True,
                           postgresql_where=sa.text("runtime_version = 1"), sqlite_where=sa.text("runtime_version = 1"))
        batch.create_check_constraint("ck_qa_runtime_counts", "runtime_version IN (0, 1) AND (runtime_version = 0 OR (passed >= 0 AND failed >= 0 AND unsupported >= 0))")
        batch.create_check_constraint("ck_qa_managed_receipt", "runtime_version = 0 OR (managed_build_id IS NOT NULL AND managed_build_id = build_run_id "
                                      "AND generation IS NOT NULL AND generation > 0 AND source_artifact_id IS NOT NULL "
                                      "AND source_sha256 IS NOT NULL AND length(source_sha256) = 64 "
                                      "AND source_manifest_digest IS NOT NULL AND length(source_manifest_digest) = 64 "
                                      "AND contract_digest IS NOT NULL AND length(contract_digest) = 64 "
                                      "AND input_digest IS NOT NULL AND length(input_digest) = 64 "
                                      "AND state IN ('queued','provisioning','running','report_ready','passed','failed','uncertain','cancelled'))")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM qa_runs WHERE runtime_version=1 LIMIT 1")).first():
        raise RuntimeError("managed_qa_history_requires_forward_migration")
    with op.batch_alter_table("qa_runs") as batch:
        batch.drop_constraint("ck_qa_managed_receipt", type_="check")
        batch.drop_constraint("ck_qa_runtime_counts", type_="check")
        batch.drop_index("uq_qa_managed_input")
        batch.drop_constraint("fk_qa_source_scope", type_="foreignkey")
        batch.drop_constraint("fk_qa_managed_build_scope", type_="foreignkey")
        for name in ("managed_build_id", "source_artifact_id", "source_sha256", "source_manifest_digest", "contract_digest", "input_digest", "report_digest", "runtime_version", "generation", "unsupported", "receipt_json"):
            batch.drop_column(name)
    with op.batch_alter_table("build_artifacts") as batch:
        batch.drop_constraint("uq_build_artifact_scope", type_="unique")
