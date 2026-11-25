"""Intake integration: recon timeout still yields usable brief via API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path / 'cos.db'}")
    main_mod = load_company_os_app()
    # harness already resets state; keep clear for safety
    main_mod._MEM.clear()
    with TestClient(main_mod.create_app()) as c:
        yield c


def test_intake_produces_brief_and_questions(client: TestClient) -> None:
    r = client.post(
        "/ventures",
        json={
            "name": "BakeOrder",
            "raw_idea": "an app that helps home bakers take custom cake orders on WhatsApp",
        },
    )
    ven = r.json()["id"]
    out = client.post(f"/ventures/{ven}/intake")
    assert out.status_code == 200
    data = out.json()
    assert data["idea_brief_id"].startswith("idea_")
    assert "factors" in data["complexity"]
    assert 3 <= data["question_budget"]["n"] <= 9
    assert data["questions"]
    assert data["stage"] == "clarifying"
