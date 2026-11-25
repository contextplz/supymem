"""Charter lock immutability + intake timeout degrade."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path / 'cos.db'}")
    main_mod = load_company_os_app()
    main_mod._MEM.clear()
    with TestClient(main_mod.create_app()) as c:
        yield c


def test_charter_lock_immutable(client: TestClient) -> None:
    r = client.post("/ventures", json={"name": "Bake", "raw_idea": "cake orders on WhatsApp"})
    ven = r.json()["id"]
    client.post(f"/ventures/{ven}/intake")
    locked = client.post(
        f"/ventures/{ven}/charter/lock",
        json={"answers": {"gtm_order": "market_first"}, "other_verbatim": {"note": "keep my words"}},
    )
    assert locked.status_code == 200
    cid = locked.json()["charter_id"]
    assert locked.json()["content"]["provenance"]["note"]["value"] == "keep my words"
    bad = client.patch(f"/ventures/{ven}/charter/{cid}", json={"content_json": "{}"})
    assert bad.status_code == 409


@pytest.mark.asyncio
async def test_intake_research_timeout_degrades() -> None:
    load_company_os_app()
    from app.intake.research import research_recon

    notes = await research_recon("x", budget_s=0.0001)
    assert isinstance(notes, dict)
    assert notes["mode"] in {"blocked", "simulated"}
    assert notes["reason"] in {
        "no_research_api_key",
        "no_research_provider",
        "provider_unavailable",
        "provider_timeout",
    } or str(notes["reason"]).startswith("provider_unavailable")
