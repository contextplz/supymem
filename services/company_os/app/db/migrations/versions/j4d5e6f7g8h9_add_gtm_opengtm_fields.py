"""OpenGTM personalization fields on gtm_contacts.

Revision ID: j4d5e6f7g8h9
Revises: i3c4d5e6f7g8
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "j4d5e6f7g8h9"
down_revision: str | None = "i3c4d5e6f7g8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("gtm_contacts", sa.Column("icp_score", sa.Float()))
    op.add_column("gtm_contacts", sa.Column("icp_tier", sa.Text()))
    op.add_column("gtm_contacts", sa.Column("outreach_pattern", sa.Text()))


def downgrade() -> None:
    op.drop_column("gtm_contacts", "outreach_pattern")
    op.drop_column("gtm_contacts", "icp_tier")
    op.drop_column("gtm_contacts", "icp_score")
