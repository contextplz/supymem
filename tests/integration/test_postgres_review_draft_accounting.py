"""Migrated ledger: a review draft never disguises an unknown paid outcome."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


@pytest.fixture
def review_draft(journey_db, monkeypatch):
    from app.chat import assistant
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.db.models import CreditReservation
    from app.llm import founder_provider
    from llm_gateway import sole

    d = {**journey_db, "assertion_secret": "review-draft-synthetic-assertion-only" * 2}
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", d["assertion_secret"])
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "review-draft-synthetic-vault-minimum-32-bytes")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "review-draft-admin@example.test")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(
        model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default"))
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    before = get_account_by_id(account.id)
    calls = []

    def install(*, error=None, usage=None, content="Draft 1\nSubject: Bakery orders\n\nBakery order board for bakery orders."):
        async def transport(messages, **kwargs):
            with d["factory"]() as session:
                hold = session.scalars(select(CreditReservation).where(
                    CreditReservation.account_id == account.id,
                    CreditReservation.purpose == assistant.ASSISTANT_PURPOSE,
                    CreditReservation.status == "reconciling")).one()
                assert hold.workspace_id == d["wid"]
            calls.append(kwargs)
            if error is not None:
                raise error
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop")])
        monkeypatch.setattr(sole, "transmit", transport)

    def rows():
        from app.db.models import CreditLedgerEntry, PlannedTask, VentureDocument
        with d["factory"]() as session:
            tasks = session.scalars(select(PlannedTask).where(PlannedTask.venture_id == d["vid"])).all()
            docs = session.scalars(select(VentureDocument).where(VentureDocument.venture_id == d["vid"])).all()
            holds = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == account.id,
                CreditReservation.purpose == assistant.ASSISTANT_PURPOSE)).all()
            spends = session.scalars(select(CreditLedgerEntry.id).where(
                CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend")).all()
            return dict(tasks=[dict(id=r.id, status=r.status, runnable=r.runnable,
                                   error=r.last_error, finished=r.finished_at) for r in tasks],
                        docs=[dict(id=r.id, body=r.body_md) for r in docs],
                        holds=[dict(id=r.id, status=r.status, amount=r.amount_micro,
                                    settled=r.settled_micro) for r in holds], spends=len(spends))
    return d, install, rows, calls, before, lambda: get_account_by_id(account.id)


def add_task(d):
    from app.planning.store import enqueue_task
    return enqueue_task(d["vid"], {"title": "Draft the outreach emails for me to review",
        "detail": "Write short bakery order board outreach drafts for review. Do not send anything.",
        "tags": ["chat"], "runnable": True}, source="chat")


def assert_unknown(f):
    state = f[2]()
    assert len(state["holds"]) == 1 and state["holds"][0]["status"] == "reconciling"
    assert state["holds"][0]["settled"] is None and state["spends"] == 0
    assert state["docs"] == [] and len(state["tasks"]) == 1
    task = state["tasks"][0]
    assert task["status"] == "open" and task["runnable"] == 0 and task["finished"] is None
    assert task["error"] == "needs_you:review_draft_provider_outcome_unknown"
    assert f[5]().reserved_micro == f[4].reserved_micro + state["holds"][0]["amount"]
    assert f[5]().spent_micro == f[4].spent_micro
    from app.credits.reserve import sweep_expired
    from core.clock import utcnow
    sweep_expired(utcnow() + timedelta(days=2))
    assert f[2]()["holds"][0]["status"] == "reconciling"
    assert len(f[3]) == 1 and f[3][0]["single_attempt"] is True


@pytest.mark.parametrize("outcome", ["timeout", "usage_missing"])
def test_unknown_review_draft_parks_same_task_before_a_second_dispatch(review_draft, outcome):
    from app.planning.execute import TaskRunError, execute_planned_task, start_planned_task
    from app.tenancy import bind
    f = review_draft
    d = f[0]
    f[1](error=TimeoutError("Synthetic timeout") if outcome == "timeout" else None, usage=None)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        task = add_task(d)
        result = asyncio.run(execute_planned_task(d["vid"], task["id"]))
        assert result["status"] == "needs_you" and result["failure_code"] == "review_draft_provider_outcome_unknown"
        assert not any(key in result for key in ("artifact", "completion", "draft_text"))
        with pytest.raises(TaskRunError, match="saved provider"):
            asyncio.run(execute_planned_task(d["vid"], task["id"]))
        with pytest.raises(TaskRunError, match="saved provider"):
            start_planned_task(d["vid"], task["id"])
    assert_unknown(f)


def test_signed_confirm_saves_pause_and_same_task_is_not_withdrawn_or_replayed(review_draft):
    from fastapi.testclient import TestClient
    from app import main
    from app.db.models import ChatMessage
    from core.assertion import reset_nonce_store, sign_assertion

    f = review_draft
    d = f[0]
    reset_nonce_store()
    f[1](usage={})
    path = f'/ventures/{d["vid"]}/chat/confirm'
    payload = {"action": "outreach.draft", "payload": {"run": True,
        "recruitment_request": "Draft one outreach email for review. Do not send anything."}}
    with TestClient(main.create_app()) as client:
        for _ in range(2):
            token = sign_assertion(d["assertion_secret"], sub=d["actor"], email="", role="user", path=path, method="POST")
            response = client.post(path, headers={"X-COS-Assertion": token}, json=payload)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["kind"] == "needs_you" and "paused" in result["body_md"]
            assert "Here are the drafts" not in result["body_md"]
            # Chat-first: the old "Open work" link became the read-only in-chat status action; nothing may re-run the draft.
            from app.chat.navigation_map import _HIDDEN  # read-only in-chat views only; nothing may re-run the draft
            readonly = {target["action"] for _, target in _HIDDEN} - {"pagebuilder.build"}
            assert all(action.get("action") in readonly | {None} for action in result["actions"])
    assert_unknown(f)
    with d["factory"]() as session:
        saved = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == d["vid"])).all()
        assert any(row.kind == "needs_you" and "paused" in row.body_md and "check your saved request" in row.body_md for row in saved)
        assert not any("Finished **" in row.body_md or "Press Run" in row.body_md for row in saved)


def test_actual_worker_fails_owned_job_without_reopening_parked_task(review_draft):
    from app.core.jobs import lease
    from app.db.models import Job, ChatMessage
    from app.planning.execute import start_planned_task
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.tenancy import bind

    f = review_draft
    d = f[0]
    f[1](error=TimeoutError("Synthetic timeout"))
    install_default_handlers()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        task = add_task(d)
        queued = start_planned_task(d["vid"], task["id"])
        assert queued["queued"] is True
    with d["factory"]() as session:
        job = lease(session, "review-draft-worker", kinds=["task.run"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    assert_unknown(f)
    with d["factory"]() as session:
        saved = session.get(Job, job.id)
        assert saved.status == "failed_dead" and saved.last_error == "review_draft_provider_outcome_unknown"
        assert saved.attempts == 1
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == d["vid"])).all()
        assert any(row.kind == "needs_you" for row in messages)
        assert not any("Press Run" in row.body_md or "Finished **" in row.body_md for row in messages)


@pytest.mark.parametrize("tokens", [0, 10])
def test_confirmed_unusable_output_may_save_free_review_template(review_draft, tokens):
    from app.planning.execute import execute_planned_task
    from app.tenancy import bind

    f = review_draft
    d = f[0]
    f[1](usage={"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens}, content="")
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        task = add_task(d)
        result = asyncio.run(execute_planned_task(d["vid"], task["id"]))
    assert result["status"] == "done" and "Draft 1" in result["draft_text"]
    state = f[2]()
    assert state["tasks"][0]["status"] == "done" and state["tasks"][0]["finished"] is not None
    assert len(state["docs"]) == 1 and "Draft 1" in state["docs"][0]["body"]
    assert len(state["holds"]) == state["spends"] == len(f[3]) == 1
    assert state["holds"][0]["status"] == "settled" and state["holds"][0]["settled"] is not None
    assert f[5]().reserved_micro == f[4].reserved_micro


def test_confirmed_auth_rejection_is_released_but_task_still_needs_review(review_draft):
    from app.planning.execute import execute_planned_task
    from app.tenancy import bind
    from llm_gateway.errors import ProviderCallError

    f = review_draft
    d = f[0]
    f[1](error=ProviderCallError("bad_api_key", "Synthetic rejection", status=401))
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        task = add_task(d)
        result = asyncio.run(execute_planned_task(d["vid"], task["id"]))
    assert result["status"] == "needs_you" and result["failure_code"] == "review_draft_bad_api_key"
    state = f[2]()
    assert state["docs"] == [] and state["spends"] == 0 and len(state["holds"]) == len(f[3]) == 1
    assert state["holds"][0]["status"] == "released"
    assert state["tasks"][0]["runnable"] == 0 and state["tasks"][0]["finished"] is None
    assert f[5]().reserved_micro == f[4].reserved_micro


def test_signed_foreign_actor_cannot_create_task_or_dispatch(review_draft):
    from fastapi.testclient import TestClient
    from app import main
    from app.authz.workspace import ensure_subject_person
    from core.assertion import reset_nonce_store, sign_assertion

    f = review_draft
    d = f[0]
    reset_nonce_store()
    foreign = "usr_review_draft_foreign"
    ensure_subject_person(foreign)
    f[1](usage={})
    path = f'/ventures/{d["vid"]}/chat/confirm'
    token = sign_assertion(d["assertion_secret"], sub=foreign, email="", role="user", path=path, method="POST")
    with TestClient(main.create_app()) as client:
        response = client.post(path, headers={"X-COS-Assertion": token}, json={"action": "outreach.draft",
            "payload": {"run": True, "recruitment_request": "Draft one outreach email for review."}})
    assert response.status_code in {403, 404}
    assert f[3] == [] and f[2]()["tasks"] == f[2]()["holds"] == f[2]()["docs"] == []
