"""Offline SDK transport; real modern billing, wallet, Task and draft adapters.

These tests explicitly skip until the owned S06/S02 receipt ABI is present.
Passing in a private DEV graph does not qualify accepted source or live I/O.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
GOAL = "When someone replies, draft a follow-up for my approval"


@pytest.fixture()
def modern_runtime(tmp_path, monkeypatch):
    for key, value in {
        "COMPANY_OS_DB_URL": f"sqlite:///{tmp_path}/modern.db",
        "COMPANY_OS_DATA_DIR": str(tmp_path),
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "COMPANY_OS_EPHEMERAL_VAULT": "1", "APP_ENV": "local",
        "LLM_OFFLINE": "0", "LLM_PROVIDER": "openai",
        "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_API_KEY": "OFFLINE_TEST_ONLY",
        "LLM_DEPLOYMENT_PRO": "gpt-6.1-sol", "LLM_DEPLOYMENT_FLASH": "gpt-6.1-sol",
        "MODEL_ROUTING_OVERRIDES": "{}", "LLM_CREDITS_REQUIRE_GATE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CONNECTOR_VAULT_URL", raising=False)
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    load_company_os_app()
    from app import state
    from app.connectors.secrets import reset_store
    from app.credits import gate, metered, policy, reserve
    from app.credits.grants import grant
    from app.db.append_only import _install_sqlite
    from app.db import models
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import billing, drafts, usage
    from llm_gateway import routing, sole
    from core.clock import set_clock

    if not all(callable(fn) for fn in (
        getattr(reserve, "model_receipt_metadata", None), getattr(reserve, "validate_call_binding", None),
        getattr(routing, "resolve_purpose_config", None), getattr(sole, "transport_prompt_tokens", None),
        getattr(metered, "_routing_binding", None),
    )):
        pytest.skip("Requires accepted S06 captured-route/helper and S02 modern receipt APIs; no fake finance fallback")

    source_root = Path(__file__).resolve().parents[3]
    for module, relative in (
        (billing, "services/company_os/app/tasks/billing.py"), (usage, "services/company_os/app/tasks/usage.py"),
        (gate, "services/company_os/app/credits/gate.py"), (reserve, "services/company_os/app/credits/reserve.py"),
        (metered, "services/company_os/app/credits/metered.py"), (sole, "packages/llm_gateway/sole.py"),
    ):
        assert Path(module.__file__).resolve() == source_root / relative
    for function, owner in ((gate.charge, gate), (gate.reconcile_receipt, gate), (reserve.reserve, reserve),
        (reserve.settle, reserve), (metered.llm, metered), (usage.before_call, usage), (usage.after_call, usage)):
        assert function.__module__ == owner.__name__, "Actual owned implementation is required"
    monkeypatch.setenv("MODEL_ROUTING_PATH", str(source_root / "config/model_routing.yaml"))
    routing.clear_routing_cache()

    from openai import AsyncOpenAI, _base_client
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    engine = create_engine(f"sqlite:///{tmp_path}/modern.db", connect_args={"check_same_thread": False})
    rows = [getattr(models, name) for name in (
        "Person", "Workspace", "WorkspaceMember", "Venture", "Job", "GtmContact", "GtmTouch",
        "VentureDocument", "CreditAccount", "CreditLedgerEntry", "CreditReservation", "CreditPolicy",
        "FounderLLMSetting", "BoundaryLedgerEntry", "EventLog", "TokenLedger",
    )] + [Notice, Task, TaskEvent, TaskRun, TaskStep]
    models.Base.metadata.create_all(engine, tables=[row.__table__ for row in rows])
    with engine.begin() as connection:
        _install_sqlite(connection, "credit_ledger")
        _install_sqlite(connection, "task_events")
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    previous_factory = state.get_factory()
    state.set_factory(factory)
    set_clock(lambda: NOW)
    reset_store()
    policy.invalidate()
    try:
        with factory() as session:
            moment = NOW.replace(tzinfo=None)
            session.add(models.Person(id="usr_modern", display_name="Fictional founder", created_at=moment))
            session.flush()
            session.add(models.Workspace(id="ws_modern", owner_id="usr_modern", kind="company", entry_mode="idea",
                name="Fictional workspace", created_at=moment, updated_at=moment))
            session.flush()
            session.add(models.Venture(id="ven_modern", workspace_id="ws_modern", name="Fictional demo",
                raw_idea="Fictional fixture only", stage="draft", created_at=moment, updated_at=moment))
            session.add(models.CreditAccount(id="acct_modern", scope="person", owner_person_id="usr_modern",
                created_at=moment, updated_at=moment))
            session.add(Task(id="task_modern", workspace_id="ws_modern", venture_id="ven_modern",
                owner_person_id="usr_modern", kind="event", state="working", title="Draft a reply", goal=GOAL,
                budget_json="{}", deadline_at=moment + timedelta(minutes=20), created_at=moment, updated_at=moment))
            session.flush()
            session.add(TaskRun(id="run_modern", task_id="task_modern", run_key="event:outreach.reply:reply_modern",
                state="working", started_at=moment, deadline_at=moment + timedelta(minutes=20)))
            session.flush()
            session.add(TaskStep(id="step_modern", run_id="run_modern", position=0, title="Draft the exact reply",
                skill="tasks.reply_draft", state="admitting", deadline_at=moment + timedelta(minutes=20)))
            session.add(models.GtmContact(id="contact_modern", workspace_id="ws_modern", email="fictional@example.invalid",
                created_at=moment, stage_changed_at=moment))
            session.add(models.GtmTouch(id="sent_modern", workspace_id="ws_modern", venture_id="ven_modern",
                contact_id="contact_modern", direction="out", simulated=0, provider_thread_id="thread_modern",
                sent_at=moment, created_at=moment))
            session.add(models.GtmTouch(id="reply_modern", workspace_id="ws_modern", venture_id="ven_modern",
                contact_id="contact_modern", direction="in", channel="email", simulated=0,
                provider_thread_id="thread_modern", message_id="message_modern", classification="human_positive",
                subject="A short demo", body_text="Could we discuss the fictional product?", created_at=moment))
            session.commit()
        grant(account_id="acct_modern", amount_micro=1_000_000, reason="Fictional offline test funding",
            actor_kind="system", actor_id="synthetic", idempotency_key="modern-coupling-funding")
        d = SimpleNamespace(factory=factory, engine=engine, billing=billing, drafts=drafts, usage=usage,
            gate=gate, requests=[], clients=[], admissions=[], response_model="gpt-6.1-sol",
            output_error=None, prompt=80, completion=40, cached=20,
            keys=dict(task_id="task_modern", run_id="run_modern", step_id="step_modern",
                workspace_id="ws_modern", venture_id="ven_modern"))

        async def respond(request):
            wire = json.loads(request.content)
            with factory() as session:
                events = session.scalars(select(TaskEvent).where(TaskEvent.kind == "call_reserved")).all()
                assert len(events) == 1
                admission = json.loads(events[0].data_json)
                route = admission["call_binding"]
                assert (events[0].task_id, events[0].run_id, events[0].step_id) == ("task_modern", "run_modern", "step_modern")
                assert route == {"schema_version": 1, "purpose": "task.draft", "provider": "openai",
                    "model": "gpt-6.1-sol", "catalog_model": "gpt-6.1-sol", "request_id": route["request_id"],
                    "captured_tier": "pro", "api_mode": "responses", "max_output_tokens": 800, "reasoning_effort": "low"}
                assert len(session.scalars(select(models.BoundaryLedgerEntry)).all()) == 1
                assert json.loads(session.get(TaskStep, "step_modern").evidence_json)["state"] == "calling"
                holds = session.scalars(select(models.CreditReservation)).all()
                if holds:
                    assert len(holds) == 1 and holds[0].status == "reconciling" and holds[0].settled_micro is None
                    assert holds[0].task_invocation == admission["invocation"]
                d.admissions.append(deepcopy(admission))
            assert str(request.url).endswith("/responses") and wire["model"] == "gpt-6.1-sol"
            assert wire["max_output_tokens"] == 800 and wire["reasoning"]["effort"] == "low"
            text = json.dumps(wire)
            assert GOAL in text and "Could we discuss the fictional product?" in text and "fictional@example.invalid" not in text
            d.requests.append(wire)
            content = [{"type": "output_text", "text": json.dumps({"subject": "Re: A short demo",
                "body": "What time would suit you for a short discussion?", "source_ref": "gtm_touch:reply_modern"}), "annotations": []}]
            if d.output_error == "refused":
                content = [{"type": "refusal", "refusal": "Fictional provider refusal"}]
            body = {"id": "resp_offline", "object": "response", "created_at": 1, "model": d.response_model,
                "status": "completed", "output": [{"id": "msg_offline", "type": "message", "role": "assistant",
                    "status": "completed", "content": content}], "usage": {"input_tokens": d.prompt,
                    "output_tokens": d.completion, "total_tokens": d.prompt + d.completion,
                    "input_tokens_details": {"cached_tokens": d.cached}, "output_tokens_details": {"reasoning_tokens": 10}}}
            if d.output_error in {"incomplete", "filtered"}:
                body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens" if d.output_error == "incomplete" else "content_filter"})
            return httpx.Response(200, json=body)

        def client(config):
            sdk = AsyncOpenAI(api_key=config.api_key.get_secret_value(), base_url=config.base_url,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False))
            d.clients.append(sdk)
            return sdk, config
        monkeypatch.setattr(sole, "create_async_client_from_config", client)
        yield d
    finally:
        set_clock(None)
        reset_store()
        policy.invalidate()
        state.set_factory(previous_factory)
        engine.dispose()


def _draft(d):
    from app.tenancy import bind
    from app.tasks import service
    from app.tasks.locks import lock_step
    with bind(workspace_id="ws_modern", venture_id="ven_modern", actor="usr_modern"):
        result = asyncio.run(d.drafts.draft_reply(**d.keys))
        # The actual inline handler owns application of an adapter blocker.
        if not result["verified"] and result.get("blocker"):
            with d.factory() as session:
                task, run, step = lock_step(session, d.keys["step_id"])
                service.block(session, task, result["blocker"], run=run, step=step)
                session.commit()
        return result


def _saved(d):
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, VentureDocument
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    with d.factory() as session:
        return SimpleNamespace(task=session.get(Task, "task_modern"), run=session.get(TaskRun, "run_modern"),
            step=session.get(TaskStep, "step_modern"), journal=json.loads(session.get(TaskStep, "step_modern").evidence_json),
            account=session.get(CreditAccount, "acct_modern"), holds=session.scalars(select(CreditReservation)).all(),
            ledger=[(row.id, row.kind, row.amount_micro, row.reservation_id, row.meta_json, row.prev_hash, row.entry_hash)
                for row in session.scalars(select(CreditLedgerEntry).order_by(CreditLedgerEntry.seq))],
            settled=[json.loads(event.data_json) for event in session.scalars(select(TaskEvent).where(TaskEvent.kind == "call_settled"))],
            artifacts=session.scalars(select(VentureDocument)).all())


def _scope(d, checkpoint):
    return dict(workspace_id="ws_modern", venture_id="ven_modern", purpose="task.draft",
        request_id=checkpoint["usage"]["request_id"], run_id=d.keys["run_id"], step_id=d.keys["step_id"])


def _billing_saved(d):
    """Original private receipt moves to the exact artifact after completion."""
    from app.db.models import VentureDocument
    from app.db.task_models import TaskStep
    with d.factory() as session:
        journal = json.loads(session.get(TaskStep, d.keys["step_id"]).evidence_json)
        if isinstance(journal, dict) and "billing" in journal:
            return journal["billing"]
        artifact = session.get(VentureDocument, "task-draft:" + d.keys["run_id"])
        assert artifact and (artifact.kind, artifact.venture_id) == (d.drafts.KIND, d.keys["venture_id"])
        provenance = json.loads(artifact.provenance_json)
        assert all(provenance["binding"][key] == value for key, value in d.keys.items())
        return provenance["billing"]


def _reconcile_saved(d):
    checkpoint = _billing_saved(d)  # Reread durable private state in a separate transaction.
    return d.billing.reconcile(checkpoint, **_scope(d, checkpoint))


def _failure(d, boundary, enabled=True):
    table, condition = ("credit_ledger", "NEW.kind = 'spend'") if boundary == "wallet" else ("task_events", "NEW.kind = 'call_settled'")
    with d.engine.begin() as connection:
        if enabled:
            connection.exec_driver_sql(f"CREATE TRIGGER modern_storage_failure BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, 'fictional storage outage'); END")
        else:
            connection.exec_driver_sql("DROP TRIGGER modern_storage_failure")


def _assert_settled(d, raw, *, platform=True):
    from app.credits.ledger import verify_chain
    row = _saved(d)
    assert (row.run.cost_micro, row.run.tokens_used, row.run.reserved_cost_micro, row.run.reserved_tokens) == (raw, d.prompt + d.completion, 0, 0)
    assert row.account.reserved_micro == 0 and len(row.settled) == 1
    assert row.settled[0]["cost_micro"] == raw and row.settled[0]["tokens"] == d.prompt + d.completion
    spends = [entry for entry in row.ledger if entry[1] == "spend"]
    assert len(spends) == 1 and spends[0][2] == -row.account.spent_micro
    assert json.loads(spends[0][4])["cached_tokens"] == d.cached
    assert len(row.holds) == (1 if platform else 0)
    if platform:
        assert row.holds[0].status == "settled" and row.holds[0].settled_micro == row.account.spent_micro
    else:
        assert row.account.spent_micro == 0 and spends[0][3] is None
    assert len(d.requests) == 1 and all(client.is_closed() for client in d.clients)
    assert verify_chain("acct_modern") == (True, None)
    return row


def _actual(d):
    from app.credits.estimate import actual_llm_micro
    return actual_llm_micro(d.response_model, prompt_tokens=d.prompt, completion_tokens=d.completion, cached_tokens=d.cached)


def test_actual_async_modern_draft_persists_source_bound_checkpoint_and_replays_once(modern_runtime):
    d = modern_runtime
    first = _draft(d)
    assert first["verified"]
    row = _assert_settled(d, _actual(d))
    assert row.task.goal == GOAL and len(row.artifacts) == 1 and row.journal["state"] == "complete"
    checkpoint = _billing_saved(d)
    assert checkpoint["usage"]["call_binding"] == checkpoint["binding"]["call_binding"] == d.admissions[0]["call_binding"]
    assert checkpoint["binding"]["reserved_event_id"] and checkpoint["usage"]["total_tokens"] == 120
    assert _reconcile_saved(d) == _actual(d) and _reconcile_saved(d) == _actual(d)
    assert _draft(d) == first and _saved(d).ledger == row.ledger
    assert len(_saved(d).artifacts) == 1 and len(_saved(d).settled) == len(d.requests) == 1


@pytest.mark.parametrize("boundary", ["wallet", "task_counter"])
def test_persisted_response_survives_actual_storage_failure_and_recovers_original_receipt(modern_runtime, boundary):
    d = modern_runtime
    _failure(d, boundary)
    first = _draft(d)
    before = _saved(d)
    assert not first["verified"] and before.journal["state"] == "responded" and before.journal["response_digest"]
    assert before.journal["billing"] and not before.artifacts and len(d.requests) == 1
    assert before.run.reserved_cost_micro > 0 and before.run.reserved_tokens > 0 and not before.settled
    assert len(before.holds) == 1 and before.holds[0].status == ("reconciling" if boundary == "wallet" else "settled")
    assert before.account.spent_micro == (0 if boundary == "wallet" else _actual(d))
    assert not _draft(d)["verified"] and _saved(d).journal == before.journal and len(d.requests) == 1
    _failure(d, boundary, False)
    assert _reconcile_saved(d) == _actual(d)
    after = _assert_settled(d, _actual(d))
    assert after.holds[0].id == before.holds[0].id and after.journal == before.journal
    assert after.task.state == before.task.state == "input_required" and not after.artifacts
    assert not _draft(d)["verified"] and _saved(d).ledger == after.ledger and len(d.requests) == 1


@pytest.mark.parametrize("error", ["model_mismatch", "incomplete", "refused", "filtered"])
def test_known_paid_output_error_settles_before_refusing_any_draft_artifact(modern_runtime, error):
    d = modern_runtime
    if error == "model_mismatch":
        d.response_model = "gpt-6-luna"  # A recognized different price, never a changed admitted route.
    else:
        d.output_error = error
    assert not _draft(d)["verified"]
    row = _assert_settled(d, _actual(d))
    assert not row.artifacts and row.task.goal == GOAL
    spend = next(entry for entry in row.ledger if entry[1] == "spend")
    # The owned Responses adapter labels a wire refusal as content_filter.
    assert json.loads(spend[4])["output_error"] == ("filtered" if error == "refused" else error)
    if error == "model_mismatch":
        assert row.journal["state"] == "responded" and row.journal["billing"]["usage"]["output_error"] == error
        with pytest.raises(d.gate.CreditsUnavailable):
            _reconcile_saved(d)  # Confirms money first, then refuses result applicability.
        assert _saved(d).ledger == row.ledger
    assert not _draft(d)["verified"] and len(d.requests) == 1 and _saved(d).ledger == row.ledger


def test_actual_byok_modern_draft_has_no_hold_but_counts_raw_cost_and_full_tokens(modern_runtime):
    from app.llm.founder_provider import set_founder_config
    d = modern_runtime
    set_founder_config(workspace_id="ws_modern", provider="openai", model="gpt-6.1-sol",
        base_url="https://api.openai.com/v1", api_key="OFFLINE_BYOK_TEST_ONLY")
    first = _draft(d)
    assert first["verified"]
    row = _assert_settled(d, _actual(d), platform=False)
    assert _billing_saved(d)["usage"]["source"] == "byok"
    assert _reconcile_saved(d) == 0 and _draft(d) == first and _saved(d).ledger == row.ledger


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive"])
def test_retired_custody_allows_original_funded_cleanup_but_no_goal_application(modern_runtime, change):
    from app.db.models import Person, Venture, Workspace
    d = modern_runtime
    _failure(d, "wallet")
    assert not _draft(d)["verified"]
    before = _saved(d)
    with d.factory() as session:
        if change == "owner":
            session.add(Person(id="usr_retired", display_name="Fictional successor", created_at=NOW))
            session.get(Workspace, "ws_modern").owner_id = "usr_retired"
        else:
            session.get(Workspace if change == "workspace_archive" else Venture,
                "ws_modern" if change == "workspace_archive" else "ven_modern").archived_at = NOW
        session.commit()
    _failure(d, "wallet", False)
    assert _reconcile_saved(d) == _actual(d)
    row = _assert_settled(d, _actual(d))
    assert row.holds[0].id == before.holds[0].id and row.task.goal == GOAL and row.journal == before.journal
    assert row.task.state == "input_required" and not row.artifacts
    assert not _draft(d)["verified"] and len(d.requests) == 1 and _saved(d).ledger == row.ledger


@pytest.mark.parametrize("change", ["goal", "reply"])
def test_paid_saved_draft_cannot_apply_to_a_changed_goal_or_source(modern_runtime, change):
    from app.db.models import GtmTouch
    from app.db.task_models import Task
    d = modern_runtime
    _failure(d, "wallet")
    assert not _draft(d)["verified"]
    original = _saved(d).journal
    with d.factory() as session:
        if change == "goal":
            session.get(Task, "task_modern").goal = "A different fictional founder goal"
        else:
            session.get(GtmTouch, "reply_modern").body_text = "The fictional reply changed after the call."
        session.commit()
    _failure(d, "wallet", False)
    assert _reconcile_saved(d) == _actual(d)
    assert not _draft(d)["verified"]
    row = _assert_settled(d, _actual(d))
    assert not row.artifacts and row.journal == original
    assert row.task.goal == (GOAL if change == "reply" else "A different fictional founder goal")


def test_actual_raw_overage_preserves_budget_stop_and_cached_response_without_new_call(modern_runtime):
    from app.db.task_models import Task
    d = modern_runtime
    # Admission fits the real estimate; anomalous confirmed output exceeds it.
    with d.factory() as session:
        session.get(Task, "task_modern").budget_json = json.dumps({"cost_micro": 50_000})
        session.commit()
    d.completion = 20_000
    assert not _draft(d)["verified"]
    row = _assert_settled(d, _actual(d))
    assert _actual(d) > 50_000 and row.run.cost_micro > row.account.spent_micro
    assert row.account.spent_micro <= row.holds[0].amount_micro + min(row.holds[0].amount_micro // 20, 10_000)
    assert row.task.state == row.run.state == "input_required" and json.loads(row.task.blocker_json)["class"] == "budget_exhausted"
    assert row.journal["state"] == "responded" and not row.artifacts
    assert _reconcile_saved(d) == row.account.spent_micro
    assert not _draft(d)["verified"] and _saved(d).ledger == row.ledger and len(d.requests) == 1
    assert json.loads(_saved(d).task.blocker_json)["class"] == "budget_exhausted"


@pytest.mark.parametrize("field,value", [("reasoning_effort", "high"), ("captured_tier", "flash"),
    ("max_output_tokens", 799), ("catalog_model", "gpt-6-luna")])
def test_resealed_modern_checkpoint_cannot_substitute_original_task_admission(modern_runtime, field, value):
    d = modern_runtime
    assert _draft(d)["verified"]
    before = _saved(d)
    checkpoint = deepcopy(_billing_saved(d))
    for part in ("usage", "binding"):
        checkpoint[part]["call_binding"][field] = value
        if field == "catalog_model":
            checkpoint[part][field] = value
    content = {name: value for name, value in checkpoint.items() if name != "sha256"}
    checkpoint["sha256"] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(d.gate.CreditsUnavailable):
        d.billing.reconcile(checkpoint, **_scope(d, checkpoint))
    assert _saved(d).ledger == before.ledger and _saved(d).settled == before.settled and len(d.requests) == 1
