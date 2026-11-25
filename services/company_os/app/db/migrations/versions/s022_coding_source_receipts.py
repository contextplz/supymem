"""Keep immutable transport custody for local coding source."""

import sqlalchemy as sa
from alembic import op

revision = "s022codingsource"
down_revision = "s021noticetargets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "coding_source_receipts" in sa.inspect(op.get_bind()).get_table_names():
        return  # s001 creates current metadata on a fresh database.
    op.create_table(
        "coding_source_receipts",
        sa.Column("order_id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("app_id", sa.Text(), sa.ForeignKey("apps.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("plan_task_id", sa.Text(), nullable=False),
        sa.Column("execution_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("receipt_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stored_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('awaiting_upload','stored','needs_reconciliation')", name="ck_coding_source_state"),
    )
    op.create_index("ix_coding_source_receipts_workspace_id", "coding_source_receipts", ["workspace_id"])
    op.create_index("ix_coding_source_receipts_venture_id", "coding_source_receipts", ["venture_id"])
    op.create_index("ix_coding_source_scope", "coding_source_receipts",
                    ["workspace_id", "venture_id", "app_id", "created_at"])


def downgrade() -> None:
    if "coding_source_receipts" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("coding_source_receipts")
