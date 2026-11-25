"""Chat reconnect and scoped nonce regressions through signed venture access."""
from __future__ import annotations

import pytest

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def test_same_nonce_never_returns_another_ventures_message(client):
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.state import db
    alice, _, _ = venture(client)
    bob, _, _ = venture(client, "usr_bob")
    first = append_message(alice, role="user", body_md="Alice private request", idempotency_key="browser-send-1")
    # Existing releases stored the unscoped nonce. Preserve its legitimate retry
    # while preventing a different venture from discovering the legacy row.
    with db()() as session:
        session.get(ChatMessage, first["id"]).idempotency_key = "browser-send-1"
        session.commit()
    second = append_message(bob, role="user", body_md="Bob private request", idempotency_key="browser-send-1")
    assert first["id"] != second["id"]
    assert append_message(alice, role="user", body_md="Alice private request", idempotency_key="browser-send-1")["id"] == first["id"]
    with pytest.raises(ValueError, match="chat_idempotency_conflict"):
        append_message(alice, role="user", body_md="Changed instructions", idempotency_key="browser-send-1")
    own = request(client, "GET", f"/ventures/{bob}/chat", "usr_bob")
    assert own.status_code == 200 and "Alice private" not in own.text and "Bob private" in own.text
    assert request(client, "GET", f"/ventures/{alice}/chat", "usr_bob").status_code == 404


def test_reconnect_returns_latest_history_in_chronological_order(client):
    from app.chat.store import append_message
    vid, _, _ = venture(client)
    saved = [append_message(vid, role="user", body_md=f"message-{i}") for i in range(105)]
    response = request(client, "GET", f"/ventures/{vid}/chat")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 100
    assert items[0]["id"] == saved[5]["id"]
    assert items[-1]["id"] == saved[-1]["id"]
    assert response.json()["next_cursor"] == saved[5]["id"]
    earlier = request(client, "GET", f"/ventures/{vid}/chat", params={"before": saved[5]["id"]})
    assert earlier.status_code == 200
    assert [item["id"] for item in earlier.json()["items"]] == [item["id"] for item in saved[:5]]
    assert earlier.json()["next_cursor"] is None


def test_chat_history_cursor_cannot_read_another_venture(client):
    from app.chat.store import append_message
    alice, _, _ = venture(client)
    bob, _, _ = venture(client, "usr_bob")
    marker = append_message(alice, role="user", body_md="Alice's private older note")
    append_message(bob, role="user", body_md="Bob's note")
    response = request(client, "GET", f"/ventures/{bob}/chat", "usr_bob", params={"before": marker["id"]})
    assert response.status_code == 404
    assert "Alice's private" not in response.text


@pytest.mark.parametrize("credential", [
    "vck_" + "ExampleGatewayCredential" * 2,
    "apikey_" + "a" * 32 + "_" + "b" * 64,
])
def test_gateway_and_typesafe_credentials_are_withheld_before_chat_dispatch(client, monkeypatch, credential):
    from app.chat import router
    from app.secrets import service
    from app.chat.store import list_messages
    vid, _, _ = venture(client)
    def no_dispatch(*args, **kwargs):
        raise AssertionError("Credential reached conversational context")
    monkeypatch.setattr(router, "assemble", no_dispatch)
    monkeypatch.setattr(service, "request_secret", lambda *args, **kwargs: {
        "slot_id": "generic.credential", "status": "pending", "env_refs": [],
        "form": {"label": "the credential"},
    })
    response = request(client, "POST", f"/ventures/{vid}/chat", json={"message": "Use this key: " + credential})
    assert response.status_code == 200, response.text
    assert credential not in response.text
    assert response.json()["kind"] == "needs_you"
    assert credential not in str(list_messages(vid))
    assert any("withheld" in row["body_md"] for row in list_messages(vid))
