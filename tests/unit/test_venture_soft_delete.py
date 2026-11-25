"""Typed-confirm soft-delete with 7-day restore."""

from __future__ import annotations

import sys
import types
from datetime import timedelta

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/del.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    from fastapi.testclient import TestClient

    with TestClient(main.create_app()) as c:
        yield c


def _make_venture(client):
    r = client.post(
        "/ventures",
        json={"raw_idea": "a marketplace for restoring vintage synthesizers", "start": False},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_cannot_delete_without_exact_phrase(client):
    vid = _make_venture(client)
    bad = client.post(
        f"/ventures/{vid}/delete",
        json={"confirmation": "delete please"},
        headers={"X-Subject": "founder"},
    )
    assert bad.status_code == 422
    still = client.get("/ventures")
    assert any(i["id"] == vid for i in still.json()["items"])


def test_card_patch_is_owned_validated_durable_and_preserves_other_settings(client):
    from app.workspaces import service

    venture_id = _make_venture(client)
    workspace_id = client.get(f"/ventures/{venture_id}").json()["workspace_id"]
    service.merge_settings(workspace_id, {"ui_pins": ["Research"]})
    choice = {"card_type": "hardware", "card_color": "green", "card_art": "8"}
    path = f"/ventures/{venture_id}/card"
    assert client.patch(path, json=choice, headers={"X-Subject": "another-owner"}).status_code == 404
    assert client.patch(path, json={**choice, "card_art": "../../private"}, headers={"X-Subject": "founder"}).status_code == 422
    saved = client.patch(path, json=choice, headers={"X-Subject": "founder"})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"venture_id": venture_id, **choice}
    assert service.get_workspace(workspace_id).settings["ui_pins"] == ["Research"]
    for path in (f"/ventures/{venture_id}", "/ventures?summary=true", "/ventures"):
        result = client.get(path).json()
        row = next(item for item in result["items"] if item["id"] == venture_id) if "items" in result else result
        assert {key: row[key] for key in choice} == choice
    assert client.patch(f"/ventures/{venture_id}/card", json={"card_type": "software-ai", "card_color": "blue"}, headers={"X-Subject": "founder"}).status_code == 200
    assert service.get_workspace(workspace_id).settings["card_art"] == "8"
    assert client.post(f"/ventures/{venture_id}/delete", json={"confirmation": "DELETE"}, headers={"X-Subject": "founder"}).status_code == 200
    assert client.patch(f"/ventures/{venture_id}/card", json=choice, headers={"X-Subject": "founder"}).status_code == 404


def test_delete_accepts_lowercase_word(client):
    vid = _make_venture(client)
    ok = client.post(
        f"/ventures/{vid}/delete",
        json={"confirmation": "delete"},
        headers={"X-Subject": "founder"},
    )
    assert ok.status_code == 200, ok.text
    listed = client.get("/ventures").json()["items"]
    assert not any(i["id"] == vid for i in listed)


def test_delete_requires_phrase_then_restore(client):
    from app.ventures.lifecycle import CONFIRM_PHRASE

    vid = _make_venture(client)
    phrase = client.get("/ventures/delete-phrase").json()
    assert phrase["phrase"] == CONFIRM_PHRASE
    assert phrase["phrase"] == "DELETE"
    assert "delete" in phrase["instruction"].lower()

    ok = client.post(
        f"/ventures/{vid}/delete",
        json={"confirmation": CONFIRM_PHRASE},
        headers={"X-Subject": "founder"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["retention_days"] == 7
    assert ok.json()["recoverable"] is True

    listed = client.get("/ventures").json()["items"]
    assert not any(i["id"] == vid for i in listed)

    gone = client.get(f"/ventures/{vid}")
    assert gone.status_code == 404

    trash = client.get("/ventures/deleted").json()["items"]
    assert any(i["id"] == vid for i in trash)

    restored = client.post(
        f"/ventures/{vid}/restore",
        headers={"X-Subject": "founder"},
    )
    assert restored.status_code == 200, restored.text
    back = client.get(f"/ventures/{vid}")
    assert back.status_code == 200
    assert back.json()["id"] == vid


def test_retention_expired_cannot_restore(client, monkeypatch):
    from app.ventures import lifecycle
    from app.db.models import Venture
    from app.state import db
    from core.clock import utcnow

    vid = _make_venture(client)
    client.post(
        f"/ventures/{vid}/delete",
        json={"confirmation": lifecycle.CONFIRM_PHRASE},
        headers={"X-Subject": "founder"},
    )
    with db()() as session:
        ven = session.get(Venture, vid)
        assert ven and ven.archived_at
        ven.archived_at = utcnow() - timedelta(days=8)
        session.commit()

    denied = client.post(
        f"/ventures/{vid}/restore",
        headers={"X-Subject": "founder"},
    )
    assert denied.status_code == 410
