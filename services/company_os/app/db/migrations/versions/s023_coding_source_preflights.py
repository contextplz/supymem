"""Record immutable read-only contract checks for captured coding source."""

import sqlalchemy as sa
from alembic import op

revision = "s023sourcepreflight"
down_revision = "s022codingsource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "coding_source_preflights" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "coding_source_preflights",
        sa.Column("order_id", sa.Text(), sa.ForeignKey("coding_source_receipts.order_id"), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("app_id", sa.Text(), sa.ForeignKey("apps.id"), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("source_manifest_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("report_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('blocked_runtime_contract','requires_runtime_qa')", name="ck_coding_preflight_status"),
    )
    op.create_index("ix_coding_preflight_scope", "coding_source_preflights",
                    ["workspace_id", "venture_id", "app_id", "created_at"])


def downgrade() -> None:
    if "coding_source_preflights" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("coding_source_preflights")
