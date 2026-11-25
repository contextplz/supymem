"""A20 — First look generates a real page (45, 47)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/a20.db")
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "fallback")
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    main = load_company_os_app()
    from app import state as state_mod
    from app.onboarding.registry import reset_steps_cache

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_steps_cache()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.mark.acceptance
def test_a20_generate_siteversion_html_interest(client: TestClient, tmp_path) -> None:
    from app.db.models import InterestSignal, SiteVersion
    from app.state import db
    from sqlalchemy import select

    ws = client.post(
        "/workspaces",
        json={"kind": "venture", "name": "FarmNote", "entry_mode": "idea"},
    )
    assert ws.status_code == 201, ws.text
    workspace_id = ws.json()["id"]

    idea = "A weekly digest of local farm produce for city cooks"
    refused = client.post(
        f"/workspaces/{workspace_id}/firstlook",
        json={"idea_text": idea, "venture_name": "FarmNote", "archetype": "content"},
    )
    assert refused.status_code == 410, refused.text  # no new template pages (Claude Code / Codex builds them)
    assert refused.json()["detail"]["error"]["code"] == "firstlook_retired"
    # An already-existing page (made before retirement) keeps serving and its hosted waitlist keeps working.
    import asyncio

    from app.firstlook.service import generate_first_look
    from tests.firstlook_legacy import seed_legacy_page

    seed_legacy_page(workspace_id)
    body = asyncio.run(generate_first_look(workspace_id, idea, venture_name="FarmNote", archetype="content")).to_dict()
    assert body["version"] == 1
    assert body["stage"] == "first_look"
    assert body["url"]
    path = Path(body["path"])
    assert path.exists()
    html = path.read_text(encoding="utf-8")
    assert "FarmNote" in html
    assert "Trusted by" not in html
    assert "I assumed:" in html
    assert "data-layout=" in html
    assert "prefers-reduced-motion" in html

    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(SiteVersion).where(SiteVersion.workspace_id == workspace_id, SiteVersion.version > 0)
        ).all()
        assert len(rows) == 1
        assert rows[0].sha256
        assert rows[0].template.startswith("composed:") or rows[0].template.startswith("coded:")

    site = client.get(f"/workspaces/{workspace_id}/site")
    assert site.status_code == 200
    assert site.json()["version"] == 1

    interest = client.post(
        f"/w/{workspace_id}/interest",
        data={"email": "cook@example.com", "note": "want greens", "site_version": 1},
    )
    assert interest.status_code == 201, interest.text
    payload = interest.json()
    assert payload["has_email"] is True
    assert "email" not in payload  # PII stays out of the response

    with factory() as session:
        signals = session.scalars(
            select(InterestSignal).where(InterestSignal.workspace_id == workspace_id)
        ).all()
        assert len(signals) == 1
        assert signals[0].email == "cook@example.com"
        assert signals[0].site_version == 1


def test_onboarding_never_creates_a_template_page() -> None:
    from app.onboarding.registry import all_steps, reset_steps_cache

    reset_steps_cache()
    assert not [s.id for s in all_steps("idea") if s.id.startswith("first_look.")]
