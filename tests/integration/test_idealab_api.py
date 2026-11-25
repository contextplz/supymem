"""Integration tests for Idea Lab API (37 §9) — hermetic, LLM offline."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


def _seed_corpus(folder) -> None:
    now = datetime.now(timezone.utc).isoformat()
    items = []
    for i in range(8):
        audience = f"independent bookstores in district {i}"
        need = f"used-inventory listing sync for shops {i}"
        items.append(
            {
                "claim": f"Shop owners still track stock on paper sheets {i}",
                "source": f"https://example.com/forum/{i}",
                "kind": "complaint_thread",
                "category": "retail",
                "retrieved_at": now,
                "tags": ["retail", "inventory", f"shape{i}"],
                "audience": audience,
                "geography": "Portland",
                "need": need,
            }
        )
        items.append(
            {
                "claim": f"POS vendors opened export APIs this quarter {i}",
                "source": f"https://example.com/changelog/{i}",
                "kind": "platform_change",
                "category": "retail",
                "retrieved_at": now,
                "tags": ["retail", "inventory", f"shape{i}"],
                "audience": audience,
                "geography": "Portland",
                "need": need,
            }
        )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "corpus.json").write_text(
        json.dumps({"refreshed_at": now, "items": items}),
        encoding="utf-8",
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/idealab_api.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    data = tmp_path / "idealab"
    monkeypatch.setenv("IDEALAB_DATA_DIR", str(data))
    _seed_corpus(data)
    main = load_company_os_app()
    from app import state as state_mod
    from app.idealab import generate as generate_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    generate_mod.reset_cache()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def desk_id(client: TestClient) -> str:
    return client.get("/me").json()["desk_id"]


def test_generate_slate(client: TestClient, desk_id: str) -> None:
    r = client.post(f"/workspaces/{desk_id}/idealab/slate", json={"seed": {}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("ops_")
    assert 1 <= len(body["opportunities"]) <= 3
    for opp in body["opportunities"]:
        assert opp["title"]
        assert opp["first_customer"]
        assert opp["wedge"]
        assert len(opp["evidence"]) >= 1


def test_get_latest_slate(client: TestClient, desk_id: str) -> None:
    created = client.post(f"/workspaces/{desk_id}/idealab/slate", json={}).json()
    fetched = client.get(f"/workspaces/{desk_id}/idealab/slate")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]


def test_reject_and_reroll(client: TestClient, desk_id: str) -> None:
    slate = client.post(f"/workspaces/{desk_id}/idealab/slate", json={}).json()
    opp = slate["opportunities"][0]
    rejected = client.post(
        f"/workspaces/{desk_id}/idealab/reject",
        json={"opportunity_id": opp["id"], "reason_code": "too_hard"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    reroll = client.post(
        f"/workspaces/{desk_id}/idealab/reroll",
        json={"previous_slate_id": slate["id"]},
    )
    assert reroll.status_code == 201, reroll.text
    assert reroll.json()["round"] == slate["round"] + 1


def test_promote_prefills_slots(client: TestClient, desk_id: str) -> None:
    slate = client.post(f"/workspaces/{desk_id}/idealab/slate", json={}).json()
    assert slate["opportunities"], "need at least one opportunity to promote"
    opp = slate["opportunities"][0]
    promoted = client.post(
        f"/workspaces/{desk_id}/idealab/promote",
        json={"opportunity_id": opp["id"]},
    )
    assert promoted.status_code == 201, promoted.text
    body = promoted.json()
    assert body["venture_id"].startswith("ven_")
    assert len(body["prefilled_slots"]) >= 3
    keys = {s["key"] for s in body["prefilled_slots"]}
    assert "audience" in keys or "mvp_scope" in keys
    assert "mvp_scope" in keys

    from app.state import MEM

    answers = MEM.get(f"answers:{body['venture_id']}") or {}
    assert len(answers) >= 3
