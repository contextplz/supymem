"""Saved founder turns and their replies retain request identity through background delivery."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient, load_company_os_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rid")
    mp = pytest.MonkeyPatch()
    mp.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp}/rid.db")
    mp.setenv("COMPANY_OS_DATA_DIR", str(tmp))
    mp.setenv("COMPANY_OS_MEMORY_PATH", str(tmp / "mem.sqlite3"))
    mp.setenv("LLM_OFFLINE", "1")
    mp.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c
    mp.undo()


@pytest.mark.parametrize("idea", ["Warehouse shift scheduling", "Family swimming lessons"])
def test_local_client_uses_owner_without_allowing_remote_requests(client, monkeypatch, idea):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("SUPYMEM_LOCAL_ALLOW_REMOTE", raising=False)
    from app.core.local_owner import ensure_owner, _ensure_proxy_secret
    from core.local_security import _proxy_secret

    _ensure_proxy_secret()
    client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
    from app.db.models import Workspace
    from app.state import db

    created = client.post("/ventures", json={"raw_idea": idea}, headers={"X-Subject": "another-owner"})
    assert created.status_code == 201, created.text
    with db()() as session:
        assert session.get(Workspace, created.json()["workspace_id"]).owner_id == ensure_owner().id
    with TestClient(client.app, base_url="http://127.0.0.1", client=("192.0.2.1", 50000)) as remote:
        rejected = remote.post("/ventures", json={"raw_idea": idea})
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "local_only"


@pytest.mark.parametrize("idea", ["Invoice matching for finance teams", "Hiking plans for families"])
def test_founder_and_followup_replies_share_their_request_without_leaking_metadata(client, monkeypatch, idea):
    from app.chat.store import CLIENT_REQUEST_ID, REPLY_REQUEST_ID, append_message, list_messages, request_citations
    from app.chat.milestones import post_milestone
    from app.chat import next_moves
    from tests.unit.chat.test_next_moves import _saved_state

    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))

    created = client.post("/ventures", json={"raw_idea": idea}, headers={"X-Subject": "rid-user"})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    nonce = "Nonce-12345678"
    token = CLIENT_REQUEST_ID.set(nonce)
    try:
        citation = {"label": "Your saved plan", "href": "https://example.com/plan"}
        mine = append_message(vid, role="user", body_md="yes please", kind="text", citations=[citation])
        reply = append_message(vid, role="assistant", body_md="done", kind="text", citations=[citation])
    finally:
        CLIENT_REQUEST_ID.reset(token)
    similar = CLIENT_REQUEST_ID.set(nonce.lower())
    try:
        append_message(vid, role="user", body_md="yes please", kind="text")
    finally:
        CLIENT_REQUEST_ID.reset(similar)
    other = append_message(vid, role="user", body_md="yes please", kind="text")  # no nonce, same text
    background = REPLY_REQUEST_ID.set(nonce)
    try:
        followup = append_message(vid, role="assistant", body_md="Your work is ready.", kind="task_completed")
        assert request_citations([citation]) == [citation, {"client_request_id": nonce}]
        with monkeypatch.context() as no_suggestions:
            no_suggestions.setattr(next_moves, "attach", lambda *args, **kwargs: False)
            assert post_milestone(vid, key="request-finished", body_md="Your background work is saved.",
                                  kind="task_completed", requested=True, citations=[citation])
    finally:
        REPLY_REQUEST_ID.reset(background)
    unrelated = append_message(vid, role="assistant", body_md="Your other answer is here.")
    assert mine["client_request_id"] == reply["client_request_id"] == followup["client_request_id"] == nonce
    assert reply["reply_to_message_id"] == followup["reply_to_message_id"] == mine["id"]
    assert mine["citations"] == reply["citations"] == [citation]
    assert "client_request_id" not in other and "client_request_id" not in unrelated
    rows = {m["id"]: m for m in list_messages(vid)}
    milestone = next(message for message in rows.values() if "Your background work is saved." in message["body_md"])
    assert milestone["client_request_id"] == nonce and milestone["reply_to_message_id"] == mine["id"]
    assert milestone["citations"] == [citation]
    assert rows[mine["id"]]["client_request_id"] == nonce
    assert mine["id"] != other["id"]  # a genuinely repeated answer is a second row, not collapsed
