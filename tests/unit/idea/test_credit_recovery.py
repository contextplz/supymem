"""Restored person wallets resume saved work only after explicit approval."""
import json

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, run_queued, venture


@pytest.fixture
def failed(client, monkeypatch):
    from tests.idea_fixtures import install_provider
    calls = install_provider(monkeypatch)
    from app.credits.accounts import get_account
    from app.credits.policy import WALLET_PHASE_KEY
    from app.db.models import CreditAccount, CreditPolicy, Job
    from app.state import db
    from core.clock import utcnow
    vid, wid, sid = venture(client)
    wallet = get_account(vid)
    with db()() as session:
        account = session.get(CreditAccount, wallet.id)
        account.granted_micro = account.spent_micro = account.reserved_micro = 0
        phase = session.get(CreditPolicy, WALLET_PHASE_KEY)
        if phase:
            phase.value_json = '"person"'
        else:
            session.add(CreditPolicy(key=WALLET_PHASE_KEY, value_json='"person"', updated_at=utcnow(), updated_by='test'))
        session.add(CreditAccount(id="legacy-recovery", scope="venture", venture_id=vid, workspace_id=wid,
            owner_person_id=wallet.owner_person_id, granted_micro=0, spent_micro=0, reserved_micro=0,
            status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    jid = run_queued("idea.clarify_turn")
    with db()() as session:
        job = session.get(Job, jid)
        assert job.last_error == "clarification_insufficient_credits"
    assert calls == []
    return {"vid": vid, "wid": wid, "sid": sid, "jid": jid, "wallet": wallet.id,
        "calls": calls, "base": f"/ventures/{vid}/idea/sessions/{sid}"}


def restore(data, *, granted=5_000_000_000, reserved=0, status="active"):
    from app.db.models import CreditAccount
    from app.state import db
    with db()() as session:
        account = session.get(CreditAccount, data["wallet"])
        account.granted_micro, account.reserved_micro, account.status = granted, reserved, status
        session.commit()


def test_status_rechecks_current_person_wallet_and_explicit_continue_is_idempotent(failed, client):
    from app.db.models import CreditAccount, CreditReservation, Job
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun
    from app.state import db
    from app.ventures.run_status import snapshot
    data = failed
    before = request(client, "GET", data["base"]).json()
    assert before["credits_continue_job_id"] is None
    assert snapshot(data["vid"], data["wid"])["action"]["label"] == "Request credits"
    with db()() as session:
        original = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == data["jid"]))
        original_snapshot, original_id = original.snapshot, original.id
        turns = len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == data["sid"])).all())
    restore(data)
    refreshed = request(client, "POST", f'/ventures/{data["vid"]}/idea/sessions').json()
    assert refreshed["credits_continue_job_id"] == data["jid"]
    view = snapshot(data["vid"], data["wid"])
    assert view["state"] == "needs_you"
    assert view["action"] == {"label": "Continue", "href": f'/v/{data["vid"]}/idea#journey-action'}
    assert "out of credits" not in view["activity"] and "continue now" in view["activity"]
    path = data["base"] + "/retry-clarification"
    body = {"failed_job_id": data["jid"]}
    first = request(client, "POST", path, json=body)
    second = request(client, "POST", path, json=body)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["job_id"] == second.json()["job_id"] == data["jid"]
    assert data["calls"] == []
    with db()() as session:
        assert session.get(CreditAccount, data["wallet"]).spent_micro == 0
        assert session.get(CreditAccount, data["wallet"]).reserved_micro == 0
        assert len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == data["sid"])).all()) == turns
    run_queued("idea.clarify_turn", through_job_id=data["jid"])
    assert len(data["calls"]) == 1
    replay = request(client, "POST", path, json=body)
    assert replay.status_code == 200 and replay.json()["job_id"] == data["jid"]
    with db()() as session:
        run = session.get(IdeaClarificationRun, original_id)
        assert run.snapshot == original_snapshot and run.state == "completed"
        account = session.get(CreditAccount, data["wallet"])
        assert account.spent_micro > 0 and account.reserved_micro == 0
        holds = session.scalars(select(CreditReservation).where(CreditReservation.account_id == account.id)).all()
        assert len(holds) == 1 and holds[0].status == "settled"
        assert session.get(CreditAccount, "legacy-recovery").spent_micro == 0
        assert session.get(Job, data["jid"]).attempts == 2


@pytest.mark.parametrize("change", ["short", "reserved", "suspended", "uncertain", "paid", "stale"])
def test_recovery_remains_fail_closed(failed, client, change):
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun, IdeaSession
    from app.state import db
    from app.credits.recovery import eligible_job
    data = failed
    restore(data, granted=0 if change == "short" else 5_000_000_000,
        reserved=5_000_000_000 if change == "reserved" else 0,
        status="suspended" if change == "suspended" else "active")
    with db()() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == data["jid"]))
        if change == "uncertain":
            run.state = "uncertain"
        elif change == "paid":
            run.usage = '{"reservation_id":"unknown"}'
        elif change == "stale":
            session.get(IdeaSession, data["sid"]).current_revision_id = "newer-revision"
        session.commit()
        assert not eligible_job(session, session.get(Job, data["jid"]))
    result = request(client, "POST", data["base"] + "/retry-clarification", json={"failed_job_id": data["jid"]})
    assert result.status_code == 409
    assert data["calls"] == []


def test_exact_quote_is_available_but_one_micro_short_is_not(failed):
    from app.credits.recovery import eligible_job
    from app.credits.estimate import count_prompt_tokens, estimate_llm_micro
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea.agent import messages_for
    from app.idea.runner import MAX_TOKENS
    from app.llm.founder_provider import resolve_llm_config
    from app.state import db
    with db()() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == failed["jid"]))
        required = estimate_llm_micro(resolve_llm_config(failed["wid"]).model,
            prompt_tokens=count_prompt_tokens(messages_for(json.loads(run.snapshot))), max_tokens=MAX_TOKENS)
    for amount in (required - 1, required):
        restore(failed, granted=amount)
        with db()() as session:
            assert eligible_job(session, session.get(Job, failed["jid"])) == (amount == required)


def test_display_uses_latest_founder_statement_not_model_scaffolding(failed, client):
    from app.db.models import Venture
    from app.state import db
    from app.ventures.current_idea import current_idea, idea_summary
    statement = "A quiet coffee shop with online table reservations for remote workers."
    result = request(client, "POST", failed["base"] + "/messages", json={"text": statement})
    assert result.status_code == 200, result.text
    with db()() as session:
        venture = session.get(Venture, failed["vid"])
        venture.raw_idea = "hello"
        session.commit()
        context = current_idea(venture, session=session)
        assert "Latest founder statement" in context and "hello" in context
        assert idea_summary(venture, session=session) == statement
    saved = request(client, "GET", failed["base"]).json()
    assert saved["idea_summary"] == saved["current_idea"] == statement
    detail = request(client, "GET", f'/ventures/{failed["vid"]}').json()
    assert detail["idea_summary"] == detail["current_idea"] == statement
    assert detail["raw_idea"] == "hello"


def test_confirmed_display_uses_current_fields_without_stale_summary(failed):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from app.ventures.current_idea import idea_summary
    with db()() as session:
        idea = session.get(IdeaSession, failed["sid"])
        revision = session.get(IdeaRevision, idea.current_revision_id)
        revision.source, revision.summary = "agent", "hello"
        revision.structured = json.dumps({"customer": "Remote workers", "offer": "Book a cafe table", "problem": "No quiet space"})
        idea.state, idea.confirmed_revision_id = "confirmed", revision.id
        session.commit()
        display = idea_summary(session.get(Venture, failed["vid"]), session=session)
        assert "Remote workers" in display and "Book a cafe table" in display
        assert "hello" not in display and "Summary" not in display


@pytest.mark.parametrize("legacy_quote", [False, True])
def test_research_continue_preserves_completed_receipts_and_charges_only_failed_step(failed, client, monkeypatch, legacy_quote):
    from app.core.jobs import FatalError, lease
    from app.db.models import CreditAccount, Job
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier
    from app.research import execution, metered
    from app.research.service import continue_failed
    from app.credits.policy import web_search_credits
    from app.credits.units import credits_to_micro
    from app.state import db
    from app.tenancy import bind
    from app.credits.recovery import eligible_job
    from core.clock import utcnow
    data = failed
    quote = credits_to_micro(web_search_credits())
    with db()() as session:
        idea = session.get(IdeaSession, data["sid"])
        idea.state, idea.confirmed_revision_id, idea.confirmed_at = "confirmed", idea.current_revision_id, utcnow()
        session.get(IdeaRevision, idea.current_revision_id).structured = '{"problem":"Remote workers need quiet cafe tables"}'
        prior = session.get(Job, data["jid"])
        payload = {**json.loads(prior.payload_json), "_job_id": "research-credit-failed",
            "_worker_id": "worker", "_job_attempt": 1}
        actor = payload["_actor_subject"]
        session.add(Job(id=payload["_job_id"], kind="research.deep", venture_id=data["vid"], workspace_id=data["wid"],
            payload_json=json.dumps(payload), status="leased", locked_by="worker", attempts=1, created_at=utcnow()))
        session.commit()
    did = execution.claim(payload, kind="deep", budget=10)
    restore(data, granted=quote)
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=actor):
        metered.perform(did, "direct", payload, purpose="research.search", provider="test", estimate=quote,
            callback=lambda: {"sources": ["saved-source"]})
    completed = execution.operation(did, "direct")
    searches = []
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=actor):
        with pytest.raises(FatalError, match="research_insufficient_credits"):
            metered.perform(did, "alternatives", payload, purpose="research.search", provider="test",
                estimate=quote, callback=lambda: searches.append(True) or {"sources": []})
    with db()() as session:
        job = session.get(Job, payload["_job_id"])
        job.status, job.last_error = "failed_dead", "research_insufficient_credits"
        dossier = session.get(ResearchDossier, did)
        document = json.loads(dossier.summary)
        document["error_code"] = job.last_error
        if legacy_quote:
            document["_runtime"]["operations"]["alternatives"].pop("required_micro")
            document["_runtime"]["operations"]["alternatives"].pop("error_code")
        dossier.state, dossier.summary = "partial", json.dumps(document)
        session.commit()
        assert not eligible_job(session, job)
    restore(data, granted=2 * quote)
    with db()() as session:
        assert eligible_job(session, session.get(Job, payload["_job_id"]))
    saved = request(client, "GET", data["base"]).json()
    assert saved["credits_continue_job_id"] == payload["_job_id"]
    assert saved["credits_continue_kind"] == "research.deep"
    path = f'/ventures/{data["vid"]}/research/dossier/regenerate'
    for _ in range(2):
        response = request(client, "POST", path, json={"failed_job_id": payload["_job_id"]})
        assert response.status_code == 200 and response.json()["job_id"] == payload["_job_id"], response.text
    assert searches == []
    assert execution.operation(did, "direct") == completed
    assert execution.read(did).state == "running"
    with db()() as session:
        assert session.get(CreditAccount, data["wallet"]).spent_micro == quote
        jobs = lease(session, "recovery-worker", kinds=["research.deep"], n=1)
        session.commit()
    leased = jobs[0]
    resumed = {**leased.payload, "_job_id": leased.id, "_worker_id": "recovery-worker", "_job_attempt": leased.attempts}
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=actor):
        assert metered.perform(did, "direct", resumed, purpose="research.search", provider="test", estimate=quote,
            callback=lambda: pytest.fail("completed search must never be sent twice")) == completed["response"]
        for _ in range(2):
            assert metered.perform(did, "alternatives", resumed, purpose="research.search", provider="test", estimate=quote,
                callback=lambda: searches.append(True) or {"sources": []}) == {"sources": []}
    assert searches == [True]
    with db()() as session:
        account = session.get(CreditAccount, data["wallet"])
        assert account.spent_micro == 2 * quote and account.reserved_micro == 0
        assert session.get(CreditAccount, "legacy-recovery").spent_micro == 0
        session.get(Job, resumed["_job_id"]).status = "succeeded"
        session.commit()
    assert continue_failed(data["vid"], resumed["_job_id"])["job_id"] == resumed["_job_id"]
