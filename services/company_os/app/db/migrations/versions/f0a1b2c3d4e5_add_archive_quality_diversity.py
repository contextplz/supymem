"""Add quality-diversity fields to evolution archive entries.

Revision ID: f1a2b3c4d5e6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "archive_entries",
        sa.Column("descriptors_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "archive_entries",
        sa.Column("novelty", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("archive_entries", sa.Column("fitness_scalar", sa.Float()))
    op.add_column(
        "archive_entries",
        sa.Column("sample_n", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "archive_entries",
        sa.Column("outcome", sa.Text(), nullable=False, server_default="never_promoted"),
    )


def downgrade() -> None:
    op.drop_column("archive_entries", "outcome")
    op.drop_column("archive_entries", "sample_n")
    op.drop_column("archive_entries", "fitness_scalar")
    op.drop_column("archive_entries", "novelty")
    op.drop_column("archive_entries", "descriptors_json")
