"""Durable founder-application runtime intent and phase receipts."""

import sqlalchemy as sa
from alembic import op

revision = "s020appruntime"
down_revision = "s019chatproposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_runtime_intents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("app_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), nullable=False),
        sa.Column("plan_task_id", sa.Text(), nullable=False),
        sa.Column("build_run_id", sa.Text(), nullable=False),
        sa.Column("source_artifact_id", sa.Text(), nullable=False),
        sa.Column("qa_run_id", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("source_manifest_digest", sa.Text(), nullable=False),
        sa.Column("qa_contract_digest", sa.Text(), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("phase", sa.Text(), nullable=False, server_default="source_accepted"),
        sa.Column("receipts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("reservation_id", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        sa.ForeignKeyConstraint(["plan_task_id"], ["plan_tasks.id"]),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_runs.id"]),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["build_artifacts.id"]),
        sa.ForeignKeyConstraint(["qa_run_id"], ["qa_runs.id"]),
        sa.ForeignKeyConstraint(["domain_id"], ["app_domains.id"]),
        sa.ForeignKeyConstraint(["reservation_id"], ["credit_reservations.id"]),
        sa.UniqueConstraint("id", "app_id", "workspace_id", "venture_id", name="uq_app_runtime_intent_scope"),
        sa.UniqueConstraint("app_id", "generation", name="uq_app_runtime_intent_generation"),
        sa.UniqueConstraint("app_id", "request_digest", name="uq_app_runtime_intent_request"),
        sa.CheckConstraint("generation > 0", name="ck_app_runtime_intent_generation"),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND length(source_manifest_digest) = 64 "
            "AND length(qa_contract_digest) = 64 AND length(request_digest) = 64",
            name="ck_app_runtime_intent_digests",
        ),
        sa.CheckConstraint(
            "state IN ('pending','provisioning','reconciling','staged','review','live','failed','cancelled')",
            name="ck_app_runtime_intent_state",
        ),
        sa.CheckConstraint(
            "phase IN ('source_accepted','database_secret_created','database_provisioning',"
            "'auth_provisioned','image_building','image_ready','runtime_migrated','service_staged','preview_live','runtime_qa',"
            "'publication_review','live')",
            name="ck_app_runtime_intent_phase",
        ),
    )
    op.create_index("ix_app_runtime_intents_workspace_id", "app_runtime_intents", ["workspace_id"])
    op.create_index("ix_app_runtime_intents_venture_id", "app_runtime_intents", ["venture_id"])
    predicate = sa.text("state IN ('pending','provisioning','reconciling','staged','review')")
    op.create_index("uq_app_runtime_intent_open", "app_runtime_intents", ["app_id"], unique=True,
                    postgresql_where=predicate, sqlite_where=predicate)


def downgrade() -> None:
    op.drop_index("uq_app_runtime_intent_open", table_name="app_runtime_intents")
    op.drop_index("ix_app_runtime_intents_venture_id", table_name="app_runtime_intents")
    op.drop_index("ix_app_runtime_intents_workspace_id", table_name="app_runtime_intents")
    op.drop_table("app_runtime_intents")
