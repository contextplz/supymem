"""Ledger expires_at + reservation status vocab (doc 07).

Revision ID: s002credits
Revises: s001supymem
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s002credits"
down_revision: Union[str, None] = "s001supymem"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("credit_ledger") as batch:
        batch.add_column(sa.Column("expires_at", sa.DateTime()))
    op.execute("UPDATE credit_reservations SET status = 'active' WHERE status = 'open'")


def downgrade() -> None:
    op.execute("UPDATE credit_reservations SET status = 'open' WHERE status = 'active'")
    with op.batch_alter_table("credit_ledger") as batch:
        batch.drop_column("expires_at")
