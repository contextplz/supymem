"""Add workspace-scoped BYOK LLM settings.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "founder_llm_settings",
        sa.Column(
            "workspace_id",
            sa.Text(),
            sa.ForeignKey("workspaces.id"),
            primary_key=True,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text()),
        sa.Column("model", sa.Text(), nullable=False),
        # This is a vault handle, never an API key.
        sa.Column("api_key_ref", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("founder_llm_settings")
