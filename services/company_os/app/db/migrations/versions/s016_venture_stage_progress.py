"""Backfill venture stages from durable approved work.

Revision ID: s016venturestage
Revises: s015mergereleaseheads
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s016venturestage"
down_revision: str | None = "s015mergereleaseheads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEFORE_BUILDING = {
    "draft", "classifying", "clarifying", "charter_review", "org_planning",
    "fork_choice", "demand_testing",
}
_BEFORE_READINESS = _BEFORE_BUILDING | {"building"}


def upgrade() -> None:
    connection = op.get_bind()

    approved = connection.execute(
        sa.text("SELECT DISTINCT venture_id FROM execution_plans WHERE state IN ('approved', 'executing', 'completed')")
    ).scalars()
    for venture_id in approved:
        connection.execute(
            sa.text("UPDATE ventures SET stage = 'building' WHERE id = :venture_id AND stage IN :stages")
            .bindparams(sa.bindparam("stages", expanding=True)),
            {"venture_id": venture_id, "stages": sorted(_BEFORE_BUILDING)},
        )

    accepted: set[str] = set()
    for venture_id, raw_spec in connection.execute(
        sa.text("SELECT venture_id, spec FROM build_runs WHERE runtime_version = 1")
    ):
        try:
            review = json.loads(raw_spec or "{}").get("lifecycle", {}).get("review", {})
        except (TypeError, json.JSONDecodeError):
            continue
        if review.get("decision") == "accept" and review.get("status") == "accepted":
            accepted.add(venture_id)
    for venture_id in accepted:
        connection.execute(
            sa.text("UPDATE ventures SET stage = 'readiness_review' WHERE id = :venture_id AND stage IN :stages")
            .bindparams(sa.bindparam("stages", expanding=True)),
            {"venture_id": venture_id, "stages": sorted(_BEFORE_READINESS)},
        )


def downgrade() -> None:
    # Stages represent completed founder-approved work. A schema rollback must
    # not erase that business progress.
    pass
