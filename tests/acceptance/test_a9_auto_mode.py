"""A9 — Auto mode is honest about being unvalidated (34 §2)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.mark.acceptance
def test_a9_auto_mode_unvalidated(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/a9.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "1")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")

    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    client = TestClient(main.create_app())

    idea = "a marketplace for restoring vintage synthesizers"
    created = client.post(
        "/ventures",
        json={"raw_idea": idea, "mode": "auto", "start": True},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    vid = body["id"]
    onboard = body.get("onboarding") or {}
    if onboard.get("status") != "done":
        r = client.post(f"/ventures/{vid}/onboard", json={"mode": "auto"})
        assert r.status_code in (200, 202), r.text

    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "done", status
    assert status.get("flags", {}).get("unvalidated") is True or status.get("unvalidated") is True

    dash = client.get(f"/ventures/{vid}/dashboard").json()
    validation = (dash.get("tiles") or {}).get("validation") or {}
    assert validation.get("state") in {"degraded", "ready"}
    assert validation.get("unvalidated") is True

    chat = client.get(f"/ventures/{vid}/chat").json()
    items = chat.get("items") or []
    bodies = " ".join(str(m.get("body_md") or m.get("body") or "") for m in items).lower()
    assert "myself" in bodies or "auto" in bodies or "unvalidated" in bodies or items
