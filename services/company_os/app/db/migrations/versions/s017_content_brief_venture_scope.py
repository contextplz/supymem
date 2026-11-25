"""Scope content briefs to the venture they describe.

Revision ID: s017contentventure
Revises: s016venturestage
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s017contentventure"
down_revision: str | None = "s016venturestage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_content_briefs_status", table_name="content_briefs")
    with op.batch_alter_table("content_briefs") as batch:
        batch.drop_constraint("uq_content_brief_version", type_="unique")
        batch.add_column(sa.Column("venture_id", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_content_briefs_venture_id", "ventures", ["venture_id"], ["id"]
        )
        batch.create_unique_constraint(
            "uq_content_brief_version", ["workspace_id", "venture_id", "version"]
        )
    op.create_index("ix_content_briefs_venture_id", "content_briefs", ["venture_id"])
    op.create_index(
        "ix_content_briefs_status",
        "content_briefs",
        ["workspace_id", "venture_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_content_briefs_status", table_name="content_briefs")
    op.drop_index("ix_content_briefs_venture_id", table_name="content_briefs")
    with op.batch_alter_table("content_briefs") as batch:
        batch.drop_constraint("uq_content_brief_version", type_="unique")
        batch.drop_constraint("fk_content_briefs_venture_id", type_="foreignkey")
        batch.drop_column("venture_id")
        batch.create_unique_constraint(
            "uq_content_brief_version", ["workspace_id", "version"]
        )
    op.create_index(
        "ix_content_briefs_status", "content_briefs", ["workspace_id", "status"]
    )
