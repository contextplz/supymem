"""Public POST /e under signed-caller enforcement (production Core posture)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from core.assertion import sign_assertion
from tests.company_os_harness import load_company_os_app

SECRET = "test-events-ingest-secret-at-least-32-bytes"


def headers(path: str, method: str = "GET", subject: str = "usr_alice", **claims) -> dict[str, str]:
    return {
        "X-COS-Assertion": sign_assertion(
            SECRET, sub=subject, email="", role="user", path=path, method=method, **claims
        )
    }


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/events_ingest.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "test-vault-secret-with-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.test")
    main = load_company_os_app()
    return TestClient(main.create_app())


def _event_count() -> int:
    from app.db.supymem_models import AppEvent
    from app.state import db

    with db()() as session:
        return int(session.scalar(select(func.count()).select_from(AppEvent)) or 0)


def _mint_ingest_key(client: TestClient) -> str:
    created = client.post(
        "/workspaces",
        json={"name": "Events", "kind": "company"},
        headers=headers("/workspaces", "POST"),
    )
    assert created.status_code == 201, created.text
    venture = client.post(
        "/ventures",
        json={"raw_idea": "Neighborhood bakery waitlist for custom cake orders", "start": False},
        headers=headers("/ventures", "POST"),
    )
    assert venture.status_code == 201, venture.text
    vid = venture.json()["id"]
    path = f"/ventures/{vid}/apps"
    app = client.post(
        path,
        json={"name": "Bakery", "slug": f"bakery{vid[-6:]}"},
        headers=headers(path, "POST"),
    )
    assert app.status_code == 201, app.text
    key_path = f"/apps/{app.json()['id']}/ingest-key"
    key = client.post(key_path, headers=headers(key_path, "POST"))
    assert key.status_code == 201, key.text
    secret = key.json()["secret"]
    assert secret.startswith("pk_app_")
    return secret


def test_anonymous_post_e_unknown_key_is_204_with_no_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    before = _event_count()
    response = client.post(
        "/e",
        content=json.dumps({"key": "pk_app_unknownzzzz", "event": "landing_view"}),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert response.status_code == 204
    assert _event_count() == before


def test_anonymous_post_e_valid_pk_app_stores_one_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    secret = _mint_ingest_key(client)
    from contextlib import contextmanager
    from app.apps import keys
    from app.tenancy import bind

    actors = []

    @contextmanager
    def capture_scope(**scope):
        actors.append(scope["actor"])
        with bind(**scope) as exact:
            yield exact

    monkeypatch.setattr(keys, "bind", capture_scope)
    before = _event_count()
    response = client.post(
        "/e",
        content=json.dumps({"key": secret, "event": "landing_view", "props": {"path": "/"}}),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert response.status_code in {200, 204}
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert _event_count() == before + 1
    assert actors and all(actor != "system" for actor in actors)


def test_anonymous_post_e_non_pk_app_key_stores_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    before = _event_count()
    # A real-looking but non-ingest prefix must not store.
    response = client.post(
        "/e",
        content=json.dumps({"key": "sk_app_not_allowed", "event": "landing_view"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 204
    assert _event_count() == before


def test_anonymous_post_e_oversize_body_stores_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    secret = _mint_ingest_key(client)
    before = _event_count()
    huge = json.dumps({"key": secret, "event": "landing_view", "props": {"blob": "x" * 9000}})
    response = client.post(
        "/e",
        content=huge,
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert response.status_code == 204
    assert _event_count() == before


def test_anonymous_post_e_text_plain_json_body_works(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    secret = _mint_ingest_key(client)
    before = _event_count()
    response = client.post(
        "/e",
        content=json.dumps({"key": secret, "event": "cta_click", "props": {}}),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert response.status_code == 200
    assert _event_count() == before + 1


def test_nearby_anonymous_paths_still_require_assertion(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/e").status_code == 401
    assert client.post("/e/x", json={"key": "pk_app_x", "event": "landing_view"}).status_code == 401
    assert client.post("/telemetry", json={}).status_code == 401
