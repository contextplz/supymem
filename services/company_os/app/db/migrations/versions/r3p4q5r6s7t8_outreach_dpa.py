"""Phase 3 hosted gates: DPA timestamp (OT-36).

Revision ID: r3p4q5r6s7t8
Revises: r2n3o4p5q6r7
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r3p4q5r6s7t8"
down_revision: str | None = "r2n3o4p5q6r7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gtm_outreach_settings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("gtm_outreach_settings")}
    if "dpa_accepted_at" not in cols:
        op.add_column("gtm_outreach_settings", sa.Column("dpa_accepted_at", sa.DateTime()))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gtm_outreach_settings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("gtm_outreach_settings")}
    if "dpa_accepted_at" in cols:
        op.drop_column("gtm_outreach_settings", "dpa_accepted_at")
