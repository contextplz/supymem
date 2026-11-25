"""Successful per-person invitation admission, independent of installer clicks."""
import sqlalchemy as sa
from datetime import datetime, timezone
from alembic import op

revision = "s034referrals"
down_revision = "s033needsyou"
branch_labels = None
depends_on = None


def upgrade():
    state = op.create_table("referral_program_state", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("grandfather_before", sa.DateTime(timezone=True), nullable=False))
    op.bulk_insert(state, [{"id": 1, "grandfather_before": datetime.now(timezone.utc)}])
    op.create_table("referral_admissions",
        sa.Column("person_id", sa.Text(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("legacy", sa.Boolean(), nullable=False, server_default=sa.false()))
    # People already known before rollout remain admitted; do not fabricate redemptions.
    op.execute("INSERT INTO referral_admissions (person_id, created_at, legacy) SELECT id, created_at, true FROM people")
    op.create_table("person_referral_codes",
        sa.Column("person_id", sa.Text(), primary_key=True),
        sa.Column("code", sa.String(8), sa.ForeignKey("beta_codes.code"), nullable=False, unique=True))
    op.create_table("beta_code_redemptions",
        sa.Column("person_id", sa.Text(), primary_key=True),
        sa.Column("code", sa.String(8), sa.ForeignKey("beta_codes.code"), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_beta_code_redemptions_code", "beta_code_redemptions", ["code"])
    op.create_table("beta_code_allowance_changes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("code", sa.String(8), sa.ForeignKey("beta_codes.code"), nullable=False),
        sa.Column("actor_admin_id", sa.Text(), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_limit", sa.Integer()), sa.Column("after_limit", sa.Integer()),
        sa.Column("before_active", sa.Boolean(), nullable=False), sa.Column("after_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_beta_code_allowance_changes_code", "beta_code_allowance_changes", ["code"])


def downgrade():
    op.drop_table("beta_code_allowance_changes")
    op.drop_table("beta_code_redemptions")
    op.drop_table("person_referral_codes")
    op.drop_table("referral_admissions")
    op.drop_table("referral_program_state")
