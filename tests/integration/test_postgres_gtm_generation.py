"""Migrated PostgreSQL and real jobs/credits; replace only provider network I/O."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.integration.test_postgres_auth_security import pg_database as pg_database, concurrent
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db

DIAGNOSIS = {"bottleneck": "Buyer demand is unverified", "rationale": "The founder describes a concept, not observed demand.",
    "assumptions": ["Bakery owners need an order board"], "evidence_gaps": ["No customer interviews supplied"],
    "next_actions": [{"kind": "human_interview", "instruction": "Ask a bakery owner how orders arrive.", "reason": "Test the stated pain before choosing a channel."}]}
REQUEST = {"kind": "diagnose", "idea": "Bakery order board", "problem": "Notes get lost", "who": "Bakery owners", "users_count": 0}

@pytest.fixture()
def gtm_db(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, Job
    from tests.idea_fixtures import install_provider
    d = journey_db
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with d["factory"]() as session:
        session.get(CreditAccount, account.id).granted_micro = 100000000
        session.commit()
    d["account"] = account.id
    d["responses"] = []
    def complete(messages, options):
        result = d["responses"].pop(0) if d["responses"] else DIAGNOSIS
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)), finish_reason=d.get("finish_reason", "stop"))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450), model="DeepSeek-V4-Pro")
    d["calls"] = install_provider(monkeypatch, complete)
    yield d
    with d["factory"]() as session:
        for job in session.scalars(select(Job).where(Job.workspace_id == d["wid"], Job.kind == "gtm.god.run")):
            job.status = "cancelled"
        session.commit()


def start(d, key="diagnosis-intent-1", request=None):
    from app.gtm.god import generation as gen
    from app.tenancy import bind
    request = request or REQUEST
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        q = gen.quote(d["wid"], d["vid"], request)
        run = gen.start(d["wid"], d["vid"], request, idempotency_key=key,
            **{k:q[k] for k in ("input_digest", "resource_digest", "ceiling_micro")})
    return run, q


def view(d, run):
    from app.gtm.god.generation import get
    from app.tenancy import bind
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        return get(d["wid"], d["vid"], run["run_id"])


def work(d, run):
    from app.core.jobs import lease
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with d["factory"]() as session:
        jobs = lease(session, "gtm-test-worker", kinds=["gtm.god.run"], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].id == run["job_id"]
    asyncio.run(_run_one(d["factory"], jobs[0], {jobs[0].id}))
    from app.db.models import Job
    with d["factory"]() as session:
        stored = session.get(Job, jobs[0].id)
        print("job outcome", stored.status, stored.last_error)
    return jobs[0]


def test_quote_start_diagnosis_receipt_no_specialists(gtm_db):
    from app.db.models import GtmGodPlan, GtmGodTask, CreditAccount, Job
    d = gtm_db
    run, q = start(d)
    assert not d["calls"] and run["state"] == "queued"
    assert q["call_budget"] == 1 and q["operations"] == ["diagnosis:1"]
    work(d, run)
    result = view(d, run)
    assert result["state"] == "completed", result
    assert result["result"]["diagnosis"] == DIAGNOSIS
    assert result["result"]["scope"] == "diagnosis_only"
    assert result["result"]["strategy_execution_supported"] is False
    assert 0 < result["consumed_micro"] <= q["ceiling_micro"] and result["reserved_micro"] == 0
    assert len(d["calls"]) == 1
    messages, options = d["calls"][0]
    assert options["purpose"] == "gtm.god_diagnose"
    assert options["captured_config"].timeout_seconds == 240
    assert options["max_tokens"] == 8192
    contract = json.loads(messages[-1]["content"])
    assert contract["input_provenance"] == "founder_reported"
    assert contract["schema"]["additionalProperties"] is False
    assert "No external research has been performed" in messages[0]["content"]
    with d["factory"]() as session:
        assert session.get(GtmGodPlan, run["plan_id"]).status == "paused"
        assert not list(session.scalars(select(GtmGodTask).where(GtmGodTask.plan_id == run["plan_id"])))
        assert session.get(Job, run["job_id"]).status == "succeeded"
        assert session.get(CreditAccount, d["account"]).spent_micro == result["consumed_micro"]
    assert "_runtime" not in json.dumps(result)


def test_concurrent_idempotency_and_resource_changed_replay(gtm_db, monkeypatch):
    from app.gtm.god import generation as gen
    from app.tenancy import bind
    d = gtm_db
    run, q = start(d)
    accepted = {k:q[k] for k in ("input_digest", "resource_digest", "ceiling_micro")}
    def again():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return gen.start(d["wid"], d["vid"], REQUEST, idempotency_key="diagnosis-intent-1", **accepted)["run_id"]
    assert set(concurrent(again)) == {run["run_id"]}
    monkeypatch.setattr(gen, "resources", lambda *args: pytest.fail("Replay must not re-resolve provider"))
    assert again() == run["run_id"]
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(ValueError, match="gtm_idempotency_conflict"):
            gen.start(d["wid"], d["vid"], {**REQUEST, "who": "Changed"}, idempotency_key="diagnosis-intent-1", **accepted)
    assert not d["calls"]


@pytest.mark.parametrize("mode,error", [("offline", "gtm_provider_offline"), ("invalid", "gtm_invalid_output"), ("truncated", "gtm_output_truncated")])
def test_failure_never_fabricates_diagnosis(gtm_db, monkeypatch, mode, error):
    d = gtm_db
    run, _ = start(d)
    if mode == "offline":
        monkeypatch.setenv("LLM_OFFLINE", "1")
    if mode == "invalid":
        d["responses"] = [{"done": True}]
    if mode == "truncated":
        d["finish_reason"] = "length"
    work(d, run)
    result = view(d, run)
    assert result["state"] == "failed" and result["error_code"] == error, result
    assert result["result"] == {}
    assert len(d["calls"]) == (0 if mode == "offline" else 1)
    assert result["consumed_micro"] == 0 if mode == "offline" else result["consumed_micro"] > 0


def test_enqueue_rollback_and_changed_quote(gtm_db, monkeypatch):
    from app.gtm.god import generation as gen
    from app.db.models import GtmGodRun, GtmGodPlan
    from app.tenancy import bind
    d = gtm_db
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        q = gen.quote(d["wid"], d["vid"], REQUEST)
        accepted = {k:q[k] for k in ("input_digest", "resource_digest", "ceiling_micro")}
        with pytest.raises(ValueError, match="gtm_quote_changed"):
            gen.start(d["wid"], d["vid"], {**REQUEST, "idea":"Changed"}, idempotency_key="changed-intent", **accepted)
        monkeypatch.setattr(gen, "enqueue", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("enqueue failed")))
        with pytest.raises(RuntimeError, match="enqueue failed"):
            gen.start(d["wid"], d["vid"], REQUEST, idempotency_key="failed-enqueue", **accepted)
    with d["factory"]() as session:
        assert not list(session.scalars(select(GtmGodRun).where(GtmGodRun.workspace_id == d["wid"])))
        assert not list(session.scalars(select(GtmGodPlan).where(GtmGodPlan.workspace_id == d["wid"])))


def test_scope_and_selected_venture_account(gtm_db):
    from app.db.models import Venture, CreditAccount
    from app.credits.accounts import ensure_account
    from app.gtm.god import generation as gen
    from app.tenancy import bind
    from fastapi import HTTPException
    from core.clock import utcnow
    from core.ids import new_id
    d = gtm_db
    other = new_id("ven")
    with d["factory"]() as session:
        session.add(Venture(id=other, workspace_id=d["wid"], name="Other", raw_idea="Other", stage="draft", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    account = ensure_account(other, workspace_id=d["wid"], owner_person_id=d["actor"])
    with d["factory"]() as session:
        row = session.get(CreditAccount, account.id)
        row.granted_micro = 100000000
        session.commit()
    selected = {**d, "vid": other}
    run, _ = start(selected)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(ValueError, match="gtm_not_found"):
            gen.get(d["wid"], d["vid"], run["run_id"])
    with bind(workspace_id=d["wid"], venture_id=other, actor="foreign-person"):
        with pytest.raises(HTTPException):
            gen.get(d["wid"], other, run["run_id"])
    work(selected, run)
    assert view(selected, run)["state"] == "completed"
    with d["factory"]() as session:
        assert session.get(CreditAccount, d["account"]).spent_micro == 0
        assert session.get(CreditAccount, account.id).spent_micro > 0


def test_insufficient_and_changed_resources_block_before_provider(gtm_db, monkeypatch):
    from app.gtm.god import generation as gen
    from app.db.models import CreditAccount
    from app.tenancy import bind
    d = gtm_db
    run, _ = start(d)
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "unpriced-changed-model")
    work(d, run)
    assert not d["calls"]
    assert view(d, run)["state"] in {"failed", "needs_input"}
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro")
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        account.granted_micro = account.spent_micro + account.reserved_micro
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        req = REQUEST
        q = gen.quote(d["wid"], d["vid"], req)
        assert not q["sufficient_now"]
        with pytest.raises(ValueError, match="gtm_insufficient_credits"):
            gen.start(d["wid"], d["vid"], req, idempotency_key="empty-balance-intent", input_digest=q["input_digest"],
                resource_digest=q["resource_digest"], ceiling_micro=q["ceiling_micro"])


def test_unknown_outcome_is_held_and_not_replayed(gtm_db):
    import httpx
    from app.gtm.god import generation as gen
    from app.core.jobs import FatalError
    from app.db.models import CreditAccount, Job
    d = gtm_db
    d["responses"] = [httpx.ReadTimeout("test transport response lost")]
    run, _ = start(d)
    job = work(d, run)
    done = view(d, run)
    assert done["state"] == "needs_input" and done["error_code"] == "gtm_outcome_uncertain"
    assert done["reserved_micro"] > 0 and done["consumed_micro"] == 0
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.reserved_micro > 0 and account.spent_micro == 0
        row = session.get(Job, job.id)
        row.status, row.attempts, row.locked_by = "leased", 2, "recovered-worker"
        session.commit()
    payload = {**job.payload, "_job_id": job.id, "_job_attempt": 2, "_worker_id": "recovered-worker"}
    with pytest.raises(FatalError, match="gtm_outcome_uncertain"):
        gen.run_diagnosis(payload)
    assert len(d["calls"]) == 1


def test_saved_receipt_restarts_without_provider_or_duplicate_outputs(gtm_db, monkeypatch):
    from app.gtm.god import generation as gen
    from app.core.jobs import RetryableError
    from app.db.models import GtmGodRun, Job
    from core.clock import utcnow
    d = gtm_db
    real = gen.journal.recover
    failed = False
    def drop_response(rid, key, payload=None):
        nonlocal failed
        if payload is None and not failed:
            failed = True
            raise RetryableError("gtm_settlement_pending")
        return real(rid, key, payload)
    monkeypatch.setattr(gen.journal, "recover", drop_response)
    run, _ = start(d)
    work(d, run)
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        job = session.get(Job, run["job_id"])
        assert job.status == "retry_wait"
        job.run_after, job.scheduled_for = utcnow(), utcnow()
        session.commit()
    monkeypatch.setenv("LLM_OFFLINE", "1")
    work(d, run)
    result = view(d, run)
    assert result["state"] == "completed" and result["consumed_micro"] > 0
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        assert len(session.scalars(select(GtmGodRun).where(GtmGodRun.id == run["run_id"])).all()) == 1


def test_founder_byok_is_zero_platform_debit(gtm_db, monkeypatch):
    from app.llm import founder_provider
    from llm_gateway import sole
    from app.db.models import CreditAccount, GtmGodRun
    d = gtm_db
    class Secrets:
        def put(self, **kwargs): return "test-scope-key-ref"
        def get(self, ref, **kwargs):
            assert ref == "test-scope-key-ref"
            return "test-founder-key"
    monkeypatch.setattr(founder_provider, "secret_store", lambda: Secrets())
    founder_provider.set_founder_config(workspace_id=d["wid"], provider="azure_foundry", model="DeepSeek-V4-Pro",
        base_url="https://founder-foundry.example.test/openai/v1", api_key="test-founder-key")
    original = sole._primary_chat
    async def byok(messages, **kwargs):
        assert kwargs["config"].source == "founder_override"
        assert kwargs["config"].api_key.get_secret_value() == "test-founder-key"
        return await original(messages, **kwargs)
    monkeypatch.setattr(sole, "byok_chat_completion", byok)
    run, q = start(d)
    assert q["payer"] == "founder" and q["ceiling_micro"] == 0
    work(d, run)
    result = view(d, run)
    assert result["state"] == "completed" and result["consumed_micro"] == 0
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.spent_micro == account.reserved_micro == 0
        row = session.get(GtmGodRun, run["run_id"])
        usage = json.loads(row.document_json)["_runtime"]["operations"]["diagnosis:1"]["usage"]
        assert usage["source"] == "byok" and usage["prompt_tokens"] == 300


def test_lease_loss_retains_paid_receipt_and_recovers_once(gtm_db, monkeypatch):
    from app.db.models import GtmGodRun, Job
    from core.clock import utcnow
    from datetime import timedelta
    from llm_gateway import sole
    d = gtm_db
    real = sole._primary_chat
    run, _ = start(d)
    async def lose_lease(*args, **kwargs):
        response = await real(*args, **kwargs)
        with d["factory"]() as session:
            row = session.get(Job, run["job_id"])
            row.locked_by = "replacement-worker"
            row.locked_at = utcnow() - timedelta(seconds=300)
            session.commit()
        return response
    monkeypatch.setattr(sole, "_primary_chat", lose_lease)
    work(d, run)
    with d["factory"]() as session:
        row = session.get(GtmGodRun, run["run_id"])
        assert row.state == "running" and not json.loads(row.document_json)["result"]
        assert json.loads(row.document_json)["_runtime"]["operations"]["diagnosis:1"]["state"] == "completed"
    monkeypatch.setenv("LLM_OFFLINE", "1")
    work(d, run)
    assert view(d, run)["state"] == "completed"
    assert len(d["calls"]) == 1


def test_http_signed_quote_start_read_and_legacy_mutations(gtm_db, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.security.test_core_signed_boundary import SECRET, headers
    d = gtm_db
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "gtm-test-vault-secret-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "fixture@example.test")
    from app.main import create_app
    path = f"/api/v1/ventures/{d['vid']}/gtm/god"
    def auth(suffix, method="GET", actor=None):
        return {**headers(path + suffix, method, actor or d["actor"]), "X-Workspace-Id": d["wid"]}
    with TestClient(create_app()) as client:
        assert client.post(path + "/quotes", json=REQUEST).status_code == 401
        response = client.post(path + "/quotes", json=REQUEST, headers=auth("/quotes", "POST"))
        assert response.status_code == 200, response.text
        q = response.json()
        body = {**REQUEST, **{k:q[k] for k in ("input_digest", "resource_digest", "ceiling_micro")}}
        response = client.post(path + "/runs", json=body, headers={**auth("/runs", "POST"), "Idempotency-Key":"api-intent-once"})
        assert response.status_code == 202, response.text
        run = response.json()
        replay = client.post(path + "/runs", json=body, headers={**auth("/runs", "POST"), "Idempotency-Key":"api-intent-once"})
        assert replay.status_code == 202 and replay.json()["run_id"] == run["run_id"]
        assert client.get(path + "/runs", headers=auth("/runs")).json()["runs"][0]["run_id"] == run["run_id"]
        assert client.get(path + "/runs/" + run["run_id"], headers=auth("/runs/" + run["run_id"])).status_code == 200
        assert client.get(path + "/runs", headers=auth("/runs", actor="foreign-person")).status_code in {403,404}
        for legacy, request in [("/gtm/god/start", {"idea":"legacy"}), ("/gtm/god/tick", {}), ("/gtm/god/tasks/old/complete", {})]:
            target = "/api/v1" + legacy
            rejected = client.post(target, json=request, headers={**headers(target,"POST",d["actor"]), "X-Workspace-Id":d["wid"]})
            assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "gtm_quote_required"
    assert not d["calls"]


@pytest.mark.parametrize("change", ["viewer", "archived"])
def test_authority_revoked_before_worker_cannot_spend(gtm_db, change):
    from app.db.models import WorkspaceMember, Venture
    from app.gtm.god import generation as gen
    from app.tenancy import bind
    from core.clock import utcnow
    d = gtm_db
    run, _ = start(d)
    with d["factory"]() as session:
        if change == "viewer":
            member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"], WorkspaceMember.person_id == d["actor"]))
            member.role = "viewer"
        else:
            session.get(Venture, d["vid"]).archived_at = utcnow()
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(ValueError, match="gtm_write_forbidden|gtm_venture_unavailable"):
            gen.quote(d["wid"], d["vid"], REQUEST)
    work(d, run)
    assert not d["calls"]


def test_explicit_provider_capture_mismatch_is_rejected_before_call(gtm_db, monkeypatch):
    from app.gtm.god import generation as gen
    d = gtm_db
    run, _ = start(d)
    real = gen.validate_usage
    def mismatch(row, doc, session, usage):
        return real(row, doc, session, {**usage, "account_id":"foreign-account"})
    monkeypatch.setattr(gen.journal, "validate_usage", mismatch)
    work(d, run)
    result = view(d, run)
    assert result["error_code"] == "gtm_resources_changed" and result["state"] == "failed"
    assert not d["calls"] and result["consumed_micro"] == 0


@pytest.mark.parametrize("retain_hold", [False, True])
def test_pre_call_rejection_requires_no_hold_before_fresh_approval(gtm_db, monkeypatch, retain_hold):
    from app.gtm.god import generation as gen
    from app.credits import gate
    from app.core.jobs import FatalError
    d = gtm_db
    run, q = start(d)
    real = gen.journal.validate_usage
    def reject(*args):
        raise FatalError("gtm_resources_changed")
    monkeypatch.setattr(gen.journal, "validate_usage", reject)
    if retain_hold:
        def unavailable(*args, **kwargs):
            raise RuntimeError("isolated release failure")
        monkeypatch.setattr(gate, "release", unavailable)
    work(d, run)
    result = view(d, run)
    assert not d["calls"] and result["calls_used"] == 0 and result["consumed_micro"] == 0
    assert result["state"] == ("needs_input" if retain_hold else "failed")
    assert (result["reserved_micro"] > 0) == retain_hold
    # Restart/read preserves the terminal record. Replaying the old approved
    # request cannot turn it into another provider attempt.
    replay, _ = start(d)
    assert replay["run_id"] == run["run_id"] and replay["state"] == result["state"]
    if not retain_hold:
        monkeypatch.setattr(gen.journal, "validate_usage", real)
        fresh, _ = start(d, key="fresh-explicit-approval")
        assert fresh["run_id"] != run["run_id"]
        work(d, fresh)
        assert view(d, fresh)["state"] == "completed" and len(d["calls"]) == 1


def test_balance_lost_after_enqueue_is_terminal_before_provider(gtm_db):
    from app.db.models import CreditAccount
    d = gtm_db
    run, _ = start(d)
    with d["factory"]() as session:
        account = session.get(CreditAccount, d["account"])
        account.granted_micro = 0
        session.commit()
    work(d, run)
    result = view(d, run)
    assert result["state"] == "failed" and result["calls_used"] == 0
    assert result["reserved_micro"] == 0 and not d["calls"]
    with d["factory"]() as session:
        session.get(CreditAccount, d["account"]).granted_micro = 100000000
        session.commit()
    fresh, _ = start(d, key="balance-restored-approval")
    work(d, fresh)
    assert view(d, fresh)["state"] == "completed" and len(d["calls"]) == 1
