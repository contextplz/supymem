"""Disposable SQLite source/lease custody with mocked provider and financial receipts."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.unit.memory.test_facts import facts as facts


@pytest.fixture
def extraction(facts, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import app.state
    from app.chat import conversations
    from app.core.jobs import FatalError
    from app.db.models import Base, Job, VentureDocument
    from app.memory import model_extract as api
    from app.config import settings
    monkeypatch.setattr(settings, "company_os_memory_model_extract_enabled", True)
    from app.research import metered
    from app.credits import gate
    from llm_gateway import sole
    d = facts
    real_reconcile, real_perform, real_capture = gate.reconcile_receipt, metered.perform, api.capture_route
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setattr(app.state, "db", lambda: d.factory)
    monkeypatch.setattr(api, "db", lambda: d.factory)
    monkeypatch.setattr(conversations, "db", lambda: d.factory)
    Base.metadata.create_all(d.factory.kw["bind"], tables=[Base.metadata.tables[name] for name in
                                                        ("jobs", "venture_documents")])
    text = "Our audience is independent bookshops. Our pricing is ten dollars."
    d.message("msg_source", text)
    payload = {"workspace_id": "wsp_owned", "venture_id": "ven_owned", "source_message_id": "msg_source",
               "_actor_subject": "usr_alice", "_job_id": "job_extract", "_worker_id": "worker_one", "_job_attempt": 1,
               "_chat_conversation": {"schema_version": 1, "conversation_id": "cnv_owned", "venture_id": "ven_owned",
                                      "workspace_id": "wsp_owned", "actor_id": "usr_alice"}}
    with d.factory() as session:
        session.add(Job(id=payload["_job_id"], kind="memory.extract", workspace_id="wsp_owned", venture_id="ven_owned",
                        payload_json=json.dumps({key: value for key, value in payload.items() if not key.startswith("_job") and key != "_worker_id"}),
                        status="leased", locked_by="worker_one", attempts=1, created_at=d.when))
        session.commit()
    config = SimpleNamespace(captured_tier="flash")
    route = {"provider": "openai", "model": "gpt-6.1-sol", "catalog_model": "gpt-6.1-sol",
             "api_mode": "responses", "reasoning_effort": "low", "tier": "flash",
             "endpoint_digest": "host-digest", "request_digest": "request-digest",
             "prompt_tokens": 200, "max_tokens": 2048, "estimate_micro": 10}
    monkeypatch.setattr(api, "capture_route", lambda workspace_id, text:
                        (config, [{"role": "user", "content": text}], api.FactExtraction.model_json_schema(), route))
    calls, settlements = [], []
    state = {"facts": [{"subject": "idea.audience", "quote": "independent bookshops"}], "after_response": None,
             "settlement_error": False}

    def settle(usage, binding=None):
        if state["settlement_error"]:
            raise RuntimeError("mock receipt temporarily unavailable")
        if isinstance(usage, dict):
            assert usage["workspace_id"] == "wsp_owned" and usage["venture_id"] == "ven_owned"
            assert usage["purpose"] == "memory.extract" and usage["source"] == "platform"
            assert usage["provider_actual_micro"] == 1 and usage["total_tokens"] == 102
            assert usage["task_invocation"] is None
            if binding is not None:
                assert binding["request_id"] == usage["request_id"]
        settlements.append(usage)
        return 1

    monkeypatch.setattr(gate, "reconcile_receipt", settle, raising=False)

    async def transmit(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs["config"] is config and kwargs["tier"] == "flash"
        assert kwargs["use_workspace_override"] is False and kwargs["single_attempt"] is True
        assert kwargs["purpose"] == "memory.extract" and kwargs["max_tokens"] == 2048
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"facts": state["facts"]})),
                                                       finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2, total_tokens=102,
                                                     prompt_tokens_details=SimpleNamespace(cached_tokens=0)))
    monkeypatch.setattr(sole, "transmit", transmit)

    def perform(did, key, payload, **kwargs):
        assert kwargs["byok"] is False and kwargs["cap_at_reservation"] is True and kwargs["require_stop"] is True
        assert kwargs["token_ceiling"] == 2248 and kwargs["model"] == "gpt-6.1-sol"
        journal = kwargs["journal"]
        token = journal.prepare(did, key, payload)
        usage = {"provider": "openai", "model": "gpt-6.1-sol", "source": "platform",
                 "workspace_id": "wsp_owned", "venture_id": "ven_owned", "purpose": "memory.extract",
                 "request_id": f"{did}:{key}:{payload['_job_attempt']}", "task_invocation": None,
                 "account_id": "wallet_alice", "reservation_id": "hold_extract", "estimate_micro": kwargs["estimate"],
                 "provider_identity": kwargs["provider_identity"]}
        journal.begin(did, key, payload, usage, token)
        result = kwargs["callback"]()
        usage.update(actual_micro=1, provider_actual_micro=1, prompt_tokens=100,
                     completion_tokens=2, cached_tokens=0, total_tokens=102)
        response = kwargs["parse"](result.choices[0].message.content)
        journal.save_operation(did, key, {"state": "responded", "response": response, "usage": usage})
        if state["after_response"]:
            state["after_response"]()
        return journal.recover(did, key, payload)
    monkeypatch.setattr(metered, "perform", perform)
    @contextmanager
    def bound():
        with d.bind(), conversations.bind_job_conversation(payload, venture_id="ven_owned", workspace_id="wsp_owned"):
            yield
    return SimpleNamespace(d=d, api=api, payload=payload, calls=calls, settlements=settlements, state=state, bound=bound,
                           Job=Job, Document=VentureDocument, Fatal=FatalError, route=route,
                           real_reconcile=real_reconcile, real_perform=real_perform,
                           real_capture=real_capture, config=config)


def run(d):
    with d.bound():
        return asyncio.run(d.api.run(d.payload))


@pytest.mark.parametrize("statement", ["Our audience is warehouse operators.", "Our audience is first-time parents."])
def test_capture_decisions_reuse_settled_cheapest_tier_extractor_and_replay_without_payment(extraction, monkeypatch, statement):
    from app.core.ratelimit import limiter
    from core.ratelimit import identity_key
    from app.credits import policy
    from app.memory import extraction as capture
    d = extraction
    admissions = []
    monkeypatch.setattr(limiter, "check_ai", admissions.append)
    monkeypatch.setattr(policy, "get_policy", lambda *args: {"memory_capture_v1": True})
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = statement
        session.commit()
    d.state["facts"] = [{"subject": "idea.target_users", "quote": statement, "decision": "add"}]
    monkeypatch.setattr(capture, "decision_route", lambda workspace, text, existing:
        (d.config, [{"role": "user", "content": text}], capture.FactDecisions.model_json_schema(), d.route))
    with d.bound():
        ids = capture.capture_model_decisions(d.payload)
        assert capture.capture_model_decisions(d.payload) == ids
        assert len(ids) == 1
        rows = d.d.read()
        assert rows[0]["value"] == statement and rows[0]["subject"] == "idea.audience"
    assert len(d.calls) == 1
    assert {(usage["request_id"], usage["reservation_id"]) for usage in d.settlements} == {
        (f"{d.api.identifier(d.payload['_job_id'])}:extract:1", "hold_extract")}
    assert d.calls[0]["tier"] == "flash" and d.calls[0]["response_schema"] == capture.FactDecisions.model_json_schema()
    assert admissions == ["memory-founder:" + identity_key(d.api._json(["wsp_owned", "ven_owned", "usr_alice"]))]


@pytest.mark.parametrize("audience", ["warehouse operators", "first-time parents"])
def test_capture_worker_never_overrides_a_metered_ignore_with_regex_learning(extraction, monkeypatch, audience):
    from app.core.ratelimit import limiter
    from app.credits import policy
    from app.memory import extraction as capture
    d = extraction
    monkeypatch.setattr(policy, "get_policy", lambda *args: {"memory_capture_v1": True})
    monkeypatch.setattr(limiter, "check_ai", lambda key: None)
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = f"Our audience is {audience}."
        session.commit()
    d.state["facts"] = [{"subject": "idea.audience", "quote": audience, "decision": "ignore"}]
    monkeypatch.setattr(capture, "decision_route", lambda workspace, text, existing:
        (d.config, [{"role": "user", "content": text}], capture.FactDecisions.model_json_schema(), d.route))
    with d.bound():
        result = asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
        assert len(result["fact_ids"]) == 1 and d.d.read() == []
    assert len(d.calls) == 1


@pytest.mark.parametrize("statement", ["Our audience is warehouse operators.", "Our audience is first-time parents."])
@pytest.mark.parametrize("route_error", ["memory_extract_byok_unavailable", "memory_extract_sol_unavailable"])
def test_capture_worker_preserves_private_deterministic_learning_without_bypassing_platform_route(extraction, monkeypatch, statement, route_error):
    from app.credits import policy
    from app.memory import extraction as capture
    d = extraction
    monkeypatch.setattr(policy, "get_policy", lambda *args: {"memory_capture_v1": True})
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = statement
        session.commit()
    def restricted(*args):
        raise d.Fatal(route_error)
    monkeypatch.setattr(capture, "decision_route", restricted)
    with d.bound():
        result = asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
        assert len(result["fact_ids"]) == 1 and d.d.read()[0]["value"] == statement
    assert d.calls == [] and d.settlements == []


@pytest.mark.parametrize("statement", ["Our audience is warehouse operators.", "Our audience is first-time parents."])
def test_capture_decisions_retry_founder_admission_without_freezing_or_repaying(extraction, monkeypatch, statement):
    from app.core.ratelimit import RateLimitedJob, limiter
    from app.credits import policy
    from app.memory import extraction as capture
    from fastapi import HTTPException
    d = extraction
    monkeypatch.setattr(policy, "get_policy", lambda *args: {"memory_capture_v1": True})
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = statement
        session.commit()
    d.state["facts"] = [{"subject": "idea.audience", "quote": statement, "decision": "add"}]
    def restricted(key):
        raise HTTPException(429, headers={"Retry-After": "7"})
    monkeypatch.setattr(limiter, "check_ai", restricted)
    monkeypatch.setattr(capture, "decision_route", lambda workspace, text, existing:
        (d.config, [{"role": "user", "content": text}], capture.FactDecisions.model_json_schema(),
         {**d.route, "request_digest": "with-existing" if existing else "without-existing"}))
    with d.bound():
        with pytest.raises(RateLimitedJob) as error:
            asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
        assert error.value.retry_after_s == 7 and d.calls == []
        assert "route" not in d.api.read(d.api.identifier(d.payload["_job_id"])).document["_runtime"]
        monkeypatch.setattr(limiter, "check_ai", lambda key: None)
        result = asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
        assert len(result["fact_ids"]) == 1 and len(d.calls) == 1


@pytest.mark.parametrize("audience", ["warehouse operators", "first-time parents"])
def test_decision_schema_never_promotes_quoted_external_data_or_unproven_retirement(extraction, audience):
    from app.memory.extraction import validate_decisions
    quote = f"Our audience is {audience}."
    for body in ("> " + quote, "Website:\n" + quote, "Email:\n" + quote, "Document:\n" + quote,
                 "Website:\n\n" + quote, "Email:\n\n" + quote, "Document:\n\n" + quote):
        with pytest.raises(ValueError, match="quote_unproven"):
            validate_decisions(json.dumps({"facts": [{"subject": "idea.audience", "quote": quote, "decision": "add"}]}), body)
    with pytest.raises(ValueError, match="retirement_unproven"):
        validate_decisions(json.dumps({"facts": [{"subject": "idea.audience", "quote": quote, "decision": "retire"}]}), quote)


@pytest.mark.parametrize("statement", ["Our audience is warehouse operators.", "Our audience is first-time parents."])
def test_capture_worker_runs_source_validation_and_capture_off_the_event_loop(extraction, monkeypatch, statement):
    import threading
    from app.credits import policy
    from app.memory import extraction as capture
    d = extraction
    monkeypatch.setattr(policy, "get_policy", lambda *args: {"memory_capture_v1": True})
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = statement
        session.commit()
    caller = threading.get_ident()
    threads = []
    original = capture.capture_chat_message
    def record(source):
        threads.append(threading.get_ident())
        return original(source)
    monkeypatch.setattr(capture, "capture_chat_message", record)
    monkeypatch.setattr(capture, "capture_model_decisions", lambda payload: [])
    with d.bound():
        result = asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
    assert len(result["fact_ids"]) == 1 and threads and all(thread != caller for thread in threads)


@pytest.mark.parametrize("statement", ["Remember: Our audience is warehouse operators.", "Remember: Our audience is first-time parents."])
def test_capture_worker_calls_capture_but_never_pays_for_ineligible_founder_commands(extraction, monkeypatch, statement):
    from app.credits import policy
    from app.memory import extraction as capture
    d = extraction
    monkeypatch.setattr(policy, "get_policy", lambda *args: {"memory_capture_v1": True})
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = statement
        session.commit()
    called = []
    original = capture.capture_chat_message
    def record(source):
        called.append(source)
        return original(source)
    monkeypatch.setattr(capture, "capture_chat_message", record)
    monkeypatch.setattr(capture, "capture_model_decisions", lambda _: pytest.fail("ineligible source was sent to a model"))
    with d.bound():
        result = asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
    assert result["fact_ids"] == [] and called == ["msg_source"]


def test_model_job_uses_frozen_sol_call_source_quotes_and_replays_without_provider(extraction):
    d = extraction
    first = run(d)
    assert first["availability"] == "available" and len(first["fact_ids"]) == 1
    with d.bound():
        row = d.d.read()[0]
        assert row["source"] == {"type": "chat", "ref": "chat:msg_source", "quote": "independent bookshops"}
        assert row["predicate"] == "statement" and row["valid_from"].startswith("2026-10-01")
        assert run(d) == first
    assert len(d.calls) == 1
    with d.d.factory() as session:
        receipt = session.get(d.Document, d.api.identifier(d.payload["_job_id"]))
        runtime = json.loads(receipt.provenance_json)["_runtime"]
        assert runtime["calls_used"] == 1 and runtime["operations"]["extract"]["usage"]["settled"] is True
        assert "api_key" not in receipt.provenance_json and receipt.body_md is None


@pytest.mark.parametrize("flag", ["company_os_memory_facts_enabled", "company_os_memory_model_extract_enabled"])
def test_disabled_extraction_never_claims_or_calls_provider(extraction, monkeypatch, flag):
    from app.config import settings
    d = extraction
    monkeypatch.setattr(settings, flag, False)
    with pytest.raises(d.Fatal, match="memory_extract_disabled"):
        run(d)
    assert d.calls == [] and d.settlements == []
    with d.bound(), d.d.factory() as session:
        assert session.get(d.Document, d.api.identifier(d.payload["_job_id"])) is None


def test_existing_deterministic_full_quote_is_kept_without_losing_new_model_subject(extraction):
    from app.memory.extraction import capture_chat_message
    d = extraction
    d.state["facts"] = [{"subject": "idea.audience", "predicate": "is", "quote": "independent bookshops"},
                        {"subject": "decision.strategy", "quote": "Our pricing is ten dollars."}]
    with d.bound():
        canonical = capture_chat_message("msg_source")
        assert len(canonical) == 2
    result = run(d)
    with d.bound():
        rows = d.d.read()
    assert len(result["fact_ids"]) == 2
    assert any(row["value"] == "Our audience is independent bookshops." for row in rows)
    assert any(row["subject"] == "decision.strategy" for row in rows)
    assert not any(row["value"] == "independent bookshops" for row in rows)


def test_forgotten_deterministic_fact_is_not_revived_by_model(extraction):
    from app.memory.extraction import capture_chat_message
    d = extraction
    with d.bound():
        capture_chat_message("msg_source")
        row = d.d.read(subject="idea.audience")[0]
        with d.d.factory() as session:
            d.d.api.forget_fact(session, venture_id="ven_owned", fact_id=row["id"])
            session.commit()
    assert run(d)["fact_ids"] == [row["id"]]
    with d.bound():
        assert d.d.read(subject="idea.audience") == []


def test_model_keeps_sixth_same_turn_deterministic_correction_from_exact_source(extraction):
    from app.memory.extraction import capture_chat_message
    d = extraction
    body = " ".join([f"Our audience is group{index}." for index in range(5)]) + " Actually, our audience is independent bookshops."
    with d.d.factory() as session:
        session.get(d.d.Message, "msg_source").body_md = body
        session.commit()
    with d.bound():
        ids = capture_chat_message("msg_source")
        assert len(ids) == 1
        assert d.d.read()[0]["value"] == "Actually, our audience is independent bookshops."
    d.state["facts"] = [{"subject": "idea.audience", "quote": "group0"}]
    assert run(d)["fact_ids"] == ids
    with d.bound():
        assert d.d.read()[0]["value"] == "Actually, our audience is independent bookshops."


@pytest.mark.parametrize("change", [{"_job_attempt": True}, {"_worker_id": "other"},
                                    {"source_message_id": "other"}, {"_actor_subject": "usr_bob"}])
def test_lease_or_payload_forgery_is_refused_before_provider(extraction, change):
    d = extraction
    d.payload.update(change)
    with pytest.raises(d.Fatal):
        run(d)
    assert d.calls == []


def test_private_receipt_read_and_mutation_refuse_other_actor(extraction):
    d = extraction
    run(d)
    with d.d.bind(actor="usr_bob"):
        with pytest.raises(d.Fatal, match="receipt_invalid"):
            d.api.read(d.api.identifier(d.payload["_job_id"]))
        with pytest.raises(d.Fatal, match="receipt_invalid"):
            d.api.mutate(d.api.identifier(d.payload["_job_id"]), None, lambda *args: None)


@pytest.mark.parametrize("mode", ["tenant_only", "missing_proof", "invalid_proof", "revoked_owner"])
def test_automatic_model_ingress_requires_bound_live_private_source_proof(extraction, mode):
    from app.chat import conversations
    d = extraction
    if mode in {"missing_proof", "invalid_proof"}:
        if mode == "missing_proof":
            d.payload.pop("_chat_conversation")
        else:
            d.payload["_chat_conversation"]["schema_version"] = 2
        with d.d.factory() as session:
            job = session.get(d.Job, "job_extract")
            saved = json.loads(job.payload_json)
            if mode == "missing_proof":
                saved.pop("_chat_conversation")
            else:
                saved["_chat_conversation"] = d.payload["_chat_conversation"]
            job.payload_json = json.dumps(saved)
            session.commit()
    with d.d.bind():
        if mode == "tenant_only":
            with pytest.raises(d.Fatal, match="source_invalid"):
                asyncio.run(d.api.run(d.payload))
        else:
            with conversations.bind_conversation("ven_owned", "cnv_owned"):
                if mode == "revoked_owner":
                    with d.d.factory() as session:
                        session.get(d.d.Conversation, "cnv_owned").creator_person_id = "usr_bob"
                        session.commit()
                with pytest.raises(d.Fatal, match="source_invalid|conversation_invalid"):
                    asyncio.run(d.api.run(d.payload))
    assert d.calls == []


def test_response_settlement_can_replay_after_source_change_but_cannot_apply(extraction):
    d = extraction
    def change_source():
        with d.d.factory() as session:
            session.get(d.d.Message, "msg_source").body_md = "Our audience is different now."
            session.commit()
    d.state["after_response"] = change_source
    with pytest.raises(d.Fatal, match="source_changed"):
        run(d)
    assert d.settlements and len(d.calls) == 1
    with pytest.raises(d.Fatal, match="source_changed"):
        run(d)
    with d.bound():
        assert d.d.read() == []
    assert len(d.calls) == 1


def test_paid_response_after_lost_lease_is_retained_and_cannot_apply(extraction):
    d = extraction
    def lose_lease():
        with d.d.factory() as session:
            job = session.get(d.Job, "job_extract")
            job.locked_by, job.attempts = "other_worker", 2
            session.commit()
    d.state["after_response"] = lose_lease
    with pytest.raises(d.Fatal, match="lease_lost"):
        run(d)
    with d.bound():
        assert d.d.read() == []
        assert d.api.journal.operation(d.api.identifier("job_extract"), "extract")["state"] == "completed"
    assert len(d.calls) == 1 and d.settlements


@pytest.mark.parametrize("state", ["calling", "uncertain"])
def test_calling_or_uncertain_receipt_never_redials(extraction, state):
    d = extraction
    run(d)
    with d.bound():
        d.api.journal.save_operation(d.api.identifier("job_extract"), "extract", {"state": state})
    with pytest.raises(d.Fatal, match="outcome_uncertain|operation_in_progress"):
        run(d)
    assert len(d.calls) == 1


def test_pending_settlement_does_not_apply_or_redial(extraction):
    d = extraction
    d.state["settlement_error"] = True
    with pytest.raises(Exception, match="settlement_pending"):
        run(d)
    with d.d.bind():
        assert d.d.read() == []
    d.state["settlement_error"] = False
    assert len(run(d)["fact_ids"]) == 1 and len(d.calls) == 1


@pytest.mark.parametrize("result", [
    {"facts": [{"subject": "idea.audience", "quote": "invented audience"}]},
    {"facts": [{"subject": "idea.audience", "quote": "A"}, {"subject": "idea.audience", "quote": "B"}]},
    {"facts": [{"subject": "system.approval", "quote": "A"}]},
    {"facts": [{"subject": "idea.audience", "quote": "A", "confidence": 1}]},
])
def test_model_prose_duplicates_and_authority_fields_cannot_be_sources(extraction, result):
    with pytest.raises(ValueError):
        extraction.api.validate_quotes(json.dumps(result), "A B")


@pytest.fixture
def funded_extraction(extraction, tmp_path, monkeypatch):
    """Actual wallet, reservation, gate and ledger; only the model transport is mocked."""
    from sqlalchemy import select
    import app.state
    from app.chat import conversations
    from app.credits import _txn, gate, ledger, policy
    from app.db.models import Base, ChatConversation, ChatMessage, CreditAccount, CreditPolicy, Job, Person, Venture, Workspace, WorkspaceMember
    from app.db.append_only import _install_sqlite
    from app.db.session import make_engine, make_session_factory
    from app.research import metered
    d = extraction
    models = (Venture, ChatConversation, ChatMessage, Job)
    with d.d.factory() as session:
        sources = {model: [{column.name: getattr(row, column.name) for column in model.__table__.columns}
                          for row in session.scalars(select(model)).all()] for model in models}
    engine = make_engine(f"sqlite:///{tmp_path}/funded-extract.db")
    names = ("people", "person_identities", "workspaces", "workspace_members", "ventures", "chat_conversations",
             "chat_messages", "venture_facts", "jobs", "venture_documents", "credit_accounts", "credit_policies",
             "credit_reservations", "credit_ledger")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in names])
    factory = make_session_factory(engine)
    monkeypatch.setattr(app.state, "db", lambda: factory)
    monkeypatch.setattr(d.api, "db", lambda: factory)
    monkeypatch.setattr(conversations, "db", lambda: factory)
    monkeypatch.setattr(_txn, "db", lambda: factory)
    monkeypatch.setattr(gate, "reconcile_receipt", d.real_reconcile)
    monkeypatch.setattr(metered, "perform", d.real_perform)
    d.route["estimate_micro"] = 5_000_000
    now = _txn.naive_utc()
    with factory() as session:
        session.add_all([Person(id=person, display_name="Synthetic founder", created_at=now) for person in ("usr_alice", "usr_bob")])
        session.flush()
        session.add(Workspace(id="wsp_owned", kind="venture", name="Synthetic", entry_mode="idea", owner_id="usr_alice",
                              created_at=now, updated_at=now))
        session.flush()
        session.add_all([WorkspaceMember(id="member_" + person, workspace_id="wsp_owned", person_id=person,
                                        role="owner" if person == "usr_alice" else "contributor", created_at=now)
                         for person in ("usr_alice", "usr_bob")])
        for model in models:
            session.add_all([model(**values) for values in sources[model]])
            session.flush()
        session.merge(CreditPolicy(key=policy.WALLET_PHASE_KEY, value_json='"person"', updated_at=now, updated_by="fixture"))
        for person in ("usr_alice", "usr_bob"):
            wallet = CreditAccount(id="wallet_" + person, scope="person", owner_person_id=person, granted_micro=10_000_000,
                                   spent_micro=0, reserved_micro=0, status="active", version=0, created_at=now, updated_at=now)
            session.add(wallet)
            session.flush()
            ledger.append(session, account=wallet, kind="grant", amount_micro=wallet.granted_micro,
                          reason="Synthetic extraction funding", actor_kind="system", actor_id="fixture",
                          idempotency_key="fixture-grant:" + person)
        _install_sqlite(session.connection(), "credit_ledger")
        session.commit()
    policy.invalidate()
    d.funded_factory = factory
    yield d
    policy.invalidate()
    engine.dispose()


def test_actual_wallet_and_confirmed_usage_settle_once_on_model_replay(funded_extraction):
    from sqlalchemy import select
    from app.credits.ledger import verify_chain
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
    d = funded_extraction
    first = run(d)
    assert run(d) == first and len(d.calls) == 1
    with d.funded_factory() as session:
        spends = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")).all()
        holds = session.scalars(select(CreditReservation)).all()
        alice = session.get(CreditAccount, "wallet_usr_alice")
        bob = session.get(CreditAccount, "wallet_usr_bob")
        assert len(spends) == len(holds) == 1 and holds[0].status == "settled"
        assert spends[0].account_id == alice.id and spends[0].amount_micro < 0
        assert (spends[0].prompt_tokens, spends[0].completion_tokens) == (100, 2)
        assert alice.spent_micro == -spends[0].amount_micro and alice.reserved_micro == bob.spent_micro == bob.reserved_micro == 0
        meta = json.loads(spends[0].meta_json)
        assert meta["workspace_id"] == "wsp_owned" and meta["venture_id"] == "ven_owned" and meta["task_invocation"] is None
        assert holds[0].provider == "openai" and holds[0].model == "gpt-6.1-sol" and holds[0].purpose == "memory.extract"
    assert verify_chain("wallet_usr_alice") == (True, None)


@pytest.mark.parametrize("change", [{"account_id": "wallet_usr_bob"}, {"task_invocation": "foreign_task"}])
def test_real_receipt_replay_refuses_wrong_wallet_or_task_without_new_spend(funded_extraction, change):
    from app.db.models import CreditAccount
    from app.core.jobs import RetryableError
    d = funded_extraction
    run(d)
    with d.bound():
        did = d.api.identifier("job_extract")
        usage = d.api.journal.operation(did, "extract")["usage"]
        d.api.journal.save_operation(did, "extract", {"usage": {**usage, **change}})
    with pytest.raises(RetryableError, match="settlement_pending"):
        run(d)
    assert len(d.calls) == 1
    with d.funded_factory() as session:
        assert session.get(CreditAccount, "wallet_usr_bob").spent_micro == 0


def _leave_real_response_unsettled(d, monkeypatch):
    """Simulate a crash boundary after saved confirmed usage, before settlement."""
    from app.credits import gate
    from app.core.jobs import RetryableError
    monkeypatch.setattr(gate, "_exit", lambda meter: None)
    def pending(*args, **kwargs):
        raise RuntimeError("synthetic settlement outage")
    monkeypatch.setattr(gate, "reconcile_receipt", pending)
    with pytest.raises(RetryableError, match="settlement_pending"):
        run(d)
    monkeypatch.setattr(gate, "reconcile_receipt", d.real_reconcile)


def test_durable_confirmed_response_reconciles_before_already_lost_lease_on_retry(funded_extraction, monkeypatch):
    from sqlalchemy import select
    from app.credits.ledger import verify_chain
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
    from app.db.fact_models import VentureFact
    d = funded_extraction
    _leave_real_response_unsettled(d, monkeypatch)
    with d.funded_factory() as session:
        assert session.get(CreditAccount, "wallet_usr_alice").spent_micro == 0
        hold = session.scalar(select(CreditReservation))
        assert hold.status == "reconciling"
        job = session.get(d.Job, "job_extract")
        job.locked_by, job.attempts, job.status = "replacement_worker", 2, "retry_wait"
        session.commit()
    with pytest.raises(d.Fatal, match="lease_lost"):
        run(d)
    assert len(d.calls) == 1
    with d.funded_factory() as session:
        spends = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")).all()
        assert len(spends) == 1 and session.scalar(select(VentureFact.id)) is None
        assert session.scalar(select(CreditReservation)).status == "settled"
        alice = session.get(CreditAccount, "wallet_usr_alice")
        assert alice.spent_micro == -spends[0].amount_micro and alice.reserved_micro == 0
        assert session.get(CreditAccount, "wallet_usr_bob").spent_micro == 0
        runtime = json.loads(session.get(d.Document, d.api.identifier("job_extract")).provenance_json)["_runtime"]
        assert runtime["operations"]["extract"]["state"] == "completed"
        assert runtime["operations"]["extract"]["usage"]["settled"] is True
    assert verify_chain("wallet_usr_alice") == (True, None)


def test_disabling_extraction_still_reconciles_confirmed_usage_without_applying(funded_extraction, monkeypatch):
    from sqlalchemy import select
    from app.config import settings
    from app.credits.ledger import verify_chain
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
    d = funded_extraction
    _leave_real_response_unsettled(d, monkeypatch)
    monkeypatch.setattr(settings, "company_os_memory_model_extract_enabled", False)
    with pytest.raises(d.Fatal, match="memory_extract_disabled"):
        run(d)
    with d.funded_factory() as session:
        assert session.scalar(select(CreditReservation)).status == "settled"
        assert len(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")).all()) == 1
        assert session.get(CreditAccount, "wallet_usr_alice").reserved_micro == 0
    assert len(d.calls) == 1 and verify_chain("wallet_usr_alice") == (True, None)


@pytest.mark.parametrize("statement", ["Our audience is warehouse operators.", "Our audience is first-time parents."])
def test_capture_disabled_still_reconciles_owned_confirmed_usage_without_learning(funded_extraction, monkeypatch, statement):
    from sqlalchemy import select
    from app.credits import policy
    from app.credits.ledger import verify_chain
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
    from app.db.fact_models import VentureFact
    from app.memory import extraction as capture
    d = funded_extraction
    with d.funded_factory() as session:
        session.get(d.d.Message, "msg_source").body_md = statement
        session.commit()
    d.state["facts"] = [{"subject": "idea.audience", "quote": statement}]
    _leave_real_response_unsettled(d, monkeypatch)
    monkeypatch.setattr(policy, "get_policy", lambda *args: {})
    with d.bound():
        result = asyncio.run(capture.run_capture({**d.payload, "conversation_id": "cnv_owned"}))
    assert result == {"fact_ids": []} and len(d.calls) == 1
    with d.funded_factory() as session:
        assert session.scalar(select(CreditReservation)).status == "settled"
        assert len(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")).all()) == 1
        assert session.scalar(select(VentureFact.id)) is None
        assert session.get(CreditAccount, "wallet_usr_alice").reserved_micro == 0
        assert session.get(CreditAccount, "wallet_usr_bob").spent_micro == 0
    assert verify_chain("wallet_usr_alice") == (True, None)


@pytest.mark.parametrize("change", [{"source_message_id": "forged_source"}, {"_actor_subject": "usr_bob"},
    {"_chat_conversation": {"schema_version": 1, "conversation_id": "foreign"}}, {"workspace_id": "foreign"}])
def test_forged_recovery_payload_cannot_settle_owned_pending_usage(funded_extraction, monkeypatch, change):
    from app.db.models import CreditAccount
    d = funded_extraction
    _leave_real_response_unsettled(d, monkeypatch)
    payload = {**d.payload, **change}
    with d.bound(), pytest.raises(d.Fatal, match="scope_invalid"):
        d.api._run(payload)
    assert len(d.calls) == 1
    with d.funded_factory() as session:
        assert session.get(CreditAccount, "wallet_usr_alice").spent_micro == 0
        assert session.get(CreditAccount, "wallet_usr_bob").spent_micro == 0


def test_foreign_actor_cannot_reconcile_private_confirmed_receipt(funded_extraction, monkeypatch):
    from app.db.models import CreditAccount
    d = funded_extraction
    _leave_real_response_unsettled(d, monkeypatch)
    with d.d.bind(actor="usr_bob"), pytest.raises(d.Fatal, match="receipt_invalid"):
        d.api._run(d.payload)
    with d.funded_factory() as session:
        assert session.get(CreditAccount, "wallet_usr_alice").spent_micro == 0
        assert session.get(CreditAccount, "wallet_usr_bob").spent_micro == 0
    assert len(d.calls) == 1


def test_registered_sol_route_schema_and_wire_estimate_are_captured_before_handler_dial(extraction, monkeypatch):
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, routing, sole
    d = extraction
    config = d.config
    config.source, config.provider, config.api_mode = "platform_default", "openai", "responses"
    config.catalog_model = config.deployment_pro = config.deployment_flash = "gpt-6.1-sol"
    config.reasoning_effort, config.base_url = "low", "https://api.openai.com/v1"
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda workspace_id: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    captured = []
    def resolve(purpose, original, tier=None):
        assert purpose == "memory.extract" and original is config and tier is None
        captured.append(purpose)
        return config
    def wire(messages, **kwargs):
        assert kwargs["purpose"] == "memory.extract" and kwargs["config"] is config
        assert kwargs["response_schema"] == d.api.FactExtraction.model_json_schema()
        assert messages[-1]["role"] == "user" and "bookshops" in messages[-1]["content"]
        return 200
    monkeypatch.setattr(routing, "resolve_purpose_config", resolve, raising=False)
    monkeypatch.setattr(routing, "is_sol_model", lambda *args: True, raising=False)
    monkeypatch.setattr(sole, "transport_prompt_tokens", wire, raising=False)
    monkeypatch.setattr(d.api, "capture_route", d.real_capture)
    assert len(run(d)["fact_ids"]) == 1 and captured == ["memory.extract"] and len(d.calls) == 1
    with d.bound():
        runtime = d.api.read(d.api.identifier("job_extract")).document["_runtime"]
        assert runtime["route"]["api_mode"] == "responses" and runtime["route"]["tier"] == "flash"
        assert runtime["route"]["reasoning_effort"] == "low" and runtime["route"]["prompt_tokens"] == 200
        assert runtime["route"]["max_tokens"] == d.calls[0]["max_tokens"] == 2048


def test_eval_collection_reads_no_key_and_literal_key_parser_runs_no_shell(tmp_path, monkeypatch, capsys):
    from scripts.memory import eval_extract as script
    missing = tmp_path / "missing.env"
    assert script.main(["--max-calls", "3", "--key-file", str(missing)]) == 0
    collected = json.loads(capsys.readouterr().out)
    assert collected["status"] == "collected_no_calls" and len(collected["cases"]) == 3
    literal = "unit" + "$" + "{HOME}#$(touch unwanted)"
    key_file = tmp_path / "private.env"
    key_file.write_text("export PAGEBUILDER_OPENAI_API_KEY='" + literal + "'\n")
    key_file.chmod(0o600)
    assert script.read_key(key_file) == literal and capsys.readouterr().out == ""
    assert not (tmp_path / "unwanted").exists()
    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions_invalid"):
        script.read_key(key_file)


@pytest.mark.parametrize("value", ["0", "-1", "1.51", "NaN", "Infinity"])
def test_eval_cannot_raise_root_spend_allocation(value):
    import argparse
    from scripts.memory.eval_extract import budget_usd
    with pytest.raises(argparse.ArgumentTypeError):
        budget_usd(value)


def test_eval_call_ceiling_and_protected_cost_prevent_extra_reservation():
    import argparse
    from scripts.memory.eval_extract import call_limit, can_reserve
    with pytest.raises(argparse.ArgumentTypeError):
        call_limit("31")
    assert can_reserve(100, 100, 100, 300)
    assert not can_reserve(101, 100, 100, 300)
    assert not can_reserve(True, 0, 0, 300)


def _blocked_eval_worker(args, directory, sender):
    """Owned dummy process; it never loads a key, imports an app or calls a model."""
    import time
    time.sleep(60)


def _completed_eval_worker(args, directory, sender):
    sender.send({"status": "completed", "ledger_verified": True})
    sender.close()


def test_eval_whole_process_deadline_preserves_pending_receipt_without_key_or_call(tmp_path, monkeypatch):
    from scripts.memory import eval_extract as script
    receipt = tmp_path / "eval.db"
    receipt.write_bytes(b"synthetic existing private receipt with uncertain hold")
    monkeypatch.setattr(script, "HARD_SECONDS", 0.2)
    result = script.bounded_execute(SimpleNamespace(key_file=tmp_path / "missing.env"), tmp_path,
                                    worker=_blocked_eval_worker)
    assert result["status"] == "hard_deadline_halt" and result["outcome"] == "unverified"
    assert result["receipts_retained"] is True and result["holds_released"] is False
    assert result["elapsed_seconds"] >= 0.2
    assert receipt.read_bytes() == b"synthetic existing private receipt with uncertain hold"


def test_eval_process_result_reports_truthful_parent_elapsed(tmp_path, monkeypatch):
    from scripts.memory import eval_extract as script
    monkeypatch.setattr(script, "HARD_SECONDS", 10)
    result = script.bounded_execute(SimpleNamespace(key_file=tmp_path / "missing.env"), tmp_path,
                                    worker=_completed_eval_worker)
    assert result["status"] == "completed" and result["ledger_verified"] is True
    assert 0 < result["elapsed_seconds"] < 10


def test_eval_default_collection_never_enters_execute_or_reads_key(monkeypatch, capsys):
    from scripts.memory import eval_extract as script
    def forbidden(*args, **kwargs):
        pytest.fail("default collection entered a key or process execution path")
    monkeypatch.setattr(script, "read_key", forbidden)
    monkeypatch.setattr(script, "bounded_execute", forbidden)
    assert script.main(["--key-file", "/missing/never-read.env", "--max-calls", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "collected_no_calls"
