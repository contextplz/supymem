"""God mode early access: request capture, authz, admin approval, chat routing and the beta gate."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from core.assertion import sign_assertion

from tests.unit.test_beta_codes import SECRET, client as client  # noqa: F401  (signed-caller fixture)

OWNER, OTHER = "usr_gm_owner", "usr_gm_other"


def _sign(sub, method, path, email="founder@example.test", role="user"):
    return {"X-COS-Assertion": sign_assertion(SECRET, sub=sub, email=email, role=role, path=path, method=method)}


@pytest.fixture()
def world(client):  # noqa: F811
    from app import state
    from app.db.models import Person, Venture
    from app.workspaces.service import create_workspace

    now = datetime.now(timezone.utc)
    with state.db()() as s:
        for pid, mail in ((OWNER, "owner@example.test"), (OTHER, "other@example.test")):
            s.add(Person(id=pid, display_name="Founder", email=mail, created_at=now))
        s.commit()
    ws = create_workspace(kind="company", name="GM", owner_id=OWNER)
    with state.db()() as s:
        s.add(Venture(id="ven_gm1", workspace_id=ws.id, name="GM", raw_idea="x", stage="draft",
                      autonomy_level="L1", created_at=now, updated_at=now))
        s.commit()
    return client, "ven_gm1"


def _admin(method, path):
    return _sign("usr_admin", method, path, email="admin@example.test")


def _post(c, vid, note="", sub=OWNER):
    path = f"/ventures/{vid}/god-mode/requests"
    return c.post(path, json={"note": note}, headers=_sign(sub, "POST", path))


def test_request_is_idempotent_and_captures_email(world):
    c, vid = world
    first = _post(c, vid, "draft my outreach")
    assert first.status_code == 200 and first.json()["status"] == "requested" and first.json()["created"] is True
    again = _post(c, vid, "different note")
    assert again.json()["created"] is False and again.json()["note"] == "draft my outreach"
    from app import state
    from app.db.supymem_models import GodModeRequest
    with state.db()() as s:
        rows = s.query(GodModeRequest).all()
    assert len(rows) == 1 and rows[0].email == "owner@example.test" and rows[0].person_id == OWNER
    path = f"/ventures/{vid}/god-mode/requests/me"
    mine = c.get(path, headers=_sign(OWNER, "GET", path)).json()
    assert mine["status"] == "requested" and mine["requested_at"]


def test_verified_account_email_does_not_replace_captured_request_email(world):
    c, vid = world
    from app import state
    from app.db.models import Person
    from app.db.supymem_models import GatewayUser

    with state.db()() as session:
        session.add(GatewayUser(id=OWNER, email="account@example.test", email_verified=True,
            password_hash="not-returned", created_at=datetime.now(timezone.utc)))
        session.commit()
    assert _post(c, vid, "original note").json()["created"] is True
    profile_path = f"/admin/users/{OWNER}"
    profile = c.get(profile_path, headers=_admin("GET", profile_path)).json()
    assert profile["email"] == "account@example.test" and profile["verification_status"] == "verified"

    with state.db()() as session:
        session.get(Person, OWNER).email = "new-contact@example.test"
        session.get(GatewayUser, OWNER).email = "new-account@example.test"
        session.commit()
    assert _post(c, vid, "replacement note").json()["created"] is False
    profile = c.get(profile_path, headers=_admin("GET", profile_path)).json()
    assert profile["email"] == "new-account@example.test" and profile["verified"] is True
    request_path = "/admin/god-mode/requests"
    captured = c.get(request_path, headers=_admin("GET", request_path)).json()["items"][0]
    assert captured["email"] == "owner@example.test" and captured["note"] == "original note"
    with state.db()() as session:
        assert session.get(Person, OWNER).email == "new-contact@example.test"


def test_scope_other_workspace_cannot_request_or_read(world):
    c, vid = world
    assert _post(c, vid, sub=OTHER).status_code == 404
    path = f"/ventures/{vid}/god-mode/requests/me"
    assert c.get(path, headers=_sign(OTHER, "GET", path)).status_code == 404


def test_admin_list_and_approve_authz_and_gate(world, monkeypatch):
    c, vid = world
    _post(c, vid)
    lst = "/admin/god-mode/requests"
    assert c.get(lst, headers=_sign(OWNER, "GET", lst)).status_code in {401, 403}
    data = c.get(lst, headers=_admin("GET", lst)).json()
    assert data["counts"] == {"requested": 1, "approved": 0, "declined": 0}
    row = data["items"][0]
    assert row["email"] == "owner@example.test" and row["venture_id"] == vid
    ap = f"/admin/god-mode/requests/{row['id']}/approve"
    assert c.post(ap, headers=_sign(OWNER, "POST", ap)).status_code in {401, 403}

    from app.autopilot import god_requests
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "1")
    put = f"/ventures/{vid}/autopilot"
    body = {"god_mode": True, "acknowledged_beta": True}
    blocked = c.put(put, json=body, headers=_sign(OWNER, "PUT", put))
    assert blocked.status_code == 403 and blocked.json()["detail"]["error"]["code"] == "god_mode_early_access"
    assert not god_requests.beta_gate_open(vid)
    from app.chat.actions import beta_blocked
    assert beta_blocked("autopilot.god_on", vid) is not None  # chat card refused before approval

    done = c.post(ap, headers=_admin("POST", ap))
    assert done.status_code == 200 and done.json()["status"] == "approved" and done.json()["decided_by"]
    assert c.post(ap, headers=_admin("POST", ap)).status_code == 200  # idempotent
    missing = "/admin/god-mode/requests/gmr_nope/approve"
    assert c.post(missing, headers=_admin("POST", missing)).status_code == 404
    assert god_requests.beta_gate_open(vid)
    assert beta_blocked("autopilot.god_on", vid) is None  # the chat gate defers to the same approval
    assert beta_blocked("autopilot.god_on") is not None and beta_blocked("build.start", vid) is not None
    assert c.put(put, json=body, headers=_sign(OWNER, "PUT", put)).status_code == 200  # allowlisted venture passes
    path = f"/ventures/{vid}/god-mode/requests/me"
    assert c.get(path, headers=_sign(OWNER, "GET", path)).json()["status"] == "approved"


def test_analytics_events_emitted_once(world):
    c, vid = world
    path = f"/ventures/{vid}/god-mode/requests/me?viewed=true"
    c.get(path, headers=_sign(OWNER, "GET", f"/ventures/{vid}/god-mode/requests/me"))
    _post(c, vid)
    _post(c, vid)
    lst = "/admin/god-mode/requests"
    rid = c.get(lst, headers=_admin("GET", lst)).json()["items"][0]["id"]
    ap = f"/admin/god-mode/requests/{rid}/approve"
    c.post(ap, headers=_admin("POST", ap))
    from app import state
    from app.db.models import AnalyticsEvent
    with state.db()() as s:
        names = sorted(e.event_name for e in s.query(AnalyticsEvent).filter(AnalyticsEvent.event_name.like("god_mode.%")))
    assert names == ["god_mode.approved", "god_mode.request_viewed", "god_mode.requested"]


@pytest.mark.parametrize("text", ["turn on god mode", "I want god mode", "give me god mode access",
                                  "god mode chahiye", "mujhe god mode chahiye", "god mode on kar do"])
def test_chat_routes_to_early_access_reply_and_never_enables(world, monkeypatch, text):
    c, vid = world
    from app.chat.intent import classify_intent
    assert classify_intent(text) == "god_on"
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "1")
    from app.autopilot import guard

    path = f"/ventures/{vid}/chat"
    out = c.post(path, json={"message": text}, headers=_sign(OWNER, "POST", path))
    assert out.status_code == 200, out.text
    reply = out.json()
    assert "early access" in reply["body_md"] and not any(a.get("confirmation") for a in reply["actions"])
    assert reply["actions"][0]["label"] == "Request God mode access"
    # Chat-first: Settings is hidden, so the button performs the request in chat instead of linking there.
    assert reply["actions"][0]["action"] == "god_mode.request" and "href" not in reply["actions"][0]
    confirm = f"/ventures/{vid}/chat/confirm"
    sent = c.post(confirm, json={"action": "god_mode.request"}, headers=_sign(OWNER, "POST", confirm))
    assert sent.status_code == 200, sent.text
    assert "Request sent" in sent.json()["body_md"]
    again = c.post(confirm, json={"action": "god_mode.request"}, headers=_sign(OWNER, "POST", confirm))
    assert "already asked" in again.json()["body_md"]
    assert not guard.get_settings(vid)["god_mode"]


def test_chat_turn_off_unaffected():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat.intent import classify_intent
    assert classify_intent("turn off god mode") == "god_off"
