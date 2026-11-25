from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SCAFFOLD = ROOT / "config" / "scaffold"


def test_scaffold_boots_and_401():
    sys.path.insert(0, str(SCAFFOLD))
    # Clear conflicting app modules
    for k in list(sys.modules):
        if k == "app" or k.startswith("app."):
            del sys.modules[k]
    from app.main import app  # noqa: WPS433

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/api/me").status_code == 401
    assert (SCAFFOLD / "migrations" / "001_init.sql").exists()
    assert (SCAFFOLD / ".env.example").exists()
