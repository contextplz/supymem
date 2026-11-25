"""Keep custody-bound zero-cost native coding holds without paid usage."""
from alembic import op
from sqlalchemy import text

revision = "s028zerocoding"
down_revision = "s027usererrors"
branch_labels = None
depends_on = None


def _replace_amount_check(condition):
    bind = op.get_bind()
    # SQLite batch recreation drops triggers. Keep the existing pool identity
    # guard (and any other table triggers) exactly as installed.
    triggers = list(bind.scalars(text("SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='credit_reservations' AND sql IS NOT NULL"))) if bind.dialect.name == "sqlite" else []
    with op.batch_alter_table("credit_reservations") as batch:
        batch.drop_constraint("ck_credit_reservations_amount", type_="check")
        batch.create_check_constraint("ck_credit_reservations_amount", condition)
    for trigger in triggers:
        op.execute(trigger)


def upgrade():
    _replace_amount_check("amount_micro > 0 OR (amount_micro = 0 AND purpose = 'device.work_order' "
                         "AND NOT is_pool AND model IS NULL AND provider IS NULL)")


def downgrade():
    # Existing zero holds must be explicitly reconciled/retained; never delete
    # execution custody or silently turn a free build into a paid reservation.
    if op.get_bind().scalar(text("SELECT COUNT(*) FROM credit_reservations WHERE amount_micro = 0")):
        raise RuntimeError("zero_cost_device_custody_prevents_downgrade")
    _replace_amount_check("amount_micro > 0")
