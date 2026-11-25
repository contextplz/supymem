"""Chat entry uses the authenticated gateway subject across its whole lifecycle."""

import json

from sqlalchemy import select

from tests.security.test_core_signed_boundary import client, headers  # noqa: F401


def test_signed_subject_owns_preview_confirmation_and_history(client):  # noqa: F811
    preview_path = "/entry/chat/preview"
    payload = {"text": "I already have a product. It helps studios manage bookings and I want to get customers", "request_id": "signed-1"}
    assert client.post(preview_path, json=payload, headers={"X-Subject": "usr_alice"}).status_code == 401
    created = client.post(preview_path, json=payload,
                          headers=headers(preview_path, "POST", "usr_alice"))
    assert created.status_code == 200, created.text
    proposal_id = created.json()["proposal"]["id"]
    confirm_path = "/entry/chat/confirm"
    foreign = client.post(confirm_path, json={"proposal_id": proposal_id},
                          headers=headers(confirm_path, "POST", "usr_bob"))
    assert foreign.status_code == 404
    done = client.post(confirm_path, json={"proposal_id": proposal_id},
                       headers=headers(confirm_path, "POST", "usr_alice"))
    assert done.status_code == 200, done.text
    history_path = "/entry/chat/history"
    own = client.get(history_path, headers=headers(history_path, "GET", "usr_alice"))
    other = client.get(history_path, headers=headers(history_path, "GET", "usr_bob"))
    assert own.json()["items"][0]["proposal"]["receipt"]["venture_id"] == done.json()["venture_id"]
    assert other.json()["items"] == []


def test_entry_analytics_are_subject_bound_closed_and_idempotent(client):  # noqa: F811
    from app.db.models import AnalyticsEvent
    from app.state import db

    path = "/entry/chat/preview"
    private = "I already have a product for alice@example.test and want to get customers"
    body = {"text": private, "request_id": "analytics-1"}
    assert client.post(path, json=body, headers={"X-Subject": "usr_bob"}).status_code == 401
    assert client.post(path, json={"text": "  "}, headers=headers(path, "POST")).status_code == 422
    with db()() as session:
        # Rejected input mints no product event. The only row allowed is the closed
        # server diagnostic for the 422 (route template and code, never the request).
        rows = session.scalars(select(AnalyticsEvent)).all()
        assert {row.event_name for row in rows} <= {"server.request_error"}
        assert all(private not in row.props_json and "alice@example.test" not in row.props_json for row in rows)
        assert all(json.loads(row.props_json)["route"] == "/entry/chat/preview" for row in rows)

    first = client.post(path, json=body, headers=headers(path, "POST"))
    assert first.status_code == 200, first.text
    proposal = first.json()["proposal"]
    retry = client.post(path, json=body, headers=headers(path, "POST"))
    assert retry.status_code == 200 and retry.json()["proposal"]["id"] == proposal["id"]

    confirm_path = "/entry/chat/confirm"
    assert client.post(confirm_path, json={"proposal_id": proposal["id"]},
                       headers=headers(confirm_path, "POST", "usr_bob")).status_code == 404
    done = client.post(confirm_path, json={"proposal_id": proposal["id"]},
                       headers=headers(confirm_path, "POST"))
    assert done.status_code == 200, done.text
    again = client.post(confirm_path, json={"proposal_id": proposal["id"]},
                        headers=headers(confirm_path, "POST"))
    assert again.status_code == 200 and again.json() == done.json()

    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.event_name.in_(("chat.message", "entry.chat_confirmed")))).all()
    assert len(rows) == 2
    assert {row.event_name for row in rows} == {"chat.message", "entry.chat_confirmed"}
    assert {row.person_id for row in rows} == {"usr_alice"}
    preview = next(row for row in rows if row.event_name == "chat.message")
    confirmed = next(row for row in rows if row.event_name == "entry.chat_confirmed")
    assert preview.venture_id is None and preview.workspace_id is None
    assert json.loads(preview.props_json) == {"source": "entry_chat", "route": "gtm"}
    assert confirmed.venture_id == done.json()["venture_id"]
    assert confirmed.workspace_id == done.json()["workspace_id"]
    assert json.loads(confirmed.props_json) == {"source": "entry_chat", "route": "gtm", "feature": "gtm"}
    assert private not in repr([(row.id, row.props_json) for row in rows])
    assert "alice@example.test" not in repr([(row.id, row.props_json) for row in rows])
