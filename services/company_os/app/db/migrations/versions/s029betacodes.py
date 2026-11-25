"""Beta download codes, per-code download log, and the founder's referral code."""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "s029betacodes"
down_revision = "s028zerocoding"
branch_labels = None
depends_on = None

FOUNDER_PERSON_ID = "usr_cog_72943502a13ed9e35eff9e9a870e786a"
FOUNDER_CODE = "5BN4EKZD"  # Referral code, not a credential.


def upgrade():
    codes = op.create_table(
        "beta_codes",
        sa.Column("code", sa.String(8), primary_key=True),
        sa.Column("owner_person_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer()),
    )
    op.create_index("ix_beta_codes_owner_person_id", "beta_codes", ["owner_person_id"])
    op.create_table(
        "beta_code_downloads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(8), sa.ForeignKey("beta_codes.code"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_hash", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.String(120), nullable=False, server_default=""),
        sa.Column("platform", sa.String(40), nullable=False, server_default=""),
        sa.Column("version", sa.String(40), nullable=False, server_default=""),
    )
    op.create_index("ix_beta_code_downloads_code", "beta_code_downloads", ["code", "created_at"])
    op.bulk_insert(codes, [{
        "code": FOUNDER_CODE, "owner_person_id": FOUNDER_PERSON_ID, "label": "Founder referral",
        "active": True, "created_at": datetime.now(timezone.utc), "max_uses": None,
    }])


def downgrade():
    op.drop_table("beta_code_downloads")
    op.drop_table("beta_codes")
