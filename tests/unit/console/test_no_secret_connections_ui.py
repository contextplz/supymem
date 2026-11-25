"""T10.50 — Connections tile and trust receipt never render secrets."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = [
    ROOT / "apps/console/src/app/v/[ventureId]/connections/page.tsx",
    ROOT / "apps/console/src/components/layout/SideNav.tsx",
    ROOT / "apps/console/src/components/secrets/CredentialField.tsx",
    ROOT / "apps/console/src/app/settings/local/page.tsx",
]
LEAK = re.compile(
    r"ghp_|lin_api_|secret_ref|secret_value|webhook_secret|token\.value"
)


def test_connections_surfaces_have_no_secret_fields() -> None:
    for path in FILES:
        text = path.read_text()
        match = LEAK.search(text)
        assert match is None, f"secret-shaped copy in {path}: {match.group(0) if match else ''}"
