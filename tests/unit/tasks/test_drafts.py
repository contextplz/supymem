"""Synthetic exact-reply draft receipts; no provider or send route executes."""
import asyncio
from datetime import timedelta
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import func, select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle


@pytest.fixture()
def draft_db(lifecycle, monkeypatch):
    from app.db.models import GtmContact, GtmTouch, VentureDocument
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import drafts
    from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt
    d = lifecycle
    engine = d["factory"].kw["bind"]
    for model in (GtmContact, GtmTouch, VentureDocument):
        model.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(drafts, "db", lambda: d["factory"])
    with d["factory"]() as session:
        now = d["clock"][0]
        session.add(GtmContact(id="contact_draft", workspace_id="ws_lifecycle", email="never-send@startup.example",
            created_at=now, stage_changed_at=now))
        session.add(Task(id="task_draft", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", kind="event", state="working", title="Draft a reply",
            goal="When someone replies, draft a follow-up for my approval", budget_json="{}", scope_json="{}",
            deadline_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(TaskRun(id="run_draft", task_id="task_draft", run_key="event:outreach.reply:reply_draft",
            state="working", started_at=now, deadline_at=now))
        session.flush()
        session.add(TaskStep(id="step_draft", run_id="run_draft", position=0, title="Prepare reply draft",
            skill="tasks.reply_draft", state="admitting", deadline_at=now))
        session.add(GtmTouch(id="sent_draft", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            contact_id="contact_draft", direction="out", simulated=0, provider_thread_id="thread_draft",
            sent_at=now, created_at=now))
        session.add(GtmTouch(id="reply_draft", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            contact_id="contact_draft", direction="in", channel="email", simulated=0,
            provider_thread_id="thread_draft", message_id="message_draft", classification="human_positive",
            subject="Interested in a quick demo", body_text="Could we discuss the product next Tuesday?", created_at=now))
        session.commit()
    calls, mode = [], {"value": "ok"}
    billing_recovery = install_recovery(monkeypatch)
    module = ModuleType("app.credits.metered")
    async def llm(messages, **kwargs):
        invocation = d["usage"].before_call(20, kwargs["max_tokens"])
        calls.append((messages, kwargs))
        if mode["value"] == "timeout":
            d["usage"].abort_call(invocation, definitive=False)
            raise TimeoutError("Private provider text must not escape")
        if mode["value"] in {"source_changed", "goal_changed"}:
            with d["factory"]() as session:
                if mode["value"] == "source_changed":
                    session.get(GtmTouch, "reply_draft").body_text = "The request changed while drafting."
                else:
                    session.get(Task, "task_draft").goal = "A different founder request"
                session.commit()
        body = {"subject": "Re: Interested in a quick demo", "body": "Thanks for your interest. What time on Tuesday would suit you?",
            "source_ref": json.loads(messages[1]["content"])["untrusted_reply"]["source_ref"]}
        if mode["value"] == "invented_source":
            body["source_ref"] = "gtm_touch:foreign_reply"
        result = SimpleNamespace(usage={"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)),
            finish_reason="length" if mode["value"] == "truncated" else "stop")])
        receipt = synthetic_receipt(kwargs, invocation=invocation, estimate_micro=20, actual_micro=7, prompt=4, completion=6)
        try:
            checkpoint(result, kwargs, receipt)
        finally:
            billing_recovery.confirm(receipt)
            d["usage"].after_call(invocation, 7, 10)
        return result
    module.llm = llm
    monkeypatch.setitem(sys.modules, module.__name__, module)
    d.update(drafts=drafts, calls=calls, mode=mode, billing_recovery=billing_recovery,
        keys={"task_id": "task_draft", "run_id": "run_draft", "step_id": "step_draft",
            "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"})
    return d


def _draft(d):
    return asyncio.run(d["drafts"].draft_reply(**d["keys"]))


def _verify(d):
    with d["factory"]() as session:
        return d["drafts"].verify_draft(session, **d["keys"])


def test_exact_event_reply_reaches_one_metered_request_and_private_review_artifact(draft_db):
    from app.db.models import VentureDocument
    from app.db.task_models import TaskEvent, TaskStep
    d = draft_db
    result = _draft(d)
    assert result["verified"] and result["evidence"][0]["draft_count"] == 1 and len(d["calls"]) == 1
    messages, options = d["calls"][0]
    payload = json.loads(messages[1]["content"])
    assert payload["untrusted_reply"]["body"] == "Could we discuss the product next Tuesday?"
    assert payload["untrusted_reply"]["recorded_at"] == d["clock"][0].isoformat()
    assert payload["trusted_goal"] == "When someone replies, draft a follow-up for my approval"
    assert payload["public_venture"] == {"name": "Synthetic idea"}
    assert "never-send@" not in json.dumps(payload)
    assert options["purpose"] == "task.draft" and options["tier"] == "pro" and options["max_tokens"] == 800
    assert options["response_schema"]["additionalProperties"] is False
    assert set(options["response_schema"]["required"]) == {"subject", "body", "source_ref"}
    assert options["request_id"].startswith("task-draft:")
    with d["factory"]() as session:
        row = session.get(VentureDocument, result["artifact_id"])
        assert row.kind == "task_reply_draft" and "Nothing has been sent" in row.body_md
        assert "never-send@" not in row.body_md + row.provenance_json
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == "complete"
        events = session.scalars(select(TaskEvent)).all()
        serialized = json.dumps(result) + "".join(event.message + event.data_json for event in events)
        assert "discuss the product" not in serialized and "never-send@" not in serialized
        assert "What time on Tuesday" not in serialized
    assert _verify(d)["verified"]


def test_completed_replay_recovers_artifact_without_model_or_duplicate_write(draft_db):
    from app.db.models import VentureDocument
    d = draft_db
    first = _draft(d)
    assert _draft(d) == first and len(d["calls"]) == 1
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(VentureDocument)) == 1


def test_preparation_never_calls_sender_or_creates_outbound_effect(draft_db, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    from app.email import service as email
    from app.gtm.supymem import composio_gmail
    d = draft_db
    def forbidden(*a, **kw):
        pytest.fail("Preparing a reply draft cannot call an outbound sender")
    monkeypatch.setattr(email, "send_email", forbidden)
    monkeypatch.setattr(composio_gmail, "send_gmail_email", forbidden)
    assert _draft(d)["verified"]
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(GtmTouch).where(GtmTouch.direction == "out")) == 1
        assert session.get(GtmContact, "contact_draft").times_emailed == 0


@pytest.mark.parametrize("flag", ["do_not_contact", "unsubscribed_at", "bounced_at", "negative", "automated", "simulated", "no_thread", "foreign_contact", "foreign_reply"])
def test_policy_and_source_scope_block_before_any_model_call(draft_db, flag):
    from app.db.models import GtmContact, GtmTouch
    d = draft_db
    with d["factory"]() as session:
        contact, reply = session.get(GtmContact, "contact_draft"), session.get(GtmTouch, "reply_draft")
        if flag in {"unsubscribed_at", "bounced_at"}:
            setattr(contact, flag, d["clock"][0])
        elif flag == "do_not_contact":
            contact.do_not_contact = 1
        elif flag in {"negative", "automated"}:
            reply.classification = "human_negative" if flag == "negative" else "automated"
        elif flag == "simulated":
            reply.simulated = 1
        elif flag == "no_thread":
            reply.provider_thread_id = "unconfirmed"
        elif flag == "foreign_contact":
            contact.workspace_id = "foreign"
        else:
            reply.venture_id = "foreign"
        session.commit()
    result = _draft(d)
    assert not result["verified"] and result["blocker"]["class"] == "policy" and not d["calls"]


@pytest.mark.parametrize("scope", ["workspace_id", "venture_id", "task_id", "run_id", "step_id"])
def test_foreign_task_scope_cannot_admit_paid_call(draft_db, scope):
    d = draft_db
    result = asyncio.run(d["drafts"].draft_reply(**{**d["keys"], scope: "foreign"}))
    assert not result["verified"] and result["blocker"]["class"] == "policy" and not d["calls"]


def test_secret_shaped_reply_is_withheld_before_metered_call(draft_db):
    from app.db.models import GtmTouch
    d = draft_db
    with d["factory"]() as session:
        session.get(GtmTouch, "reply_draft").body_text = "Credential: " + "sk" + "-" + "x" * 32
        session.commit()
    assert _draft(d)["blocker"]["class"] == "policy" and not d["calls"]


@pytest.mark.parametrize("limit,value", [("tokens", 799), ("cost_micro", 0)])
def test_run_budget_prevents_provider_call(draft_db, limit, value):
    from app.db.task_models import Task
    d = draft_db
    with d["factory"]() as session:
        session.get(Task, "task_draft").budget_json = json.dumps({limit: value})
        session.commit()
    result = _draft(d)
    assert not result["verified"] and result["blocker"]["class"] == "budget_exhausted" and not d["calls"]


def test_unknown_outcome_is_never_paid_or_dispatched_again(draft_db):
    from app.db.task_models import TaskStep
    d = draft_db
    d["mode"]["value"] = "timeout"
    first = _draft(d)
    assert not first["verified"] and first["blocker"]["class"] == "needs_input"
    assert not _draft(d)["verified"] and len(d["calls"]) == 1
    with d["factory"]() as session:
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == "uncertain"


def test_lost_worker_calling_checkpoint_cannot_reissue_paid_call(draft_db):
    from app.db.task_models import TaskStep
    d = draft_db
    with d["factory"]() as session:
        _, _, step, binding, _ = d["drafts"]._source(session, **d["keys"])
        d["drafts"]._journal(step, "calling", binding)
        session.commit()
    result = _draft(d)
    assert not result["verified"] and result["blocker"]["class"] == "needs_input" and not d["calls"]
    with d["factory"]() as session:
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == "calling"


def test_crash_after_response_recovers_without_another_model_call(draft_db, monkeypatch):
    from app.db.models import VentureDocument
    from app.db.task_models import TaskStep
    d = draft_db
    original = d["drafts"]._persist
    monkeypatch.setattr(d["drafts"], "_persist", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic checkpoint crash")))
    with pytest.raises(RuntimeError, match="synthetic checkpoint crash"):
        _draft(d)
    with d["factory"]() as session:
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == "responded"
        assert session.get(VentureDocument, "task-draft:run_draft") is None
    monkeypatch.setattr(d["drafts"], "_persist", original)
    assert _draft(d)["verified"] and len(d["calls"]) == 1


@pytest.mark.parametrize("mode", ["source_changed", "goal_changed", "invented_source", "truncated"])
def test_changed_inputs_or_invalid_model_output_never_become_verified_draft(draft_db, mode):
    from app.db.models import VentureDocument
    d = draft_db
    d["mode"]["value"] = mode
    assert not _draft(d)["verified"] and not _draft(d)["verified"] and len(d["calls"]) == 1
    with d["factory"]() as session:
        assert session.get(VentureDocument, "task-draft:run_draft") is None


@pytest.mark.parametrize("mutation", ["body", "body_hash", "source_digest", "source_ref"])
def test_independent_verifier_rejects_artifact_tampering(draft_db, mutation):
    from app.db.models import VentureDocument
    d = draft_db
    assert _draft(d)["verified"]
    with d["factory"]() as session:
        row = session.get(VentureDocument, "task-draft:run_draft")
        data = json.loads(row.provenance_json)
        if mutation == "body":
            row.body_md = "A different draft"
        elif mutation == "body_hash":
            data["body_hash"] = "incorrect"
        elif mutation == "source_digest":
            data["binding"]["source_digest"] = "incorrect"
        else:
            data["draft"]["source_ref"] = "gtm_touch:foreign"
        row.provenance_json = json.dumps(data)
        session.commit()
    assert not _verify(d)["verified"]
    assert not _draft(d)["verified"] and len(d["calls"]) == 1


def test_one_time_selected_reply_uses_scoped_persisted_source_reference(draft_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    with d["factory"]() as session:
        session.get(Task, "task_draft").kind = "one_time"
        session.get(TaskRun, "run_draft").run_key = "initial"
        session.get(TaskStep, "step_draft").inputs_json = json.dumps({"reply_touch_id": "reply_draft"})
        session.commit()
    assert _draft(d)["verified"] and len(d["calls"]) == 1


def test_inline_controller_completes_actual_reply_run_and_publishes_only_clean_evidence(draft_db):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    d = draft_db
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    payload = {"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}
    asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        step, run = session.get(TaskStep, "step_draft"), session.get(TaskRun, "run_draft")
        assert step.state == run.state == "completed" and session.get(Task, "task_draft").state == "submitted"
        evidence = json.loads(step.evidence_json)
        assert isinstance(evidence, list) and evidence[0]["source_ref"] == "gtm_touch:reply_draft"
        assert evidence[0]["draft_count"] == 1
        assert session.get(VentureDocument, "task-draft:run_draft") is not None
        events = session.scalars(select(TaskEvent)).all()
        serialized = json.dumps(evidence) + "".join(event.message + event.data_json for event in events)
        assert "never-send@" not in serialized and "Could we discuss" not in serialized
        assert "What time on Tuesday" not in serialized and "response" not in json.dumps(evidence)
        events_before = len(events)
    assert len(d["calls"]) == 1
    asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == events_before
    assert len(d["calls"]) == 1


def test_contact_policy_rejects_only_its_event_run_and_future_positive_reply_still_runs(draft_db):
    from app.db.models import GtmContact, GtmTouch, VentureDocument
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    with d["factory"]() as session:
        session.get(GtmTouch, "reply_draft").classification = "human_negative"
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    asyncio.run(d["execution"].execute({"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with d["factory"]() as session:
        assert session.get(TaskRun, "run_draft").state == "rejected"
        assert session.get(Task, "task_draft").state not in d["service"].TERMINAL
        assert session.get(VentureDocument, "task-draft:run_draft") is None
    assert not d["calls"]
    d["clock"][0] += timedelta(minutes=1)
    with d["factory"]() as session:
        now = d["clock"][0]
        session.add(GtmContact(id="contact_future", workspace_id="ws_lifecycle", email="future@startup.example",
            created_at=now, stage_changed_at=now))
        session.add(TaskRun(id="run_future", task_id="task_draft", run_key="event:outreach.reply:reply_future",
            state="submitted", started_at=now, deadline_at=now))
        session.flush()
        session.add(TaskStep(id="step_future", run_id="run_future", position=0, title="Draft later reply",
            skill="tasks.reply_draft", state="submitted", deadline_at=now))
        session.add(GtmTouch(id="sent_future", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            contact_id="contact_future", direction="out", simulated=0, provider_thread_id="thread_future", sent_at=now, created_at=now))
        session.add(GtmTouch(id="reply_future", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            contact_id="contact_future", direction="in", simulated=0, channel="email", provider_thread_id="thread_future",
            message_id="message_future", classification="human_positive", body_text="Could we discuss the product next Tuesday?", created_at=now))
        session.commit()
    asyncio.run(d["execution"].execute({"task_run_id": "run_future", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with d["factory"]() as session:
        assert session.get(TaskRun, "run_future").state == "completed"
        assert session.get(VentureDocument, "task-draft:run_future") is not None
    assert len(d["calls"]) == 1


def test_real_metered_settlement_overage_keeps_budget_stop_and_recovers_cached_draft(draft_db, monkeypatch):
    """Real sync wallet gate under an async shim; no LLM routing/threading or paid I/O."""
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import importlib
    from app.credits import _txn, policy
    from app.credits import gate
    from app.credits.grants import grant
    from app.credits.ledger import verify_chain
    from app.credits.reserve import Hold, settle
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation, Job, VentureDocument
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.email import triggers
    from app.tasks import budgets
    from app.tasks.contracts import Budget
    from app.tasks.locks import lock_run, lock_step
    from app.tenancy import bind
    from core.clock import as_utc

    d = draft_db
    monkeypatch.setattr(_txn, "db", lambda: d["factory"])
    engine = d["factory"].kw["bind"]
    for model in (CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation):
        model.__table__.create(engine, checkfirst=True)
    with d["factory"]() as session:
        now = d["clock"][0]
        session.add(CreditAccount(id="cra_draft_actual_meter", scope="venture", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", status="active", granted_micro=0, spent_micro=0, reserved_micro=0,
            version=0, created_at=now, updated_at=now))
        session.add(CreditPolicy(key="enforcement_enabled", value_json="true", updated_at=now, updated_by="synthetic"))
        task, _ = lock_run(session, "run_draft")
        _, _, step = lock_step(session, "step_draft")
        task.budget_json = Budget(cost_micro=1000).model_dump_json()
        step.state = "submitted"
        original_budget, original_inputs = task.budget_json, step.inputs_json
        session.commit()
    policy.invalidate()
    grant(account_id="cra_draft_actual_meter", amount_micro=1050, reason="Synthetic fixture funding",
        actor_kind="system", actor_id="synthetic", idempotency_key="synthetic-draft-funding")
    notices = []
    monkeypatch.setattr(triggers, "on_credit_balance", lambda account_id: notices.append(account_id))
    # Remove only this fixture's replacement, restoring the current S02 module.
    monkeypatch.delitem(sys.modules, "app.credits.metered")
    metered = importlib.import_module("app.credits.metered")
    assert d["billing_recovery"].original_reconcile is not None, "The real S02 recovery ABI must be present"
    monkeypatch.setattr(gate, "reconcile_receipt", d["billing_recovery"].original_reconcile)
    provider_calls = []
    async def synthetic_llm(messages, **kwargs):
        def confirmed_response():
            provider_calls.append(kwargs["request_id"])
            draft = {"subject": "Synthetic reply for review", "body": "What time would suit you for a short discussion?",
                "source_ref": json.loads(messages[1]["content"])["untrusted_reply"]["source_ref"]}
            return SimpleNamespace(usage={"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 20}},
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(draft)))])
        return metered.call_sync(confirmed_response, account_id="cra_draft_actual_meter", purpose=kwargs["purpose"],
            workspace_id=kwargs["workspace_id"], venture_id=kwargs["venture_id"], model="synthetic-confirmed-model",
            provider="synthetic", estimate_micro=1000, actual_micro=lambda _: 1100,
            max_tokens=kwargs["max_tokens"], request_id=kwargs["request_id"], before_meter_exit=kwargs["before_meter_exit"])
    monkeypatch.setattr(metered, "llm", synthetic_llm)
    payload = {"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        task, run, step = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        assert task.state == run.state == step.state == "input_required"
        assert json.loads(task.blocker_json)["class"] == "budget_exhausted"
        journal = json.loads(step.evidence_json)
        assert journal["state"] == "responded" and journal["response_digest"]
        assert budgets.eligibility(session, task, run) is None
        assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens, run.attempts) == (1100, 300, 0, 0, 1)
        assert session.get(VentureDocument, "task-draft:run_draft") is None
        account = session.get(CreditAccount, "cra_draft_actual_meter")
        assert (account.granted_micro, account.spent_micro, account.reserved_micro) == (1050, 1050, 0)
        spend = session.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend"))
        meta = json.loads(spend.meta_json)
        assert spend.amount_micro == -1050 and (spend.prompt_tokens, spend.completion_tokens) == (120, 180)
        assert meta["provider_actual_micro"] == 1100 and meta["cached_tokens"] == 20 and meta["clamped"] is True
        reservation = session.get(CreditReservation, spend.reservation_id)
        assert (reservation.status, reservation.amount_micro, reservation.settled_micro) == ("settled", 1000, 1050)
        assert reservation.task_invocation == meta["task_invocation"]
        assert (reservation.workspace_id, reservation.venture_id) == ("ws_lifecycle", "ven_lifecycle")
        hold = Hold(reservation.id, reservation.account_id, reservation.amount_micro)
        spend_id, invocation = spend.id, meta["task_invocation"]
        ledger_snapshot = [(row.id, row.amount_micro, row.prev_hash, row.entry_hash) for row in session.scalars(
            select(CreditLedgerEntry).order_by(CreditLedgerEntry.seq))]
    assert provider_calls == [journal["binding"]["request_id"]] and notices == ["cra_draft_actual_meter"]
    assert verify_chain("cra_draft_actual_meter") == (True, None)
    # Reconcile/link the actual immutable receipt and repeat both settlement
    # seams; this cannot charge again or alter already confirmed task counters.
    assert d["usage"].reconcile_usage() == 1
    assert d["usage"].reconcile_usage() == 0
    assert settle(hold, actual_micro=1100, prompt_tokens=120, completion_tokens=180, cached_tokens=20).ledger_id == spend_id
    d["usage"].after_call(invocation, 1100, 300)
    with d["factory"]() as session:
        stamp = as_utc(session.get(Task, "task_draft").updated_at).isoformat()
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        view = d["api"].resume("ven_lifecycle", "task_draft",
            d["api"].ResumeTaskBody(continue_budget=True, expected_updated_at=stamp), idempotency_key="cached-actual-overage")
        assert view["state"] == "working"
        asyncio.run(d["execution"].execute(payload))
        asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        task, run, step = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        assert task.state == "submitted" and run.state == step.state == "completed"
        assert task.budget_json == original_budget and json.loads(step.inputs_json).get("_budget_attempt") is None
        assert {key: value for key, value in json.loads(step.inputs_json).items() if not key.startswith("_admission_")} == json.loads(original_inputs)
        assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens, run.attempts) == (1100, 300, 0, 0, 1)
        assert run.wallet_account_id == "cra_draft_actual_meter" and session.get(VentureDocument, "task-draft:run_draft") is not None
        assert [(row.id, row.amount_micro, row.prev_hash, row.entry_hash) for row in session.scalars(
            select(CreditLedgerEntry).order_by(CreditLedgerEntry.seq))] == ledger_snapshot
        assert session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.kind == "call_settled")) == 1
        assert session.scalar(select(func.count()).select_from(CreditReservation)) == 1
        assert not session.scalar(select(Job.id).where(Job.kind == "tasks.verify"))
    assert len(provider_calls) == 1 and verify_chain("cra_draft_actual_meter") == (True, None)
    # Cached execution still proves the original invoice, outside row locks.
    assert _draft(d)["verified"] and len(provider_calls) == 1
    with d["factory"]() as session:
        original_provenance = session.get(VentureDocument, "task-draft:run_draft").provenance_json
    for mutation in ("missing", "hash", "forged_zero_cost", "forged_zero_tokens"):
        from app.tasks import billing
        data = json.loads(original_provenance)
        with d["factory"]() as session:
            if mutation == "missing":
                data.pop("billing")
            elif mutation == "hash":
                data["billing"]["sha256"] = "tampered"
            else:
                captured_usage = data["billing"]["usage"]
                if mutation == "forged_zero_cost":
                    captured_usage["actual_micro"] = captured_usage["provider_actual_micro"] = 0
                else:
                    for field in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"):
                        captured_usage[field] = 0
                # A recomputed structural checksum is not an original settlement.
                data["billing"] = billing.capture(captured_usage, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                    purpose="task.draft", request_id=data["binding"]["request_id"], run_id="run_draft", step_id="step_draft", session=session)
            session.get(VentureDocument, "task-draft:run_draft").provenance_json = json.dumps(data)
            session.commit()
        with d["factory"]() as reader:
            assert not d["drafts"].verify_draft(reader, **d["keys"])["verified"]
            assert not reader.dirty and not reader.new
        assert not _draft(d)["verified"] and len(provider_calls) == 1
        with d["factory"]() as reader:
            assert [(row.id, row.amount_micro, row.prev_hash, row.entry_hash) for row in reader.scalars(
                select(CreditLedgerEntry).order_by(CreditLedgerEntry.seq))] == ledger_snapshot
            run = reader.get(TaskRun, "run_draft")
            assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (1100, 300, 0, 0)
        assert verify_chain("cra_draft_actual_meter") == (True, None)
