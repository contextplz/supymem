"""Merge independent launch, GTM, and import-cleanup release heads.

No schema changes occur here.  This revision gives a fresh database one
unambiguous migration target after the three independently developed branches
are all applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "s015mergereleaseheads"
down_revision: str | tuple[str, ...] | None = (
    "s011gtmdiagnosis",
    "s011importcleanup",
    "s014launchcredits",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
