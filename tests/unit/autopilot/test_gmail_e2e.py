"""Gmail beta chain end to end with only Composio's HTTP boundary faked:
connect (managed OAuth + callback) -> draft -> founder approval -> send exactly once -> receipt -> retry is a no-op -> caps.
No real network, no real mail."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from tests.unit.autopilot.test_god_mode import env as _shared_env
from tests.unit.autopilot.test_outreach import _contacts, _run, _sending_address, _ws
from tests.unit.gtm.test_journey import journey_db as journey_db

env = _shared_env


class FakeComposio:
    """Stands in for composio_gmail._request. `send_mode` picks how GMAIL_SEND_EMAIL behaves."""

    def __init__(self, ws: str):
        self.uid, self.calls, self.sent, self.send_mode, self.status = f"gmail:{ws}", [], [], "ok", "INITIATED"

    def __call__(self, method, path, *, json=None, params=None, timeout=45.0):
        from app.gtm.supymem import composio_gmail as cg

        self.calls.append((method, path))
        if path == "/connected_accounts/link":
            assert json["user_id"] == self.uid and "supymem_state=" in json["callback_url"]
            return {"redirect_url": "https://connect.composio.dev/link/abc", "connected_account_id": "ca_live"}
        if path == "/connected_accounts/ca_live":
            return {"id": "ca_live", "status": self.status, "toolkit": {"slug": "gmail"}, "auth_config": {"id": "ac_1"},
                    "experimental": {"account_type": "PRIVATE"}}
        if path == "/connected_accounts":
            return {"items": [{"id": "ca_live", "status": self.status, "user_id": self.uid}]}
        if path == "/tools/execute/GMAIL_GET_PROFILE":
            return {"successful": True, "data": {"emailAddress": "founder@gmail.example"}}
        if path == "/tools/execute/GMAIL_SEND_EMAIL":
            if self.send_mode == "rejected":
                raise cg.DefiniteFailure("401 invalid_grant")
            if self.send_mode == "unknown":
                raise RuntimeError("Composio returned HTTP 502")
            assert json["connected_account_id"] == "ca_live" and json["user_id"] == self.uid
            self.sent.append(json["arguments"])
            n = len(self.sent)
            return {"successful": True, "data": {"id": f"gm{n}", "threadId": f"th{n}"}}
        raise AssertionError(f"unexpected Composio call {method} {path}")


@pytest.fixture()
def chain(env, monkeypatch):
    client, vid = env
    from app.autopilot import llm, outreach
    from app.gtm.supymem import composio_gmail

    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")
    monkeypatch.setenv("CONTENT_OAUTH_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("CONSOLE_URL", "https://console.example.test")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")

    async def no_model(*a, **k):
        return None

    fake = FakeComposio(_ws(vid))
    _sending_address(vid)
    monkeypatch.setattr(llm, "ask_json", no_model)
    monkeypatch.setattr(outreach, "site_url", lambda _v: "https://gym.example/")
    monkeypatch.setattr(composio_gmail, "ensure_auth_config", lambda: "ac_1")
    monkeypatch.setattr(composio_gmail, "_request", fake)
    return client, vid, fake


@pytest.mark.hosted_only
def test_connect_draft_approve_send_once_receipt_retry_and_cap(chain, monkeypatch):
    client, vid, fake = chain
    from app.autopilot import outreach, pmf
    from app.chat import connect
    from app.connections import hub
    from app.db.models import GtmTouch
    from app.gtm.supymem import composio_gmail, pipeline
    from app.state import db
    from sqlalchemy import select

    ws = _ws(vid)
    captured = {}
    real = composio_gmail.create_gmail_connection

    def spy(*a, **k):
        captured["callback"] = k["callback"]
        return real(*a, **k)

    monkeypatch.setattr(composio_gmail, "create_gmail_connection", spy)

    # 1. Not connected: sending explains how to connect and sends nothing.
    assert pipeline.mail_readiness(ws)["connected"] is False
    _contacts(vid, 17)
    batch = _run(outreach.draft_batch(vid, n=30, review_only=True))
    assert "Drafted 17" in batch["body_md"]
    bid = outreach.drafts(vid)[0]["batch_id"]
    blocked = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert blocked["kind"] == "needs_you" and "Connect your Gmail" in blocked["body_md"] and not fake.sent

    # 2. Connect through the real start + callback code; the chat card's own check flips to connected.
    connect._save_watch(vid, "gmail", ws)
    started = hub.connect_provider(ws, "gmail")
    assert started["ok"]
    state = parse_qs(urlparse(captured["callback"]).query)["supymem_state"][0]
    assert connect.check(vid, "gmail") == "waiting"  # the founder has not clicked Allow yet
    fake.status = "ACTIVE"  # ... and now they have
    back = client.get("/api/v1/ai-gtm/gmail/callback", params={"supymem_state": state}, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"].endswith("provider=gmail&oauth=connected")
    ready = pipeline.mail_readiness(ws)
    assert ready["connected"] and ready["account_email"] == "founder@gmail.example"
    assert connect.check(vid, "gmail") == "connected"  # "Gmail connected ✓" posted, watch removed
    # Another venture in the same workspace (FixtureWorks/ClubAttendance style) sees the same mailbox.
    from app.db.models import Venture
    from core.clock import utcnow
    with db()() as s:
        s.add(Venture(id="ven_second", workspace_id=ws, name="Second", raw_idea="a booking tool for climbing gyms", stage="idea",
                      created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    assert _ws("ven_second") == ws and outreach._mailbox(_ws("ven_second"))["account"] == "ca_live"

    # 3. Drafts are personal, honest, carry the page link with UTM and exactly one ask plus the opt-out line.
    d = outreach.drafts(vid, "draft")[0]
    assert "https://gym.example/?" in d["body"] and "utm_source=outreach" in d["body"]
    assert "reply \"no thanks\"" in d["body"] and d["body"].count("?") <= 3

    # 4. Approve: default cap is 15, so 15 go out, 2 stay saved. Each is exactly one Composio send.
    out = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert "Sent 15 outreach" in out["body_md"] and len(fake.sent) == 15
    assert {s["recipient_email"] for s in fake.sent} == {c.email for c in outreach.contacts(vid)[:15]}
    assert all(s["is_html"] is False for s in fake.sent)
    assert len(outreach.drafts(vid, "draft")) == 2 and outreach.send_slots_used_today(vid) == 15

    # 5. Receipts: one out touch per send with the provider ids; replies can be matched to these threads.
    with db()() as s:
        touches = s.scalars(select(GtmTouch).where(GtmTouch.venture_id == vid, GtmTouch.direction == "out")).all()
    assert len(touches) == 15 and all(t.message_id and t.provider_thread_id and t.send_mode == "founder_gmail" and not t.simulated for t in touches)
    assert pmf.read_metric(vid, "outbound_touches") == 15

    # 6. Retry / reload of the same approval, and a later one: nothing more goes out (cap used) and nothing is re-sent.
    for _ in range(2):
        again = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
        assert again["kind"] == "wait" and "limit (15)" in again["body_md"]
    assert len(fake.sent) == 15


def _one_contact_ready(chain, monkeypatch):
    client, vid, fake = chain
    from app.autopilot import outreach
    from app.gtm.supymem import pipeline

    _contacts(vid, 1)
    _run(outreach.draft_batch(vid, review_only=True))
    fake.status = "ACTIVE"
    pipeline.sync_gmail_accounts(_ws(vid), connections=[
        {"id": "ca_live", "status": "ACTIVE", "user_id": f"gmail:{_ws(vid)}"}])
    return client, vid, fake, outreach.drafts(vid)[0]["batch_id"]


@pytest.mark.hosted_only
def test_unknown_send_outcome_is_held_and_never_resent(chain, monkeypatch):
    client, vid, fake, bid = _one_contact_ready(chain, monkeypatch)
    from app.autopilot import outreach

    fake.send_mode = "unknown"
    first = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert first["kind"] == "needs_you" and "Sent folder" in first["body_md"] and "send it yourself" in first["body_md"]
    assert any(a.get("action") == "recovery.alternative" for a in first["actions"])
    assert outreach.drafts(vid)[0]["state"] == "delivery_unconfirmed"
    fake.send_mode = "ok"  # even once Gmail is healthy again, the held draft is not replayed
    again = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert again["kind"] == "needs_you" and not fake.sent and outreach.send_slots_used_today(vid) == 1


@pytest.mark.hosted_only
def test_provider_refusal_keeps_the_draft_and_a_reconnected_send_goes_out_once(chain, monkeypatch):
    client, vid, fake, bid = _one_contact_ready(chain, monkeypatch)
    from app.autopilot import outreach
    from app.gtm.supymem import pipeline

    fake.send_mode = "rejected"
    first = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert first["kind"] == "needs_you" and "Nothing was sent" in first["body_md"] and not fake.sent
    assert first["actions"][0]["action"] == "connections.connect" and first["code"] == "gmail_reconnect_required"
    assert outreach.drafts(vid)[0]["state"] == "draft" and outreach.send_slots_used_today(vid) == 0
    fake.send_mode = "ok"
    assert pipeline.sync_gmail_accounts(_ws(vid))["synced"] == 1
    ok = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert "Sent 1 outreach" in ok["body_md"] and len(fake.sent) == 1
    assert _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))["kind"] == "wait" and len(fake.sent) == 1


@pytest.mark.hosted_only
def test_expired_mailbox_stops_being_connected_and_sending_asks_to_reconnect(chain, monkeypatch):
    client, vid, fake, bid = _one_contact_ready(chain, monkeypatch)
    from app.autopilot import outreach
    from app.gtm.supymem import pipeline

    ws = _ws(vid)
    assert pipeline.mail_readiness(ws)["connected"]
    pipeline.sync_gmail_accounts(ws, connections=[{"id": "ca_live", "status": "EXPIRED", "user_id": f"gmail:{ws}"}])
    assert not pipeline.mail_readiness(ws)["connected"]
    reply = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": bid}))
    assert "Reconnect your Gmail" in reply["body_md"] and not fake.sent


@pytest.mark.hosted_only
def test_connect_card_notices_a_finished_authorization_even_if_the_callback_never_arrived(chain):
    client, vid, fake = chain
    from app.chat import connect
    from app.connections import hub
    from app.gtm.supymem import pipeline

    ws = _ws(vid)
    connect._save_watch(vid, "gmail", ws)
    hub.connect_provider(ws, "gmail")
    assert connect.check(vid, "gmail") == "waiting" and not pipeline.mail_readiness(ws)["connected"]
    fake.status = "ACTIVE"
    assert connect.check(vid, "gmail") == "connected"
    assert pipeline.mail_readiness(ws)["account_email"] == "founder@gmail.example"


@pytest.fixture(params=[("Warehouse inventory teams", "warehouse"), ("Neighborhood runners", "running")], ids=["b2b", "consumer"])
def local_chain(monkeypatch, request):
    data = request.getfixturevalue("journey_db")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    from app.autopilot import llm, outreach
    from app.core.local_secrets import update_settings
    from app.db.models import Venture
    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail

    audience, domain = request.param
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        session.commit()
    update_settings("smtp", {"host": "127.0.0.1", "port": 587, "user": "owner@sender.example",
        "sender_name": "Local owner", "postal_address": "12 Main Street, Toronto, Canada"},
        {"password": "synthetic-test-password"})
    update_settings("capabilities", {"send_email": True}, {})
    _sending_address(data["vid"])
    async def no_model(*args, **kwargs):
        return None
    monkeypatch.setattr(llm, "ask_json", no_model)
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    monkeypatch.setattr(outreach, "_brief", lambda vid: {
        "idea": {"audience": audience, "problem": "Keep plans up to date", "confirmed": True}, "research": {}})
    sent, state = [], {"outcome": "accepted"}
    @contextmanager
    def smtp(config):
        def deliver(message, **kwargs):
            sent.append(message)
            if state["outcome"] == "unknown":
                raise TimeoutError("Synthetic lost acknowledgement")
            if state["outcome"] == "refused":
                return {str(message["To"]): (550, b"Synthetic rejection")}
            return {}
        yield SimpleNamespace(send_message=deliver)
    monkeypatch.setattr(local_mail, "_smtp", smtp)
    monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: pytest.fail("Local mail must not call Composio"))
    _contacts(data["vid"], 2, domain=domain)
    _run(outreach.draft_batch(data["vid"], review_only=True))
    batch_id = outreach.drafts(data["vid"])[0]["batch_id"]
    return data, batch_id, sent, state


def test_local_requested_send_keeps_owner_switch_receipts_replay_and_cap(local_chain):
    from app.autopilot import guard, outreach, service
    from app.core.local_secrets import update_settings
    from app.db.models import GtmTouch
    from sqlalchemy import select

    data, batch_id, sent, _ = local_chain
    payload = {"approved_by_founder": True, "batch_id": batch_id}
    _run(outreach.send_batch(data["vid"], {"batch_id": batch_id}, actor_subject=data["actor"]))
    assert sent == []
    update_settings("capabilities", {"send_email": False}, {})
    assert not guard.check(data["vid"], "outreach.send", human_approved=True).ok
    blocked = _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert sent == [] and "Settings > Capabilities" in blocked["body_md"] and "Send email" in blocked["body_md"]
    update_settings("capabilities", {"send_email": True, "background_work": False}, {})
    service.update_settings(data["vid"], allow_send=True, send_max_per_day=1)
    assert guard.check(data["vid"], "outreach.send", human_approved=True).ok
    assert not guard.check(data["vid"], "outreach.send").ok
    result = _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert len(sent) == 1, result
    assert outreach.send_slots_used_today(data["vid"]) == 1
    body = sent[0].get_content()
    assert "Your email address is listed publicly on" in body
    assert "no thanks" in body and "12 Main Street" in body
    _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert len(sent) == 1
    with data["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "out")))
        assert len(touches) == 1 and touches[0].message_id and not touches[0].simulated


def test_local_unknown_delivery_is_held_and_never_resent(local_chain):
    from app.autopilot import outreach

    data, batch_id, sent, state = local_chain
    state["outcome"] = "unknown"
    payload = {"approved_by_founder": True, "batch_id": batch_id}
    first = _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert len(sent) == 1, first
    held = [draft for draft in outreach.drafts(data["vid"]) if draft["state"] == "delivery_unconfirmed"]
    assert len(held) == 1 and outreach.send_slots_used_today(data["vid"]) == 1
    state["outcome"] = "accepted"
    _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert sum(str(message["To"]) == held[0]["to"] for message in sent) == 1
    assert len({str(message["To"]) for message in sent}) == len(sent)
    assert next(draft for draft in outreach.drafts(data["vid"]) if draft["doc_id"] == held[0]["doc_id"])["state"] == "delivery_unconfirmed"
    assert outreach.send_slots_used_today(data["vid"]) == len(sent)


def test_local_refusal_keeps_draft_and_retry_sends_each_recipient_once(local_chain):
    from app.autopilot import outreach

    data, batch_id, sent, state = local_chain
    state["outcome"] = "refused"
    payload = {"approved_by_founder": True, "batch_id": batch_id}
    first = _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert len(sent) == 1, first
    assert all(draft["state"] == "draft" for draft in outreach.drafts(data["vid"]))
    assert outreach.send_slots_used_today(data["vid"]) == 0
    state["outcome"] = "accepted"
    _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert len(sent) == 3
    assert len({str(message["To"]) for message in sent[1:]}) == 2
    _run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
    assert len(sent) == 3


def test_local_reconnect_does_not_reuse_another_mailbox_binding(local_chain):
    from app.core.local_secrets import ConnectionSecretRequired, update_settings
    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail

    data, _, sent, _ = local_chain
    with data["factory"]() as session:
        account = local_mail.ensure_account(session, data["wid"])
        old_user, old_id = account.composio_user_id, account.connected_account_id
        session.commit()
    with pytest.raises(ConnectionSecretRequired):
        update_settings("smtp", {"user": "replacement@sender.example"}, {})
    update_settings("smtp", {"user": "replacement@sender.example"}, {"password": "replacement-test-password"})
    with data["factory"]() as session:
        account = local_mail.ensure_account(session, data["wid"])
        assert account.connected_account_id != old_id
        session.commit()
    with pytest.raises(composio_gmail.DefiniteFailure):
        local_mail.check_binding(old_user, old_id)
    assert sent == []
