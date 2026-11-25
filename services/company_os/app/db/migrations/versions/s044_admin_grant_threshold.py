"""Set the founder-approved 50,000-credit dual-control threshold."""

from alembic import op

revision = "s044grantthreshold"
down_revision = "s043deviceleases"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE credit_policies SET value_json = '50000', updated_at = CURRENT_TIMESTAMP, "
               "updated_by = 'system:admin-grant-threshold' WHERE key = 'dual_control_threshold'")


def downgrade():
    op.execute("UPDATE credit_policies SET value_json = '5000', updated_at = CURRENT_TIMESTAMP, "
               "updated_by = 'system:rollback-admin-grant-threshold' WHERE key = 'dual_control_threshold' "
               "AND value_json = '50000' AND updated_by = 'system:admin-grant-threshold'")
