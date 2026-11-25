"""Beta funnel: every step emits a durable, attributed event and the per-founder read counts it."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_durable_journey_boundary import provider_network, request, venture  # noqa: F401
from tests.security.test_venture_waitlist import setup_waitlist

ROOT = Path(__file__).resolve().parents[2]
# Emitted by the client (session.active, chat.* via the chat router) or added by another change.
NOT_IN_APP_SOURCE = {"god_mode.requested", "session.active", "venture.limit_reached"}  # retired policy, historical counts remain


def events(name, **where):
    from app.db.models import AnalyticsEvent
    from app.state import db

    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(AnalyticsEvent.event_name == name)).all()
    return [r for r in rows if all(getattr(r, k) == v for k, v in where.items())]


def test_signed_legacy_founder_greeting_has_no_count_cap_or_paid_dispatch(client, monkeypatch):  # noqa: F811
    from sqlalchemy import func
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Venture, Workspace
    from app.intake import smart
    from app.state import db
    from llm_gateway import sole
    from tests.unit.credits.test_wallet_bridge import phase_marker

    phase_marker("legacy")
    ids = []
    for number in (1, 2):
        created = request(client, "POST", "/ventures", json={"name": f"Owned {number}",
            "raw_idea": "A rehearsal studio scheduling board", "start": False})
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    with db()() as session:
        session.get(Venture, ids[0]).archived_at = datetime.now(timezone.utc)
        session.commit()

    def counts():
        with db()() as session:
            return [session.scalar(select(func.count()).select_from(model)) for model in
                (Venture, Workspace, CreditAccount, CreditLedgerEntry, CreditReservation)]

    dispatched = {"classifier_model": 0, "provider": 0}
    async def classifier(*args, **kwargs):
        dispatched["classifier_model"] += 1
        return None
    async def provider(*args, **kwargs):
        dispatched["provider"] += 1
        return None
    monkeypatch.setattr(smart, "_call_model", classifier)
    monkeypatch.setattr(sole, "transmit", provider)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    before = counts()
    response = request(client, "POST", "/entry/chat/preview", json={"text": "hello"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "clarify"
    from app.entry.chat_start import EntryChatProposal
    with db()() as session:
        saved = session.get(EntryChatProposal, result["id"])
        assert saved.route == "clarify"
        assert not json.loads(saved.receipt_json)["intake"]["extracted"]["idea"]
    assert counts() == before
    assert dispatched == {"classifier_model": 0, "provider": 0}


def test_every_funnel_event_is_allowed_and_has_an_emit_site(client):  # noqa: F811
    from app.analytics.events import _allowed
    from app.analytics.funnel import FUNNEL, FUNNEL_EVENT_NAMES, JOB_EVENTS

    assert FUNNEL_EVENT_NAMES and all(_allowed(n) for n in FUNNEL_EVENT_NAMES)
    assert len({key for key, *_ in FUNNEL}) == len(FUNNEL)
    sources = "\n".join(p.read_text() for p in (ROOT / "services/company_os/app").rglob("*.py")
                        if p.name not in {"funnel.py", "events.py"})
    job_names = {n for outcomes in JOB_EVENTS.values() for n in outcomes.values()}
    for name in sorted(FUNNEL_EVENT_NAMES - NOT_IN_APP_SOURCE - job_names):
        assert f'"{name}"' in sources or f"'{name}'" in sources, f"{name} is in the funnel but nothing emits it"
    assert {"research.started", "research.completed", "research.failed", "site.unpublished"} <= job_names


def test_signup_invite_limit_chat_and_waitlist_emit_attributed_events(client, monkeypatch):  # noqa: F811
    from app.analytics.funnel import founder_funnel
    from app.conversion import waitlist
    from app.consumer import referrals
    from app.credits.accounts import account_for_venture
    from app.db.models import Person, Workspace
    from app.db.supymem_models import ReferralAdmission
    from app.state import db
    from app.ventures import quota

    vid, wid, path, payload = setup_waitlist(client)  # enables the waitlist as usr_alice
    with db()() as session:
        owner = session.get(Workspace, wid).owner_id
        session.merge(Person(id=owner, display_name="Alice", email="alice@example.com", created_at=datetime.now(timezone.utc)))
        session.commit()
    assert len(events("venture.created")) >= 1
    enabled = events("waitlist.enabled", venture_id=vid)
    assert len(enabled) == 1 and enabled[0].person_id and enabled[0].workspace_id == wid
    request(client, "PUT", path, json={"enabled": True})  # no state change: not counted again
    assert len(events("waitlist.enabled", venture_id=vid)) == 1

    # A founder-run test is recorded, and a test row never counts as a real signup.
    def unconfirmed(*a):
        raise waitlist.WaitlistError("waitlist_public_submission_unconfirmed", 503)
    monkeypatch.setattr(waitlist, "_probe", unconfirmed)
    try:
        waitlist.probe(vid, wid)
    except waitlist.WaitlistError:
        pass
    monkeypatch.setattr(waitlist, "_probe", lambda *a: {"ok": True})
    assert waitlist.probe(vid, wid) == {"ok": True}
    assert len(events("waitlist.test_failed", venture_id=vid)) == len(events("waitlist.tested", venture_id=vid)) == 1
    assert client.post(f"/public/waitlists/{vid}", json=payload).status_code == 201

    # Admission commits its receipt and the signup events together; a retry cannot double count.
    monkeypatch.setattr(referrals, "redeem", lambda *a, **k: None)
    with db()() as session:
        referrals.admit(session, person_id=owner, user_id=owner, invite_code="ABCD2345", required=True)
        session.commit()
    with db()() as session:
        referrals.admit(session, person_id=owner, user_id=owner, invite_code="ABCD2345", required=True)
        session.commit()
        assert session.get(ReferralAdmission, owner) is not None
    assert len(events("signup", person_id=owner)) == len(events("invite.redeemed", person_id=owner)) == 1

    # The retired venture-count policy cannot cap a founder or mint another
    # allowance: even a third venture uses the same founder wallet.
    monkeypatch.delenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", raising=False)
    quota.assert_can_create_venture(owner)
    with db()() as session:
        wallet = account_for_venture(vid, session=session)
        assert wallet is not None and wallet.scope == "person" and wallet.owner_person_id == owner
        wallet_id, granted = wallet.id, wallet.granted_micro
    for number in (2, 3):
        created = request(client, "POST", "/ventures", json={
            "name": f"Synthetic venture {number}", "raw_idea": "A rehearsal studio booking page", "start": False})
        assert created.status_code == 201, created.text
        with db()() as session:
            shared = account_for_venture(created.json()["id"], session=session)
            assert shared is not None and shared.id == wallet_id and shared.granted_micro == granted
    assert not events("venture.limit_reached", person_id=owner)

    from app.chat import conversations
    from app.tenancy import bind
    with bind(workspace_id=wid, venture_id=vid, actor="usr_alice"):
        first = conversations.create(vid, idempotency_key="k1")
        again = conversations.create(vid, idempotency_key="k1")
    assert first["id"] == again["id"] and len(events("chat.new_conversation", venture_id=vid)) == 1

    from app.analytics.events import emit
    emit("needs_you.created", workspace_id=wid, venture_id=vid)  # emitters without an actor fall back to the owner
    emit("chat.tool_invoked", person_id=owner, venture_id=vid, props={"tool": "memory.remember"})
    emit("chat.tool_invoked", person_id=owner, venture_id=vid, props={"tool": "status"})
    emit("chat.tool_invoked", person_id="usr_outsider", venture_id=vid, props={"tool": "memory.forget"})
    result = founder_funnel()
    assert result["founders_total"] == 1
    row = result["founders"][0]
    assert row["email"] == "alice@example.com"
    c = row["counts"]
    assert (c["signup"], c["invite_redeemed"], c["venture_limit_reached"], c["new_chat"]) == (1, 1, 0, 1)
    assert c["waitlist_enabled"] == 1 and c["needs_you_shown"] == 1
    assert c["memory_remember"] == 1 and c["memory_forget"] == 0  # other tools and non-cohort people excluded
    assert c["real_waitlist_signups"] == 1  # the QA test row is excluded
    assert result["founders_reached"]["signup"] == 1 and result["founders_reached"]["email_sent"] == 0


def test_job_outcomes_and_feedback_and_admin_route(client):  # noqa: F811
    from types import SimpleNamespace

    from app.analytics.funnel import stage_job_outcome
    from app.state import db

    vid, wid, _ = venture(client)
    job = SimpleNamespace(id="job_funnel1", kind="research.deep", venture_id=vid, workspace_id=wid)
    with db()() as session:
        for outcome in ("started", "completed", "completed", "failed"):
            stage_job_outcome(session, job, outcome)
        stage_job_outcome(session, SimpleNamespace(**{**vars(job), "kind": "deploy.run"}, ), "completed")  # no event
        session.commit()
    assert [len(events(n, venture_id=vid)) for n in
            ("research.started", "research.completed", "research.failed", "site.publish_failed")] == [1, 1, 1, 0]
    assert events("research.completed")[0].person_id  # attributed to the venture owner

    r = request(client, "POST", f"/ventures/{vid}/feedback", json={"text": "the research was slow", "source": "founder"})
    assert r.status_code == 200, r.text
    fb = events("feedback.submitted", venture_id=vid)
    assert len(fb) == 1 and json.loads(fb[0].props_json) == {"kind": "venture_feedback", "source": "founder"}
    assert "slow" not in fb[0].props_json

    from app.admin.router import router
    assert "/admin/funnel" in {route.path for route in router.routes}
    assert re.search(r'"/funnel"\)\s+def admin_funnel\(ctx: AdminContext = Depends\(require_admin\("users.view"\)',
                     (ROOT / "services/company_os/app/admin/router.py").read_text())
