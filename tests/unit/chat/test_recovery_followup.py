"""Recovery follow-up: remaining job deaths, provider-error card, clarification retry, founder results used downstream."""
import asyncio
import json

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_recovery import _alt, _events, _messages
from tests.unit.chat.test_round3_matrix import venture  # noqa: F401
from tests.integration.test_postgres_chat_request_replay import chat_turn  # noqa: F401
from tests.integration.test_postgres_auth_security import pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db  # noqa: F401
from tests.unit.chat.test_chat_request_status import chat_request  # noqa: F401

load_company_os_app()


@pytest.mark.parametrize("idea", ["Shipment invoice reconciliation for freight coordinators", "Weekend meal planning for families"])
def test_completed_reply_survives_unknown_outcome_and_exact_retry(chat_turn, monkeypatch, idea):
    from sqlalchemy import func, select
    from app.chat import requests
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.credits.accounts import account_for_venture, get_account_by_id
    from app.credits.migration import migrate_wallets
    from app.credits.gate import charge_sync
    from app.credits.policy import wallet_phase
    from app.credits.reserve import release
    from app.credits.errors import CreditError
    from app.db.models import ChatMessage, CreditLedgerEntry, CreditReservation, Job, Venture
    from app.db.supymem_models import FounderRequest
    from app.tenancy import bind

    data, _, client, path, headers, body, api = chat_turn
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        migrate_wallets(session)
        session.commit()
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]), bind_conversation(data["vid"], body["conversation_id"]):
        account = account_for_venture(data["vid"])
        assert wallet_phase() == "person" and account.scope == "person"
        admission = requests._admission(data["vid"], data["actor"], body["idempotency_key"], "")
        with pytest.raises(TimeoutError):
            with charge_sync(account_id=account.id, purpose="venture.chat.assistant", estimate_micro=5000,
                             request_id=admission.id) as meter:
                meter.protect_outcome()
                raise TimeoutError("Private provider detail")
    before = get_account_by_id(account.id)

    def counts():
        with data["factory"]() as session:
            return tuple(session.scalar(select(func.count()).select_from(model))
                         for model in (CreditLedgerEntry, CreditReservation, Job))

    before_counts = counts()
    attempts, replies = [], []
    body["message"] = f"Show your saved notes about {idea}."

    async def handler(venture_id, text, **kwargs):
        attempts.append(text)
        append_message(venture_id, role="user", body_md=text)
        saved = append_message(venture_id, role="assistant", body_md="Your notes are saved. Review them here.")
        replies.append(saved)
        return saved

    original = requests.complete

    def unavailable(*args, **kwargs):
        raise RuntimeError("Private database detail")

    monkeypatch.setattr(api, "handle_message", handler)
    monkeypatch.setattr(requests, "complete", unavailable)
    first = client.post(path, headers=headers(), json=body)
    assert first.status_code == 409
    assert first.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert "Retry" in first.json()["detail"]["error"]["message"]
    assert first.json()["detail"]["error"]["actions"] == [{"label": "Retry", "action": "status", "payload": {}, "retry": True}]
    with data["factory"]() as session:
        journal = session.scalar(select(FounderRequest).where(FounderRequest.venture_id == data["vid"],
                                                            FounderRequest.source == "chat_turn_v1"))
        assert journal.outcome == "unknown"
        assert json.loads(journal.text).get("response") == replies[0]
    monkeypatch.setattr(requests, "complete", original)
    conflict = client.post(path, headers=headers(), json={**body, "message": "A different request"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"]["code"] == "chat_request_payload_conflict"
    from core.clock import utcnow
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).archived_at = utcnow()
        session.commit()
    assert client.post(path, headers=headers(), json=body).status_code == 404
    with data["factory"]() as session:
        assert session.get(FounderRequest, admission.id).outcome == "unknown"
        session.get(Venture, data["vid"]).archived_at = None
        session.commit()
    for _ in range(2):
        repeated = client.post(path, headers=headers(), json=body)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == replies[0]
    assert len(attempts) == 1
    with data["factory"]() as session:
        journal = session.scalar(select(FounderRequest).where(FounderRequest.venture_id == data["vid"],
                                                            FounderRequest.source == "chat_turn_v1"))
        assert journal.outcome == "completed" and journal.resolved_at is not None
        assert session.scalar(select(func.count()).select_from(ChatMessage).where(
            ChatMessage.conversation_id == body["conversation_id"])) == 2
    after = get_account_by_id(account.id)
    assert (after.reserved_micro, after.spent_micro) == (before.reserved_micro, before.spent_micro)
    assert counts() == before_counts
    with pytest.raises(CreditError, match="requires_reconciliation"):
        release(meter.hold)


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
@pytest.mark.parametrize("backend", ["postgres", "sqlite"])
def test_provider_request_recovers_only_its_original_receipt(request, monkeypatch, idea, backend):
    if backend == "postgres":
        data, _, client, path, headers, body, api = request.getfixturevalue("chat_turn")
    else:
        data = request.getfixturevalue("chat_request")
        client, api = data["client"], data["api"]
        path = f"/ventures/{data['vid']}/chat"
        body = {"conversation_id": data["cid"], "idempotency_key": "status-nonce"}

        def headers():
            return data["headers"](path, "POST")

    from sqlalchemy import func, select
    from app.chat.store import append_message
    from app.credits import gate, metered
    from app.credits.accounts import account_for_venture, get_account_by_id
    from app.credits.errors import CreditsUnavailable
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.db.models import CreditLedgerEntry, CreditReservation, Job, Venture
    from app.db.supymem_models import FounderRequest
    from app.tenancy import bind
    from llm_gateway.errors import ProviderCallError
    from tests.unit.credits.test_core_model_receipts import DIGEST, binding, sdk

    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        migrate_wallets(session)
        session.commit()
        before_jobs = session.scalar(select(func.count()).select_from(Job))
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        account = account_for_venture(data["vid"])
        assert wallet_phase() == "person" and account.scope == "person"
    body["message"] = f"Show your saved notes about {idea}."
    capture = {**binding(), "purpose": "venture.chat.assistant"}
    calls = []
    original = gate.settle

    def unavailable(*args, **kwargs):
        raise CreditsUnavailable()

    def callback():
        calls.append(True)
        return sdk()

    async def handler(venture_id, text, *, request_id, **kwargs):
        append_message(venture_id, role="user", body_md=text)
        try:
            metered.call_sync(callback, purpose=capture["purpose"], estimate_micro=1200,
                workspace_id=data["wid"], venture_id=venture_id, account_id=account.id,
                provider=capture["provider"], model=capture["deployment"], request_id=request_id + ":assistant",
                max_tokens=200, model_binding=capture, request_digest=DIGEST, before_model_receipt=lambda *_: None)
        except CreditsUnavailable:
            raise ProviderCallError("provider_outcome_unknown", "Private settlement detail") from None
        pytest.fail("An unresolved model receipt must not produce a usable reply")

    monkeypatch.setattr(gate, "settle", unavailable)
    monkeypatch.setattr(api, "handle_message", handler)
    for _ in range(2):
        failed = client.post(path, headers=headers(), json=body)
        assert failed.status_code == 409
        assert failed.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    before = get_account_by_id(account.id)
    assert before.reserved_micro == 1200 and before.spent_micro == 0
    monkeypatch.setattr(gate, "settle", original)
    recovered = client.post(path, headers=headers(), json=body)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["kind"] == "error"
    assert any(action.get("action") == "status" for action in recovered.json()["actions"])
    assert not any(action.get("action") == "chat.model_retry" for action in recovered.json()["actions"])
    assert client.post(path, headers=headers(), json=body).json() == recovered.json()
    assert calls == [True]
    after = get_account_by_id(account.id)
    assert after.reserved_micro == 0 and after.spent_micro == 248
    with data["factory"]() as session:
        journal = session.scalar(select(FounderRequest).where(FounderRequest.venture_id == data["vid"],
                                                            FounderRequest.source == "chat_turn_v1"))
        assert journal.outcome == "completed" and journal.resolved_at is not None
        assert session.scalar(select(func.count()).select_from(CreditReservation).where(
            CreditReservation.account_id == account.id)) == 1
        assert session.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend")) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == before_jobs


def _die(vid, wid, kind, code, payload=None):
    from app.core.jobs import FatalError, enqueue, fail_job, lease
    from app.db.models import Job
    from app.state import db

    with db()() as s:
        jid = enqueue(s, kind, {"workspace_id": wid, "venture_id": vid, **(payload or {})}, workspace_id=wid,
                      venture_id=vid)
        s.commit()
        job = next(j for j in lease(s, "w1", kinds=[kind], n=20) if j.id == jid)
        fail_job(s, job.id, FatalError(code))
        s.commit()
        assert fail_job(s, job.id, FatalError(code), worker_id=job.worker_id, attempt=job.attempts) is None
        s.commit()
        assert s.get(Job, jid).status == "failed_dead"


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
@pytest.mark.parametrize("kind,msg,step", [
    ("gtm.god.run", "gtm_failed", "gtm.diagnosis"),
    ("import.analyze", "import_failed", "import.analyze"),
    ("experiment.evaluate", "experiment_failed", "experiment.evaluate"),
])
def test_remaining_job_deaths_post_through_the_helper(venture, kind, msg, step):
    from app.core.local_mode import is_local

    _, vid, wid = venture
    _die(vid, wid, kind, "some_error")
    sent = [a for k, a in _messages(vid) if k == msg]
    assert len(sent) == 1 and _alt(sent[0])["payload"]["step"] == step
    if is_local():
        assert _events(vid) == []
    else:
        assert _events(vid)[-1]["step"] == step


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_requested_terminal_failure_survives_notice_cap(venture):
    from app.chat.milestones import DAILY_MESSAGE_LIMIT, post_milestone
    from app.chat.store import append_message
    from app.core.local_mode import is_local

    _, venture_id, workspace_id = venture
    for index in range(DAILY_MESSAGE_LIMIT):
        append_message(venture_id, role="assistant", body_md=f"Your saved note {index} is available.")
    assert not post_milestone(venture_id, key="unrequested", kind="progress", body_md="Open your saved work to review it.")
    _die(venture_id, workspace_id, "gtm.god.run", "gtm_generation_failed")
    sent = [actions for kind, actions in _messages(venture_id) if kind == "gtm_failed"]
    assert len(sent) == (1 if is_local() else 0)


def test_clarification_failure_gets_retry_only_when_a_retry_can_work(venture):
    _, vid, wid = venture
    from app.chat import failed_jobs

    assert any(a.get("action") == "job.retry" for a in failed_jobs.failure_actions(vid, "idea.clarify_turn", "clarification_invalid_output"))
    assert not any(a.get("action") == "job.retry" for a in failed_jobs.failure_actions(vid, "idea.clarify_turn", "provider_down"))
    _die(vid, wid, "idea.clarify_turn", "clarification_invalid_output", {"session_id": "ids_x", "revision_id": "r"})
    kind, actions = [m for m in _messages(vid) if m[0] == "idea_failed"][-1]
    assert actions[0]["label"] == "Try again" and actions[1].get("action") == "idea.review" and _alt(actions)  # chat-first
    card = failed_jobs.prepare(vid, "idea.clarify_turn", wid)
    assert card["kind"] == "confirmation"
    assert card["actions"][0]["confirmation"]["resulting_state"]["session_id"] == "ids_x"


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_provider_error_card_offers_another_way(venture, monkeypatch):
    client, vid, _ = venture
    from app.org.characters import parse_speaker
    from app.core.local_mode import is_local
    from llm_gateway.errors import ProviderCallError
    import app.api.chat_api as api

    async def boom(*_a, **_k):
        raise ProviderCallError("provider_down", "down")

    monkeypatch.setattr(api, "handle_message", boom)
    r = client.post(f"/ventures/{vid}/chat", json={"message": "hello there", "idempotency_key": "provider-recovery"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    history = client.get(f"/ventures/{vid}/chat")
    assert history.status_code == 200
    saved = [item for item in history.json()["items"] if item.get("kind") == "error"]
    assert len(saved) == 1
    actions = saved[0]["actions"]
    assert actions[0] == {"label": "Retry", "action": "status", "payload": {}, "retry": True}
    copy = parse_speaker(saved[0]["body_md"])[1]
    if is_local():
        assert "Settings > Keys and connections > Your AI provider" in copy
        assert "Check your saved work before trying again. Nothing was retried." in copy
    else:
        assert "Retry this same message" in copy and "without starting the work again" in copy and copy.count(".") == 2
        assert "\n" not in copy and "_" not in copy
    assert _alt(actions)["payload"]["step"] == "chat.provider"
    if is_local():
        assert _events(vid) == []
    else:
        assert _events(vid)[-1]["kind"] == "provider_failed"


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_task_run_failure_message_is_logged_and_result_lands_in_work(venture):
    from app.core.local_mode import is_local

    _, vid, wid = venture
    from app.chat import recovery
    from app.db.models import PlannedTask, VentureDocument
    from app.planning.saved_work import answer_saved_work
    from app.state import db
    from core.clock import utcnow

    assert recovery.post_failure(vid, step="task.run", kind="task_failed", key="t1", code="incomplete_site_unchanged",
                                 job_id="tsk_1", body_md="I did not mark **X** done.")
    assert len([actions for kind, actions in _messages(vid) if kind == "task_failed"]) == 1
    if is_local():
        assert _events(vid) == []
    else:
        assert _events(vid)[-1]["error_code"] == "incomplete_site_unchanged"
    with db()() as s:
        s.add(PlannedTask(id="tsk_1", venture_id=vid, workspace_id=wid, title="Write the pitch", detail="d", why_now="w", status="open",
                          created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    recovery.submit(vid, {"step": "task.run", "job_id": "tsk_1", "text": "Pitch: bakeries lose orders."}, wid)
    with db()() as s:
        doc = s.get(VentureDocument, "task-note:tsk_1")
        assert doc.kind == "task_note" and json.loads(doc.provenance_json)["source"] == "founder_provided"
    recovery.submit(vid, {"step": "mac.task", "text": "Opened TextEdit and typed the note by hand"}, wid)
    assert "TextEdit" in answer_saved_work(vid, "show my saved Mac TextEdit note")


def test_own_site_counts_everywhere(venture):
    client, vid, wid = venture
    from app.chat import recovery
    from app.chat.execute import _content_drafts
    from app.ventures import next_step as ns
    from app.ventures.state_brief import _website_section, get_venture_state_brief, render_brief_for_llm

    assert recovery.external_site(vid) is None
    facts = {**ns._facts(vid), "active_jobs": False, "first_question_pending": False, "idea_confirmed": True,
             "plan_state": "approved", "products": 0, "accepted_build": False, "product_attempt": False,
             "gtm_entry": False, "paused": False, "personal": False, "exp_review": False, "exp_run": None}
    from app.planning import store

    store_tasks = store.list_current_tasks
    store.list_current_tasks = lambda _v: []
    try:
        assert ns._resolve(vid, f"/v/{vid}", facts)["key"] == "build.first"
        recovery.submit(vid, {"step": "build", "url": "https://me.carrd.co", "text": "my page"}, wid)
        assert recovery.external_site(vid) == {"url": "https://me.carrd.co", "note": "Your own site (not checked by Supymem)"}
        facts["external_site"] = True
        assert ns._resolve(vid, f"/v/{vid}", facts)["key"] == "gtm.diagnose"  # moves on to finding users
    finally:
        store.list_current_tasks = store_tasks
    site = _website_section(wid, vid)
    assert site["live"] and site["external"] and site["url"] == "https://me.carrd.co"
    dash = client.get(f"/ventures/{vid}/dashboard").json()
    assert dash["tiles"]["external_site"]["note"].startswith("Your own site")
    recovery.submit(vid, {"step": "content.draft", "text": "Hook. Point. Question?"}, wid)
    brief = get_venture_state_brief(vid, force=True)
    assert brief["content_drafts"][-1]["state"] == "founder_provided"
    assert "not checked by Supymem" in render_brief_for_llm(brief)
    assert "written by you, not posted" in _content_drafts(vid)["body_md"]
