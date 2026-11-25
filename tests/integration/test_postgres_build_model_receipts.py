"""Provider mocked only at network seam; migrated PG locks, receipts and metering are real."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_build_pools import build_pool as build_pool, snapshot

REQUEST = {"messages": [{"role": "user", "content": "Build a bakery waitlist form"}], "max_tokens": 128}
FENCE = {"generation": 1, "controller_id": "controller-fixture", "attempt": 1}


@pytest.fixture()
def model_run(build_pool, monkeypatch):
    from app.builder import model_proxy
    from app.builder.budgets import model_pricing
    from app.db.supymem_models import BuildRun
    d = build_pool
    with d["factory"]() as session:
        run = session.get(BuildRun, d["run_id"])
        run.state = "running"
        run.spec = json.dumps({"actor": d["actor"], "generation": 1, "controller_fence": {"id": FENCE["controller_id"], "attempt": 1},
            "model_ceiling_micro": 800, "model_config": {"model": "DeepSeek-V4-Pro", "provider": "azure_foundry", "source": "platform_default", "pricing": model_pricing("DeepSeek-V4-Pro")}})
        session.commit()
    calls = []
    def provider(wid, body):
        calls.append((wid, body))
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            choices=[SimpleNamespace(message=SimpleNamespace(content="A tailored form", tool_calls=None), finish_reason="stop")])
    monkeypatch.setattr(model_proxy, "_provider_call", provider)
    yield {**d, "calls": calls, "provider": provider}


def register(d, key="intent-1", request=REQUEST):
    from app.builder.model_proxy import register
    from app.db.supymem_models import BuildRun
    with d["factory"]() as session:
        cursor = json.loads(session.get(BuildRun, d["run_id"]).spec).get("agent_cursor")
    return register(d["run_id"], invocation_key=key, request=request, input_event_id=cursor, **FENCE)["invocation_id"]


def complete(d, cid, request=REQUEST):
    from app.builder.model_proxy import complete
    return complete(d["run_id"], cid, request, generation=1)


def consume(d, cid):
    from app.builder.model_proxy import consume
    from app.credits.pools import lock_pool
    from app.db.supymem_models import BuildEvent, BuildModelCall
    from core.clock import utcnow
    with d["factory"]() as session:
        ctx = lock_pool(session, d["pool_id"])
        row = session.get(BuildModelCall, cid)
        ctx.run.event_seq += 1
        seq = ctx.run.event_seq
        session.add(BuildEvent(build_run_id=d["run_id"], seq=seq, kind="agent_event", ts=utcnow(),
            payload=json.dumps({"invocation_id": cid, "response_digest": row.response_digest, "llm_response_id": cid,
                                "event_type": "MessageEvent", "event_id": "sdk-event-" + cid, "parent_id": row.input_event_id})))
        session.commit()
    consume(d["run_id"], cid, mirrored_event_seq=seq, **FENCE)


def test_same_intent_replays_encrypted_response_with_one_actual_debit(model_run):
    from app.db.models import CreditLedgerEntry
    from app.db.supymem_models import BuildModelCall
    d = model_run
    cid = register(d)
    response = complete(d, cid)
    assert register(d) == cid
    assert complete(d, cid) == response
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        row = session.get(BuildModelCall, cid)
        assert row.state == "settled" and row.charged_micro > 0
        assert "tailored" not in row.response_ref and "bakery" not in row.response_ref
        entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == row.reservation_id)).all()
        assert len(entries) == 1 and entries[0].amount_micro == -row.charged_micro
    snapshot(d)


def test_identical_prompts_with_distinct_consumed_sdk_intents_are_distinct_calls(model_run):
    from app.builder.model_proxy import BuildModelError
    d = model_run
    first = register(d)
    complete(d, first)
    with pytest.raises(BuildModelError, match="not_consumed"):
        register(d, "intent-2")
    consume(d, first)
    second = register(d, "intent-2")
    assert second != first
    complete(d, second)
    assert len(d["calls"]) == 2 and snapshot(d)[1] > 0


def test_intent_changed_body_and_stale_generation_fail_before_provider(model_run):
    from app.builder.model_proxy import BuildModelError, complete as call
    d = model_run
    cid = register(d)
    changed = {**REQUEST, "max_tokens": 129}
    with pytest.raises(BuildModelError, match="conflict"):
        register(d, request=changed)
    with pytest.raises(BuildModelError, match="generation_revoked"):
        call(d["run_id"], cid, REQUEST, generation=2)
    assert not d["calls"]


def test_concurrent_http_retry_cannot_redial_active_provider(model_run, monkeypatch):
    from app.builder import model_proxy
    d = model_run
    cid = register(d)
    entered, proceed = threading.Event(), threading.Event()
    def blocking(wid, body):
        entered.set()
        assert proceed.wait(10)
        return d["provider"](wid, body)
    monkeypatch.setattr(model_proxy, "_provider_call", blocking)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(complete, d, cid)
        assert entered.wait(10)
        with pytest.raises(model_proxy.BuildModelError, match="in_progress"):
            complete(d, cid)
        proceed.set()
        assert future.result(timeout=10)["id"] == cid
    assert len(d["calls"]) == 1


def test_lost_provider_response_retains_hold_and_never_auto_retries(model_run, monkeypatch):
    from app.builder import model_proxy
    d = model_run
    cid = register(d)
    def unknown(*args):
        d["calls"].append("ambiguous")
        raise TimeoutError("synthetic response lost")
    monkeypatch.setattr(model_proxy, "_provider_call", unknown)
    with pytest.raises(model_proxy.BuildModelError, match="outcome_uncertain"):
        complete(d, cid)
    with pytest.raises(model_proxy.BuildModelError, match="outcome_uncertain"):
        complete(d, cid)
    assert d["calls"] == ["ambiguous"]
    assert snapshot(d)[0] == 1000 and snapshot(d)[1] == 0 and snapshot(d)[3] == "reconciling"


def test_boundary_refusal_releases_call_hold_as_known_local_outcome(model_run, monkeypatch):
    from app.builder import model_proxy
    from app.db.supymem_models import BuildModelCall
    from llm_gateway.boundary import BoundaryViolation

    d = model_run
    cid = register(d)
    monkeypatch.setattr(model_proxy, "_provider_call", lambda *_: (_ for _ in ()).throw(
        BoundaryViolation("fixture refusal", classes=("unknown",), reason="fixture")))

    with pytest.raises(model_proxy.BuildModelError, match="boundary_blocked"):
        complete(d, cid)

    with d["factory"]() as session:
        row = session.get(BuildModelCall, cid)
        assert row.state == "released"
        assert row.error_code == "build_model_boundary_blocked"
    reserved, spent, remaining, status = snapshot(d)
    assert (reserved, spent, remaining, status) == (1000, 0, 1000, "reconciling")


def test_response_persisted_before_settlement_failure_recovers_without_redial(model_run, monkeypatch):
    from app.builder import model_proxy
    from app.db.supymem_models import BuildModelCall
    d = model_run
    cid = register(d)
    original = model_proxy._settle
    with monkeypatch.context() as scoped:
        scoped.setattr(model_proxy, "_settle", lambda *args: (_ for _ in ()).throw(RuntimeError("DB settlement failure")))
        with pytest.raises(RuntimeError, match="settlement"):
            complete(d, cid)
    with d["factory"]() as session:
        row = session.get(BuildModelCall, cid)
        assert row.state == "responded" and row.response_ref
    assert model_proxy._settle is original
    assert complete(d, cid)["id"] == cid
    assert len(d["calls"]) == 1 and snapshot(d)[1] > 0


def test_worker_death_after_calling_persisted_is_uncertain_not_reexecuted(model_run):
    from app.builder import model_proxy
    from app.db.supymem_models import BuildModelCall
    from core.clock import utcnow
    d = model_run
    cid = register(d)
    with d["factory"]() as session:
        row = session.get(BuildModelCall, cid)
        row.state, row.updated_at = "calling", utcnow() - timedelta(minutes=10)
        session.commit()
    assert model_proxy.reconcile_stale_calls() >= 1
    with pytest.raises(model_proxy.BuildModelError, match="outcome_uncertain"):
        complete(d, cid)
    assert not d["calls"] and snapshot(d)[0] == 1000


def test_slow_provider_call_stays_live_within_managed_deadline(model_run):
    from app.builder import model_proxy
    from app.db.supymem_models import BuildModelCall
    from core.clock import utcnow
    d = model_run
    cid = register(d)
    with d["factory"]() as session:
        row = session.get(BuildModelCall, cid)
        row.state, row.updated_at = "calling", utcnow() - timedelta(seconds=300)
        session.commit()
    assert model_proxy.reconcile_stale_calls() == 0
    with d["factory"]() as session:
        assert session.get(BuildModelCall, cid).state == "calling"


def test_forged_sdk_ack_cannot_authorize_second_model_spend(model_run):
    from app.builder.model_proxy import consume as acknowledge, BuildModelError
    d = model_run
    cid = register(d)
    complete(d, cid)
    with pytest.raises(BuildModelError, match="not_persisted"):
        acknowledge(d["run_id"], cid, mirrored_event_seq=999, **FENCE)
    with pytest.raises(BuildModelError, match="not_consumed"):
        register(d, "second")


def test_foreign_run_receipt_and_removed_founder_cannot_call(model_run):
    from app.builder.model_proxy import BuildModelError
    from app.db.models import Workspace
    d = model_run
    cid = register(d)
    with d["factory"]() as session:
        ws = session.get(Workspace, d["wid"])
        ws.settings_json = '{"blocked":true}'
        session.commit()
    with pytest.raises(BuildModelError, match="authority_revoked"):
        complete(d, cid)
    assert not d["calls"]


@pytest.mark.parametrize("payload", [{**REQUEST, "stream": True}, {**REQUEST, "base_url": "https://attacker.test"},
    {**REQUEST, "max_tokens": True}, {**REQUEST, "max_tokens": 16385},
    {**REQUEST, "model": "unapproved-model"}])
def test_provider_controls_and_model_overrides_fail_closed(model_run, payload):
    from app.builder.model_proxy import BuildModelError
    with pytest.raises(BuildModelError):
        register(model_run, request=payload)
    assert not model_run["calls"]


def test_provider_multitool_response_is_charged_once_and_serialized(model_run, monkeypatch):
    from app.builder import model_proxy
    from app.db.supymem_models import BuildModelCall
    d = model_run
    from app.db.supymem_models import BuildRun
    with d["factory"]() as session:
        run = session.get(BuildRun, d["run_id"])
        spec = json.loads(run.spec)
        spec["allowed_tool_names"] = ["run"]
        run.spec = json.dumps(spec)
        session.commit()
    request = {**REQUEST, "tools": [{"type": "function", "function": {"name": "run", "parameters": {"type": "object"}}}]}
    cid = register(d, request=request)
    def multiple(wid, body):
        d["calls"].append("multiple")
        calls = [SimpleNamespace(id="call-" + str(i), type="function", function=SimpleNamespace(name="run", arguments="{}")) for i in range(2)]
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=calls), finish_reason="tool_calls")])
    monkeypatch.setattr(model_proxy, "_provider_call", multiple)
    first = complete(d, cid, request=request)
    replay = complete(d, cid, request=request)
    assert first == replay
    assert [call["id"] for call in first["choices"][0]["message"]["tool_calls"]] == ["call-0"]
    with d["factory"]() as session:
        row = session.get(BuildModelCall, cid)
        assert row.state == "settled" and row.charged_micro > 0 and not row.error_code
    assert len(d["calls"]) == 1 and snapshot(d)[3] == "active"


def test_exact_internal_http_contract_separates_controller_and_model_credentials(model_run, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.security.test_core_signed_boundary import SECRET, headers
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "isolated-build-vault-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "operator@example.test")
    controller = "isolated-controller-credential-more-than-32-bytes"
    monkeypatch.setenv("BUILD_CONTROLLER_TOKEN", controller)
    from app.main import create_app
    from app.builder.credentials import issue_model_credential
    d = model_run
    client = TestClient(create_app())  # No app lifespan/schedulers.
    token = issue_model_credential(d["run_id"], **FENCE)
    assert issue_model_credential(d["run_id"], **FENCE) == token
    registration = f"/internal/builds/{d['run_id']}/model-invocations"
    body = {**FENCE, "invocation_key": "http-intent", "input_event_id": None, "request": REQUEST}
    assert client.post(registration, headers=headers(registration, "POST", d["actor"]), json=body).status_code == 401
    assert client.post(registration, headers={"X-Build-Controller": token}, json=body).status_code == 401
    registered = client.post(registration, headers={"X-Build-Controller": controller}, json=body)
    assert registered.status_code == 200, registered.text
    cid = registered.json()["invocation_id"]
    path = f"/internal/builds/{d['run_id']}/v1/chat/completions"
    auth = {"Authorization": "Bearer " + token, "X-Supymem-Invocation-ID": cid}
    assert client.post(path, headers={"X-Build-Controller": controller}, json=REQUEST).status_code == 401
    assert client.post(path, headers=auth, json=REQUEST).status_code == 200
    assert client.post(path, headers=auth, json=REQUEST).status_code == 200
    foreign_path = "/internal/builds/bld_foreign/v1/chat/completions"
    assert client.post(foreign_path, headers=auth, json=REQUEST).status_code == 401
    assert client.post(f"/internal/builds/{d['run_id']}/arbitrary-command", headers={"X-Build-Controller": controller}, json={}).status_code in {401, 404}
    assert len(d["calls"]) == 1
    client.close()


def test_sdk_mirror_observation_cursor_and_old_ack_never_rewind(model_run, monkeypatch):
    from app.builder import sdk_authority as authority, model_proxy
    from app.db.supymem_models import BuildRun
    d = model_run
    with d["factory"]() as session:
        row = session.get(BuildRun, d["run_id"])
        spec = json.loads(row.spec)
        spec["allowed_tool_names"] = ["terminal"]
        row.spec = json.dumps(spec)
        session.commit()
    def event(event_id, parent_id, event_type, **kwargs):
        value = {"event_id": event_id, "parent_id": parent_id, "event_type": event_type, **kwargs}
        return {**value, "event_digest": model_proxy.digest(value)}
    system = event("sdk-system", None, "SystemPromptEvent")
    first = authority.mirror(d["run_id"], system, **FENCE)
    assert authority.mirror(d["run_id"], system, **FENCE) == first
    authority.advance_cursor(d["run_id"], "sdk-system", **FENCE)
    tool_request = {**REQUEST, "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}]}
    def tool_response(*_):
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="tool-1", type="function",
                function=SimpleNamespace(name="terminal", arguments='{"command":"synthetic test only"}'))]), finish_reason="tool_calls")])
    monkeypatch.setattr(model_proxy, "_provider_call", tool_response)
    cid = register(d, request=tool_request)
    complete(d, cid, request=tool_request)
    receipt = authority.read_receipt(d["run_id"], cid, **FENCE)
    assert receipt["expected"] == {"event_type": "ActionEvent", "tool_call_id": "tool-1"}
    assert receipt["consumed"] is False and receipt["input_event_id"] == "sdk-system"
    action = event("sdk-action", "sdk-system", "ActionEvent", llm_response_id=cid, tool_call_id="tool-1", source="agent")
    mirrored = authority.mirror(d["run_id"], action, **FENCE)
    authority.consume(d["run_id"], cid, mirrored_event_seq=mirrored["seq"], **FENCE)
    observed = event("sdk-observation", "sdk-action", "ObservationEvent", action_id="sdk-action", tool_call_id="tool-1", source="environment")
    authority.mirror(d["run_id"], observed, **FENCE)
    authority.advance_cursor(d["run_id"], "sdk-observation", **FENCE)
    authority.consume(d["run_id"], cid, mirrored_event_seq=mirrored["seq"], **FENCE)
    with d["factory"]() as session:
        assert json.loads(session.get(BuildRun, d["run_id"]).spec)["agent_cursor"] == "sdk-observation"
    assert authority.read_receipt(d["run_id"], cid, **FENCE)["consumed_event_id"] == "sdk-action"
    state = event("sdk-state", None, "ConversationStateUpdateEvent")
    authority.mirror(d["run_id"], state, **FENCE)
    assert authority.advance_cursor(d["run_id"], "sdk-observation", **FENCE)["input_event_id"] == "sdk-observation"
    with pytest.raises(model_proxy.BuildModelError, match="not_mirrored_leaf"):
        authority.advance_cursor(d["run_id"], "sdk-system", **FENCE)
    with pytest.raises(model_proxy.BuildModelError, match="event_invalid"):
        authority.mirror(d["run_id"], {**action, "response_digest": "f" * 64}, **FENCE)
    with pytest.raises(model_proxy.BuildModelError, match="conflict"):
        authority.mirror(d["run_id"], {**action, "event_digest": "f" * 64}, **FENCE)
    # A second intentional request can now bind the actual persisted SDK leaf.
    assert register(d, "next-intent", request=tool_request) != cid


def test_cancel_reconciler_releases_only_receipted_pre_io_allocations(model_run):
    from app.builder.reconcile import reconcile_held_builds
    from app.credits import pools
    from app.db.supymem_models import BuildModelCall
    d = model_run
    cid = register(d)
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        pools.mark_reconciliation(session, ctx, "founder_cancelled")
        session.commit()
    assert reconcile_held_builds()["pending"] >= 1
    with d["factory"]() as session:
        assert session.get(BuildModelCall, cid).state == "released"
    assert not d["calls"] and snapshot(d)[0:3] == (1000, 0, 1000)
