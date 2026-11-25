"""Apply Supymem's launch credit defaults to existing policy rows."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "s014launchcredits"
down_revision: str | None = "s013cleanupfromu6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only replace the legacy defaults. A value already changed by an operator
    # is deliberate and must not be silently overwritten by a release.
    op.execute("UPDATE credit_policies SET value_json = '5000', updated_by = 'system:launch-credit-policy' WHERE key = 'default_venture_credits' AND value_json = '500'")
    op.execute("UPDATE credit_policies SET value_json = '5000', updated_by = 'system:launch-credit-policy' WHERE key = 'dual_control_threshold' AND value_json = '50000'")
    op.execute("UPDATE credit_policies SET value_json = '5000', updated_by = 'system:launch-credit-policy' WHERE key = 'max_grant_credits_finance' AND value_json = '100000'")


def downgrade() -> None:
    # Preserve any later operator change; only reverse this migration's value.
    op.execute("UPDATE credit_policies SET value_json = '500', updated_by = 'system:rollback-launch-credit-policy' WHERE key = 'default_venture_credits' AND value_json = '5000' AND updated_by = 'system:launch-credit-policy'")
    op.execute("UPDATE credit_policies SET value_json = '50000', updated_by = 'system:rollback-launch-credit-policy' WHERE key = 'dual_control_threshold' AND value_json = '5000' AND updated_by = 'system:launch-credit-policy'")
    op.execute("UPDATE credit_policies SET value_json = '100000', updated_by = 'system:rollback-launch-credit-policy' WHERE key = 'max_grant_credits_finance' AND value_json = '5000' AND updated_by = 'system:launch-credit-policy'")
