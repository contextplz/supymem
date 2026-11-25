"""Installation, charged referral progress, and committed reward receipts."""
import sqlalchemy as sa
from alembic import op

revision = "s052referralrewards"
down_revision = "s051chathistory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("person_referral_codes", sa.Column("last_nudged_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("beta_code_redemptions") as batch:
        batch.add_column(sa.Column("installed_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("qualified_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("reward_ledger_id", sa.Text()))
        batch.create_foreign_key("fk_referral_reward_ledger", "credit_ledger", ["reward_ledger_id"], ["id"])
        batch.create_unique_constraint("uq_referral_reward_ledger", ["reward_ledger_id"])


def downgrade():
    with op.batch_alter_table("beta_code_redemptions") as batch:
        batch.drop_constraint("uq_referral_reward_ledger", type_="unique")
        batch.drop_constraint("fk_referral_reward_ledger", type_="foreignkey")
        batch.drop_column("reward_ledger_id")
        batch.drop_column("qualified_at")
        batch.drop_column("installed_at")
    op.drop_column("person_referral_codes", "last_nudged_at")
