"""The HTTP build acknowledges the exact displayed cost/input/provider quote."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.company_os_harness import append_idea_revision
from tests.unit.pagebuilder import test_api

setup = test_api.setup


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_evaluation_checks_private_ceiling_before_queueing(setup, monkeypatch, domain, ready):
    from app.pagebuilder import api, eval_support
    from tests.unit.pagebuilder.test_pipeline import fixture_draft, fixture_input, nightshift_draft

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setattr(eval_support, "_WORKSPACE", None)
    monkeypatch.setattr(api, "estimate", lambda *args: {"ready": ready})

    def resources(*args):
        assert ready, "Unready evaluation read private resources"
        return {"ceiling_micro": 2000000}

    monkeypatch.setattr(api, "resources", resources)
    monkeypatch.setattr(api, "start", lambda *args: pytest.fail("Over-budget evaluation queued work"))
    inputs, draft = nightshift_draft() if domain == "b2b" else (fixture_input(), fixture_draft())
    fixture = {"slug": min(eval_support.SLUGS), "name": inputs["name"], "idea": inputs["idea"],
        "audience": draft["brief"]["audience"], "category": "concept"}
    with pytest.raises(ValueError, match="pagebuilder_eval_budget_or_provider_unavailable"):
        eval_support.run_evaluation(fixture, cap_usd=1, mode="live")


@pytest.mark.parametrize("idea", ["Equipment inspections for commercial building managers", "Weekend cycling trips for families"])
@pytest.mark.parametrize("blocked", ["limit", "validated_limit", "offline", "waitlist"])
def test_page_build_unavailable_only_offers_buy_more_at_the_limit(setup, monkeypatch, idea, blocked):
    import asyncio
    from app.chat.execute import execute_action
    from app.credits.errors import LIMIT_MESSAGE
    from app.db.models import CreditAccount, Venture
    from app.db.supymem_models import IdeaRevision, VentureWaitlist
    from app.pagebuilder import api
    from app.tenancy import bind
    from app.conversion.waitlist import VERSION
    from core.clock import utcnow

    data = setup
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        data["rid"] = append_idea_revision(session, data["rid"], summary=idea).id
        if blocked in {"limit", "validated_limit"}:
            session.get(CreditAccount, data["account"].id).granted_micro = 0
        elif blocked == "waitlist":
            session.add(VentureWaitlist(venture_id=data["vid"], workspace_id=data["wid"], enabled=False,
                consent_version=VERSION, enabled_by=data["actor"], updated_at=utcnow()))
        session.commit()
    if blocked == "offline":
        monkeypatch.setenv("LLM_OFFLINE", "1")
    if blocked == "validated_limit":
        _validated_unconfirmed(data)
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        reply = asyncio.run(execute_action(data["vid"], "pagebuilder.build", payload={"prepare": True}, actor_subject=data["actor"]))
    if blocked in {"limit", "validated_limit"}:
        assert reply["body_md"] == LIMIT_MESSAGE
        assert reply["actions"][0]["label"] == "Buy more"
        assert api.price(data["vid"], data["actor"])["reason"] == "pagebuilder_insufficient_credits"
    else:
        assert "Buy more" not in str(reply) and reply["actions"]
    assert "credit" not in reply["body_md"].lower() and "—" not in reply["body_md"]
    assert_no_effects(data)


def client_for_test():
    from app.api.pagebuilder_api import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def assert_no_effects(data):
    from app.credits.accounts import get_account
    from app.db.models import Job
    from app.pagebuilder import api
    with data["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == api.KIND)) == 0
    account = get_account(data["vid"])
    assert account.spent_micro == 0 and account.reserved_micro == 0


def _validated_unconfirmed(data, complete=True, name=None, idea=None):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession, ValidationRun
    from app.idea.validation import METHODS
    from core.clock import utcnow
    with data["factory"]() as session:
        row = session.get(IdeaSession, data["sid"])
        row.state, row.confirmed_revision_id = "validating", None
        if name:
            session.get(Venture, data["vid"]).name = name
            data["rid"] = append_idea_revision(session, data["rid"], summary=idea).id
        for method in METHODS:
            session.add(ValidationRun(id=f"review_{data['vid']}_{method}", session_id=data["sid"], revision_id=data["rid"],
                workspace_id=data["wid"], venture_id=data["vid"], method=method,
                state="complete" if complete or method != METHODS[-1] else "pending", created_at=utcnow()))
        session.commit()


def _jobs(data, kind):
    from app.db.models import Job
    with data["factory"]() as session:
        return list(session.scalars(select(Job).where(Job.kind == kind, Job.venture_id == data["vid"])))


def test_unfinished_review_says_so_with_an_in_chat_next_step(setup):
    import asyncio
    from app.chat import router
    from app.chat.execute import _execute_action
    from app.tenancy import bind
    data = setup
    _validated_unconfirmed(data, complete=False)
    route = router._one_click_page_reply(data["vid"], "build my landing page")
    assert route and route["actions"][0]["action"] == "pagebuilder.build"
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        prepared = asyncio.run(_execute_action(data["vid"], "pagebuilder.build", payload={"prepare": True}))
    assert prepared["kind"] == "needs_you" and "not finished" in prepared["body_md"]
    assert prepared["actions"] == [{"label": "Continue idea review", "action": "idea.review", "style": "primary"}]
    assert_no_effects(data)


def test_one_confirmation_confirms_the_idea_and_queues_the_build_for_two_ventures(setup):
    import asyncio
    from app.chat.execute import _execute_action
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.pagebuilder import api
    from app.tenancy import bind
    second = test_api.seed(setup["factory"], actor="pagebuilder-test-owner-2")
    ventures = [(setup, "Nightshift", "A shift handoff board for warehouse supervisors."),
                (second, "Petal Ledger", "Bookkeeping that florists can run from their phone between deliveries.")]
    for data, name, idea in ventures:
        _validated_unconfirmed(data, name=name, idea=idea)
        with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
            prepared = asyncio.run(_execute_action(data["vid"], "pagebuilder.build", payload={"prepare": True}))
            assert prepared["kind"] == "confirmation" and len(prepared["actions"]) == 1
            confirmation = prepared["actions"][0]["confirmation"]
            assert confirmation["action"] == "idea.confirm"
            state = confirmation["resulting_state"]
            assert state["session_id"] == data["sid"] and state["revision_id"] == data["rid"] and state["build_landing_page"] is True
            assert type(state["ceiling_micro"]) is int and state["ceiling_micro"] > 0
            text = " ".join(confirmation["consequences"])
            assert "credit" not in text.lower() and "Nothing is published" in text and "confirms your idea" in text
            assert not _jobs(data, api.KIND)
            built = asyncio.run(_execute_action(data["vid"], "idea.confirm", payload=state))
        assert "Building your private landing page" in built["body_md"] and built["run_id"]
        with data["factory"]() as session:
            assert session.get(IdeaSession, data["sid"]).confirmed_revision_id == data["rid"]
            assert set(session.scalars(select(ValidationRun.founder_decision).where(
                ValidationRun.venture_id == data["vid"]))) == {"accept"}
        jobs = _jobs(data, api.KIND)
        assert [job.id for job in jobs] == [built["run_id"]] and jobs[0].status == "scheduled"
        assert not _jobs(data, "research.deep")


def test_changed_price_after_the_card_starts_nothing_and_shows_the_new_limit(setup, monkeypatch):
    import asyncio
    from app.chat.execute import _execute_action
    from app.db.supymem_models import IdeaSession
    from app.pagebuilder import api
    from app.tenancy import bind
    data = setup
    _validated_unconfirmed(data)
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        prepared = asyncio.run(_execute_action(data["vid"], "pagebuilder.build", payload={"prepare": True}))
        state = prepared["actions"][0]["confirmation"]["resulting_state"]
        stale = {**state, "ceiling_micro": state["ceiling_micro"] - 1}
        quoted = asyncio.run(_execute_action(data["vid"], "idea.confirm", payload=stale))
    assert quoted["actions"][0]["confirmation"]["action"] == "pagebuilder.build"
    assert "Nothing is published" in " ".join(quoted["actions"][0]["confirmation"]["consequences"])
    with data["factory"]() as session:
        assert session.get(IdeaSession, data["sid"]).confirmed_revision_id == data["rid"]
    assert_no_effects(data)


def test_http_requires_quote_and_accepts_same_quote_idempotently(setup):
    data = setup
    base = f"/api/ventures/{data['vid']}/pagebuilder"
    headers = {"X-Subject": data["actor"]}
    with client_for_test() as client:
        missing = client.post(base + "/start", json={"variants": 2}, headers=headers)
        assert missing.status_code == 409 and missing.json()["detail"]["code"] == "pagebuilder_quote_required"
        assert_no_effects(data)
        quote = client.post(base + "/estimate", json={"variants": 2}, headers=headers).json()
        assert quote["seconds"] == 600
        accepted = client.post(base + "/start", json={"variants": 2, "quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert accepted.status_code == 202 and accepted.json()["variant_count"] == 2
        # Returning the same existing job does not authorize another charge.
        repeated = client.post(base + "/start", json={"variants": 1}, headers=headers)
        assert repeated.status_code == 202 and repeated.json()["job_id"] == accepted.json()["job_id"]


def test_changed_policy_reasoning_deadline_calls_or_price_rejects_quote_without_job_or_spend(setup, monkeypatch):
    from app.pagebuilder import api
    data = setup
    base = f"/api/ventures/{data['vid']}/pagebuilder"
    headers = {"X-Subject": data["actor"]}
    with client_for_test() as client:
        quote = client.post(base + "/estimate", json={"variants": 1}, headers=headers).json()
        policy = api.REQUEST_POLICY
        monkeypatch.setattr(api, "REQUEST_POLICY", policy + "-changed")
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
        assert_no_effects(data)
        monkeypatch.setattr(api, "REQUEST_POLICY", policy)
        original_reasoning = api.reasoning_policy
        monkeypatch.setattr(api, "REQUEST_POLICY", "scoped_grounding_v18")
        prior_quote = client.post(base + "/estimate", json={}, headers=headers).json()
        monkeypatch.setattr(api, "REQUEST_POLICY", policy)
        monkeypatch.setattr(api, "reasoning_policy", original_reasoning)
        rejected = client.post(base + "/start", json={"quote_fingerprint": prior_quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
        assert_no_effects(data)
        monkeypatch.setattr(api, "reasoning_policy", lambda provider, model: {
            **original_reasoning(provider, model), "planning": "none"})
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
        assert_no_effects(data)
        monkeypatch.setattr(api, "reasoning_policy", original_reasoning)
        monkeypatch.setattr(api, "reasoning_policy", lambda provider, model: {
            **original_reasoning(provider, model), "reviewer": "none"})
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
        assert_no_effects(data)
        monkeypatch.setattr(api, "reasoning_policy", original_reasoning)
        timeout = api.CALL_TIMEOUT_SECONDS
        monkeypatch.setattr(api, "CALL_TIMEOUT_SECONDS", timeout - 1)
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
        assert_no_effects(data)
        monkeypatch.setattr(api, "CALL_TIMEOUT_SECONDS", timeout)
        calls = api.CALLS_PER_VARIANT
        monkeypatch.setattr(api, "CALLS_PER_VARIANT", 8)
        monkeypatch.setattr(api, "REQUEST_POLICY", "scoped_grounding_v8")
        old_quote = client.post(base + "/estimate", json={}, headers=headers).json()
        monkeypatch.setattr(api, "CALLS_PER_VARIANT", calls)
        monkeypatch.setattr(api, "REQUEST_POLICY", policy)
        rejected = client.post(base + "/start", json={"quote_fingerprint": old_quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
        assert_no_effects(data)
        original = api.resources
        def changed(*args):
            resources = original(*args)
            return {**resources, "resource_digest": api.digest([resources["resource_digest"], "updated-model-price"]),
                    "call_ceiling_micro": resources["call_ceiling_micro"] + 100,
                    "ceiling_micro": resources["ceiling_micro"] + 100 * api.CALLS_PER_VARIANT}
        monkeypatch.setattr(api, "resources", changed)
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
    assert_no_effects(data)


@pytest.mark.parametrize("model", ["gpt-6-sol", "claude-haiku-4-5"])
def test_expensive_configured_model_remains_blocked_by_25_credit_cap(setup, monkeypatch, model):
    from app.pagebuilder import api
    data = setup
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", model)
    base = f"/api/ventures/{data['vid']}/pagebuilder"
    headers = {"X-Subject": data["actor"]}
    with client_for_test() as client:
        quote = client.post(base + "/estimate", json={}, headers=headers).json()
        assert quote["credits"] > 25 and quote["ready"] is False
        assert api.MAX_PAGE_MICRO == 250000
        assert quote["ceiling_micro"] > api.MAX_PAGE_MICRO
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 503 and rejected.json()["detail"]["code"] == "pagebuilder_model_exceeds_limit"
    assert_no_effects(data)


def test_changed_confirmed_revision_rejects_quote_without_job_or_spend(setup):
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from core.clock import utcnow
    from core.ids import new_id
    data = setup
    base = f"/api/ventures/{data['vid']}/pagebuilder"
    headers = {"X-Subject": data["actor"]}
    with client_for_test() as client:
        quote = client.post(base + "/estimate", json={}, headers=headers).json()
        with data["factory"]() as session:
            revision_id = new_id("idv")
            session.add(IdeaRevision(id=revision_id, session_id=data["sid"], workspace_id=data["wid"], venture_id=data["vid"],
                rev=2, source="founder", summary="A concept for a revised shift handoff board for warehouse supervisors.",
                structured="{}", created_at=utcnow()))
            idea = session.get(IdeaSession, data["sid"])
            idea.current_revision_id = idea.confirmed_revision_id = revision_id
            session.commit()
        rejected = client.post(base + "/start", json={"quote_fingerprint": quote["quote_fingerprint"]}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "pagebuilder_quote_changed"
    assert_no_effects(data)
