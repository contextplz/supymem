"""Migrated PG, real worker/credits/journal; replace only the provider network seam."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from tests.integration.test_postgres_auth_security import pg_database as pg_database, concurrent
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db

@pytest.fixture()
def content_db(journey_db, monkeypatch):
    from app.content.brief import save
    from app.credits.accounts import ensure_account
    from app.tenancy import bind
    from tests.idea_fixtures import install_provider
    d = journey_db
    brief = {"product": {"name": "Order board", "one_liner": "An intake board for bakeries", "differentiators": ["Shared order notes"]},
        "icp": {"segments": [{"name": "Bakery owners", "pains": ["Order notes get lost"], "objections": [], "triggers": []}]},
        "competitors": [], "voice": {}, "claims": [], "cadence": {}}
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        d["brief"] = save(workspace_id=d["wid"], venture_id=d["vid"], data=brief)
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    from app.db.models import CreditAccount
    with d["factory"]() as session:
        row = session.get(CreditAccount, account.id)
        row.granted_micro = 100000000
        session.commit()
    d["responses"] = []
    def complete(messages, options):
        if d["responses"]:
            result = d["responses"].pop(0)
            if isinstance(result, Exception):
                raise result
        elif options["purpose"] == "content.topics":
            result = {"topics": [{"angle": "Keep bakery order notes together", "hook": "An order note should not get lost", "rationale": "Address the intake problem", "icp_segment": "Bakery owners", "intent": "value", "evidence": ["icp.segments.0"], "score": .8}]}
        elif options["purpose"] == "content.writer":
            result = {"body": "A bakery order needs a place for the customer's notes. Keep changes beside the order so the next person can see what was agreed.", "title": "", "visual": None}
        else:
            result = {"score": .85, "verdict": "publish", "problems": [], "unsupported_claims": [], "reads_as_ai": False, "claims_reviewed": True, "claim_checks": []}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)), finish_reason=d.get("finish_reason", "stop"))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450), model="DeepSeek-V4-Pro")
    d["calls"] = install_provider(monkeypatch, complete)
    yield d
    from app.db.models import Job
    with d["factory"]() as session:
        for job in session.scalars(select(Job).where(Job.workspace_id == d["wid"], Job.kind.in_(["content.topics", "content.draft"]))):
            job.status = "cancelled"
        session.commit()


def start(d, kind="topics", key="first-content-intent", **kwargs):
    from app.content.generation import quote, start
    from app.tenancy import bind
    req = {"kind": kind, "brief_id": d["brief"]["id"], **kwargs}
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        q = quote(d["wid"], d["vid"], req)
        result = start(d["wid"], d["vid"], req, idempotency_key=key, input_digest=q["input_digest"],
            resource_digest=q["resource_digest"], ceiling_micro=q["ceiling_micro"])
    return result, req, q


def work(d, result):
    from app.core.jobs import lease
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with d["factory"]() as session:
        jobs = lease(session, "content-worker", kinds=["content.topics", "content.draft"], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].id == result["job_id"]
    asyncio.run(_run_one(d["factory"], jobs[0], {jobs[0].id}))
    from app.db.models import Job
    with d["factory"]() as session:
        jobrow = session.get(Job, jobs[0].id)
        print("worker result", jobrow.status, jobrow.last_error)
    return jobs[0]


def view(d, rid):
    from app.content.generation import get
    from app.tenancy import bind
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        return get(d["wid"], d["vid"], rid)


def test_topics_draft_critic_receipts_and_editable_no_social_account(content_db):
    from app.db.models import ContentGenerationRun, ContentPost, ContentTopic, CreditAccount, Job
    d = content_db
    result, _, q = start(d)
    assert q["model"] == "DeepSeek-V4-Pro" and q["max_output_tokens"] == 8192
    work(d, result)
    done = view(d, result["run_id"])
    assert done["state"] == "completed" and done["consumed_micro"] > 0
    assert done["reserved_micro"] == 0
    draft, _, _ = start(d, "draft", key="draft-content-intent", platform="x")
    work(d, draft)
    done = view(d, draft["run_id"])
    assert done["state"] == "completed", done
    assert len(d["calls"]) == 3
    for _, options in d["calls"]:
        # The fixture observes the final provider seam, after route capture.
        assert options["captured_config"].timeout_seconds == 240
    with d["factory"]() as session:
        post = session.get(ContentPost, done["result"]["post_id"])
        assert post.state == "draft" and post.account_id is None and post.scheduled_for is None
        assert session.get(ContentTopic, post.topic_id).status == "drafted"
        assert session.get(Job, draft["job_id"]).status == "succeeded"
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.workspace_id == d["wid"] and account.spent_micro > 0 and account.reserved_micro == 0
        row = session.get(ContentGenerationRun, draft["run_id"])
        assert json.loads(row.document_json)["_runtime"]["operations"]["critic_1"]["state"] == "completed"
    assert "_runtime" not in json.dumps(done) and "operations" not in done


def test_provider_offline_never_falls_back(content_db, monkeypatch):
    d = content_db
    result, _, _ = start(d)
    monkeypatch.setenv("LLM_OFFLINE", "1")
    work(d, result)
    done = view(d, result["run_id"])
    assert done["state"] == "failed" and done["error_code"] == "content_provider_offline"
    assert not d["calls"] and not done["result"]


def test_invalid_or_truncated_output_charged_once(content_db):
    d = content_db
    d["finish_reason"] = "length"
    result, _, _ = start(d)
    work(d, result)
    done = view(d, result["run_id"])
    assert done["state"] == "failed" and done["error_code"] == "content_output_truncated"
    assert done["consumed_micro"] > 0 and len(d["calls"]) == 1


def test_idempotency_scope_and_quote_conflicts(content_db):
    from app.content import generation as gen
    from app.tenancy import bind
    from fastapi import HTTPException
    d = content_db
    result, req, q = start(d)
    def again():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return gen.start(d["wid"], d["vid"], req, idempotency_key="first-content-intent",
                input_digest=q["input_digest"], resource_digest=q["resource_digest"], ceiling_micro=q["ceiling_micro"])["run_id"]
    assert set(concurrent(again)) == {result["run_id"]}
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(ValueError, match="idempotency_conflict"):
            gen.start(d["wid"], d["vid"], {**req, "count": 2}, idempotency_key="first-content-intent",
                input_digest=q["input_digest"], resource_digest=q["resource_digest"], ceiling_micro=q["ceiling_micro"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor="foreign-person"):
        with pytest.raises(HTTPException):
            gen.get(d["wid"], d["vid"], result["run_id"])


def test_critic_repair_quality_refusal_and_manual_edit(content_db):
    from app.content import generation as gen
    from app.tenancy import bind
    from app.db.models import ContentPost
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    bad = {"body": "Too short.", "title": "", "visual": None}
    d["responses"] = [bad, bad, bad]
    run, _, _ = start(d, "draft", key="refused-draft-intent", platform="x")
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "refused" and result["calls_used"] == 3
    assert len(d["calls"]) == 4  # Lint failures do not consume critic calls.
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        post = gen.posts(d["wid"], d["vid"])[0]
        edited = gen.edit_post(d["wid"], d["vid"], post["id"], body="Keep each bakery order beside the customer's notes. The baker taking over can see the last change without asking around.", title="", expected_digest=post["edit_digest"])
        assert edited["state"] == "draft" and edited["model_review_required"] and not edited["scheduled"]
        with pytest.raises(ValueError, match="content_edit_conflict"):
            gen.edit_post(d["wid"], d["vid"], post["id"], body=post["body"], title="", expected_digest=post["edit_digest"])
    with d["factory"]() as session:
        row = session.get(ContentPost, post["id"])
        assert json.loads(row.critic_json)["previous_model_review_applies"] is False


def test_critic_reference_mismatch_revises_within_same_bounded_run(content_db):
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    writer = {"body": "A bakery order needs a place for the customer's notes. Keep changes beside the order so the next person can see what was agreed.",
              "title": "", "visual": None}
    critic = {"score": .9, "verdict": "publish", "problems": [], "unsupported_claims": [],
              "reads_as_ai": False, "claims_reviewed": True,
              "claim_checks": [{"draft_quote": writer["body"], "status": "supported",
                                "evidence": [{"source_id": "product", "quote": "Invented source excerpt"}],
                                "reason": "Claimed support"}]}
    clean = {"score": .9, "verdict": "publish", "problems": [], "unsupported_claims": [],
             "reads_as_ai": False, "claims_reviewed": True, "claim_checks": []}
    d["responses"] = [writer, critic, writer, clean]
    run, _, _ = start(d, "draft", key="critic-reference-repair", platform="linkedin")
    work(d, run)
    done = view(d, run["run_id"])
    assert done["state"] == "completed" and done["calls_used"] == 4
    assert done["consumed_micro"] > 0 and done["reserved_micro"] == 0
    assert done["result"]["attempts"] == 2


def test_brief_snapshot_concurrent_topic_claim_and_resource_change(content_db, monkeypatch):
    from app.content import generation as gen
    from app.content.brief import save
    from app.tenancy import bind
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    req = {"kind": "draft", "brief_id": d["brief"]["id"], "platform": "x"}
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        q = gen.quote(d["wid"], d["vid"], req)
    def compete(index):
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            try:
                return gen.start(d["wid"], d["vid"], req, idempotency_key="claim-" + str(index) + "-content-intent",
                    input_digest=q["input_digest"], resource_digest=q["resource_digest"], ceiling_micro=q["ceiling_micro"])
            except ValueError as exc:
                return str(exc)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(compete, [1, 2]))
    assert sum(isinstance(v, dict) for v in results) == 1
    assert "content_company_topic_required" in results
    run = next(v for v in results if isinstance(v, dict))
    saved = {k: v for k, v in d["brief"].items() if k in {"product", "icp", "competitors", "voice", "claims", "cadence"}}
    saved["product"] = {**saved["product"], "name": "Changed product"}
    save(workspace_id=d["wid"], venture_id=d["vid"], data=saved)
    work(d, run)
    assert view(d, run["run_id"])["brief_changed"]
    # Writer used the original paid input even though the brief was edited.
    writer = json.loads(d["calls"][1][0][-1]["content"])
    assert writer["product"]["name"] == "Order board"


@pytest.mark.parametrize("finish", [None, "tool_calls", "content_filter"])
def test_incomplete_finish_never_success(content_db, finish):
    d = content_db
    d["finish_reason"] = finish
    run, _, _ = start(d)
    work(d, run)
    done = view(d, run["run_id"])
    assert done["state"] == "failed" and done["error_code"] == "content_invalid_output"
    assert done["consumed_micro"] > 0 and len(d["calls"]) == 1


def test_insufficient_and_changed_resources_block_before_provider(content_db, monkeypatch):
    from app.content import generation as gen
    from app.db.models import CreditAccount
    from app.tenancy import bind
    d = content_db
    run, _, _ = start(d)
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "unpriced-changed-model")
    work(d, run)
    assert not d["calls"]
    assert view(d, run["run_id"])["state"] in {"failed", "needs_input"}
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro")
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        account.granted_micro = account.spent_micro + account.reserved_micro
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        req = {"kind": "topics", "brief_id": d["brief"]["id"]}
        q = gen.quote(d["wid"], d["vid"], req)
        assert not q["sufficient_now"]
        with pytest.raises(ValueError, match="content_insufficient_credits"):
            gen.start(d["wid"], d["vid"], req, idempotency_key="empty-balance-intent", input_digest=q["input_digest"],
                resource_digest=q["resource_digest"], ceiling_micro=q["ceiling_micro"])


def test_unknown_outcome_is_held_and_not_replayed(content_db):
    import httpx
    from app.content import generation as gen
    from app.core.jobs import FatalError
    from app.db.models import CreditAccount, Job
    d = content_db
    d["responses"] = [httpx.ReadTimeout("test transport response lost")]
    run, _, _ = start(d)
    job = work(d, run)
    done = view(d, run["run_id"])
    assert done["state"] == "needs_input" and done["error_code"] == "content_outcome_uncertain"
    assert done["reserved_micro"] > 0 and done["consumed_micro"] == 0
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.reserved_micro > 0 and account.spent_micro == 0
        row = session.get(Job, job.id)
        row.status, row.attempts, row.locked_by = "leased", 2, "recovered-worker"
        session.commit()
    payload = {**job.payload, "_job_id": job.id, "_job_attempt": 2, "_worker_id": "recovered-worker"}
    with pytest.raises(FatalError, match="content_outcome_uncertain"):
        gen.run_topics(payload)
    assert len(d["calls"]) == 1


def test_saved_receipt_restarts_without_provider_or_duplicate_outputs(content_db, monkeypatch):
    from app.content import generation as gen
    from app.core.jobs import RetryableError
    from app.db.models import ContentTopic, Job
    from core.clock import utcnow
    d = content_db
    real = gen.journal.recover
    failed = False
    def drop_response(rid, key, payload=None):
        nonlocal failed
        if gen.journal.operation(rid, key).get("state") in {"responded", "completed"} and not failed:
            assert payload is not None  # Recovery retains its actual lease/source fence.
            failed = True
            raise RetryableError("content_settlement_pending")
        return real(rid, key, payload)
    monkeypatch.setattr(gen.journal, "recover", drop_response)
    run, _, _ = start(d)
    work(d, run)
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        job = session.get(Job, run["job_id"])
        assert job.status == "retry_wait"
        job.run_after, job.scheduled_for = utcnow(), utcnow()
        session.commit()
    monkeypatch.setenv("LLM_OFFLINE", "1")
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "completed" and result["consumed_micro"] > 0
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        assert len(session.scalars(select(ContentTopic).where(ContentTopic.generation_run_id == run["run_id"])).all()) == 1


def test_provider_overage_capped_and_replayed_once(content_db, monkeypatch):
    from llm_gateway import sole
    from app.content import generation as gen
    from app.core.jobs import FatalError
    from app.db.models import ContentGenerationRun, CreditAccount
    d = content_db
    real = sole._primary_chat
    async def excessive(*args, **kwargs):
        response = await real(*args, **kwargs)
        response.usage = SimpleNamespace(prompt_tokens=10**8, completion_tokens=10**8, total_tokens=2 * 10**8)
        return response
    monkeypatch.setattr(sole, "_primary_chat", excessive)
    run, _, q = start(d)
    work(d, run)
    done = view(d, run["run_id"])
    assert done["error_code"] == "content_provider_usage_above_ceiling" and done["state"] != "completed"
    assert 0 < done["consumed_micro"] <= q["ceiling_micro"]
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, run["run_id"])
        usage = json.loads(row.document_json)["_runtime"]["operations"]["topics"]["usage"]
        assert usage["provider_actual_micro"] > usage["actual_micro"] and usage["provider_overage_micro"] > 0
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        charged = account.spent_micro
        assert charged == done["consumed_micro"] and account.reserved_micro == 0
    with pytest.raises(FatalError, match="content_provider_usage_above_ceiling"):
        gen.journal.recover(run["run_id"], "topics")
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.spent_micro == charged


def test_founder_byok_is_zero_platform_debit(content_db, monkeypatch):
    from app.llm import founder_provider
    from llm_gateway import sole
    from app.db.models import CreditAccount, ContentGenerationRun
    d = content_db
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
    run, _, q = start(d)
    assert q["payer"] == "founder" and q["ceiling_micro"] == 0
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "completed" and result["consumed_micro"] == 0
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.spent_micro == account.reserved_micro == 0
        row = session.get(ContentGenerationRun, run["run_id"])
        usage = json.loads(row.document_json)["_runtime"]["operations"]["topics"]["usage"]
        assert usage["source"] == "byok" and usage["prompt_tokens"] == 300


def test_lease_loss_retains_paid_receipt_and_recovers_once(content_db, monkeypatch):
    from app.db.models import ContentGenerationRun, Job
    from core.clock import utcnow
    from datetime import timedelta
    from llm_gateway import sole
    d = content_db
    real = sole._primary_chat
    run, _, _ = start(d)
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
        row = session.get(ContentGenerationRun, run["run_id"])
        assert row.state == "running" and not json.loads(row.document_json)["result"]
        assert json.loads(row.document_json)["_runtime"]["operations"]["topics"]["state"] == "completed"
    monkeypatch.setenv("LLM_OFFLINE", "1")
    work(d, run)
    assert view(d, run["run_id"])["state"] == "completed"
    assert len(d["calls"]) == 1


def test_schema_evidence_nan_and_missing_choice_fail_closed(content_db, monkeypatch):
    from app.content import generation_schema as schema
    from llm_gateway import sole
    d = content_db
    valid = {"angle": "Order notes", "hook": "Keep notes", "rationale": "Help owners", "icp_segment": "Bakery owners",
             "intent": "value", "evidence": ["unverified.external"], "score": .8}
    snap = {"brief": d["brief"], "count": 2, "avoid": []}
    with pytest.raises(ValueError, match="content_unknown_evidence"):
        schema.parse("topics", json.dumps({"topics": [valid]}), snap)
    valid["evidence"] = ["product"]
    valid["score"] = float("nan")
    with pytest.raises(ValueError):
        schema.parse("topics", json.dumps({"topics": [valid]}), snap)
    original = sole._primary_chat
    async def no_choice(*args, **kwargs):
        response = await original(*args, **kwargs)
        response.choices = []
        return response
    monkeypatch.setattr(sole, "_primary_chat", no_choice)
    run, _, _ = start(d)
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "failed" and result["consumed_micro"] > 0


def test_signed_http_contract_quote_enqueue_restore_and_foreign_denial(content_db, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import reset_nonce_store, sign_assertion
    from tests.integration.test_postgres_auth_security import SECRET
    d = content_db
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "postgres-test-vault-secret-at-least-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "content-admin@example.test")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    reset_nonce_store()
    from app.main import create_app
    app = create_app()
    # Deliberately no lifespan: no scheduler/background worker is started.
    client = TestClient(app)
    def send(method, path, body=None, actor=None):
        token = sign_assertion(SECRET, sub=actor or d["actor"], email="", role="user", path=path, method=method)
        return client.request(method, path + "?workspace_id=" + d["wid"], json=body,
            headers={"X-COS-Assertion": token, "X-Workspace-Id": d["wid"]})
    req = {"venture_id": d["vid"], "brief_id": d["brief"]["id"], "kind": "topics", "count": 12}
    response = send("POST", "/api/v1/content/generation/quote", req)
    assert response.status_code == 200, response.text
    q = response.json()
    body = {**req, "idempotency_key": "http-content-intent", **{k: q[k] for k in ("input_digest", "resource_digest", "ceiling_micro")}}
    response = send("POST", "/api/v1/content/topics/refill", body)
    assert response.status_code == 202, response.text
    run = response.json()
    repeated = send("POST", "/api/v1/content/topics/refill", body)
    assert repeated.status_code == 202 and repeated.json()["run_id"] == run["run_id"]
    path = "/api/v1/content/generation/runs/" + run["run_id"]
    token = sign_assertion(SECRET, sub=d["actor"], email="", role="user", path=path, method="GET")
    result = client.get(path, params={"workspace_id": d["wid"], "venture_id": d["vid"]}, headers={"X-COS-Assertion": token})
    assert result.status_code == 200 and result.json()["state"] == "queued", result.text
    assert "_runtime" not in result.text and "response_digest" not in result.text
    assert send("POST", "/api/v1/content/generation/quote", req, actor="foreign-content-user").status_code == 404
    assert send("POST", "/api/v1/content/draft", {"platform": "x"}).status_code == 422
    # Another authenticated member has access to this exact company, but did
    # not approve the first member's quote. A late/recovered envelope must not
    # become a second paid intent under the new signed-in member.
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember, ContentGenerationRun
    from core.ids import new_id
    from core.clock import utcnow
    member = new_id("usr")
    ensure_subject_person(member)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id=new_id("wm"), workspace_id=d["wid"], person_id=member,
            role="contributor", can_approve=0, created_at=utcnow()))
        session.commit()
    switched = send("POST", "/api/v1/content/topics/refill", body, actor=member)
    assert switched.status_code == 409 and switched.json()["detail"]["code"] == "content_quote_changed", switched.text
    own_quote = send("POST", "/api/v1/content/generation/quote", req, actor=member)
    assert own_quote.status_code == 200, own_quote.text
    assert own_quote.json()["input_digest"] != q["input_digest"]
    with d["factory"]() as session:
        rows = list(session.scalars(select(ContentGenerationRun).where(ContentGenerationRun.workspace_id == d["wid"])))
        assert len(rows) == 1 and rows[0].actor_id == d["actor"]
    assert not d["calls"]
    client.close()


def test_provider_route_remains_pinned_when_settings_change_after_reserve(content_db, monkeypatch):
    from app.content import generation as gen
    from app.llm import founder_provider
    from llm_gateway import sole
    from app.db.models import ContentGenerationRun
    d = content_db
    original_network = sole._primary_chat
    dialed = []
    class Client:
        def __init__(self, config):
            self.config = config
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self
        async def create(self, **kwargs):
            dialed.append((self.config.base_url, self.config.deployment_pro, kwargs["model"]))
            assert self.config.api_key.get_secret_value() == "test-provider-key"
            return await original_network(kwargs["messages"], purpose="content.topics")
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda config: (Client(config), config))
    async def wrong_byok(*args, **kwargs):
        raise AssertionError("The later workspace override must not replace the charged platform route")
    monkeypatch.setattr(sole, "byok_chat_completion", wrong_byok)
    class Secrets:
        def put(self, **kwargs): return "new-foundry-key-version"
        def get(self, ref, **kwargs): return "different-founder-key"
    monkeypatch.setattr(founder_provider, "secret_store", lambda: Secrets())
    begin = gen.journal.begin
    def change_after_hold(*args, **kwargs):
        begin(*args, **kwargs)
        founder_provider.set_founder_config(workspace_id=d["wid"], provider="azure_foundry", model="DeepSeek-V4-Pro",
            base_url="https://different-founder.example.test/openai/v1", api_key="different-founder-key")
    monkeypatch.setattr(gen.journal, "begin", change_after_hold)
    run, _, q = start(d)
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "completed" and result["consumed_micro"] > 0
    assert dialed == [("https://foundry.example.test/openai/v1", "DeepSeek-V4-Pro", "DeepSeek-V4-Pro")]
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, run["run_id"])
        usage = json.loads(row.document_json)["_runtime"]["operations"]["topics"]["usage"]
        assert usage["provider"] == "azure_foundry" and usage["source"] == "platform"


@pytest.mark.parametrize("domain", ["Freight dispatch teams", "Family pottery classes"], ids=["b2b", "consumer"])
def test_critic_feedback_repair_and_same_workspace_company_isolation(content_db, domain):
    from app.content import generation as gen
    from app.db.models import Venture, ContentBrief, ContentPost, ContentTopic
    from app.tenancy import bind
    from core.ids import new_id
    from core.clock import utcnow
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    text = {"body": "Keep each bakery order beside the customer's notes. The baker taking over can see the last change without asking around.", "title": "", "visual": None}
    revise = {"score": .4, "verdict": "revise", "problems": ["Show the handoff between the order taker and baker."], "unsupported_claims": [], "reads_as_ai": False, "claims_reviewed": True, "claim_checks": []}
    d["responses"] = [text, revise, text]
    run, _, _ = start(d, "draft", key="critic-repair-intent", platform="x")
    work(d, run)
    done = view(d, run["run_id"])
    assert done["state"] == "completed" and done["calls_used"] == 4
    assert json.loads(d["calls"][3][0][-1]["content"])["fix_these"] == revise["problems"]
    sibling, topic_id, post_id = new_id("ven"), new_id("ctp"), new_id("cps")
    legacy_brief_id, legacy_topic_id, legacy_post_id = new_id("cbf"), new_id("ctp"), new_id("cps")
    with d["factory"]() as session:
        session.add(Venture(id=sibling, workspace_id=d["wid"], name=domain, raw_idea=domain, stage="draft", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
        session.add(ContentBrief(id=legacy_brief_id, workspace_id=d["wid"], venture_id=None, created_at=utcnow()))
        session.flush()
        session.add(ContentTopic(id=topic_id, workspace_id=d["wid"], brief_id=d["brief"]["id"], angle="Company manual note", created_at=utcnow()))
        session.add(ContentTopic(id=legacy_topic_id, workspace_id=d["wid"], brief_id=legacy_brief_id, angle="Legacy manual note", created_at=utcnow()))
        session.flush()
        session.add(ContentPost(id=post_id, topic_id=topic_id, workspace_id=d["wid"], platform="x", body=f"Company content about {domain}.", state="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(ContentPost(id=legacy_post_id, topic_id=legacy_topic_id, workspace_id=d["wid"], platform="x", body=f"Legacy content about {domain}.", state="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        rows = {row["id"]: row for row in gen.posts(d["wid"], d["vid"])}
        assert set(rows) == {done["result"]["post_id"], post_id, legacy_post_id}
        assert rows[post_id]["provenance"] == "chat_draft"
        assert rows[legacy_post_id]["provenance"] == "legacy_unassigned"
    with bind(workspace_id=d["wid"], venture_id=sibling, actor=d["actor"]):
        with pytest.raises(ValueError, match="content_not_found"):
            gen.get(d["wid"], sibling, run["run_id"])
        rows = gen.posts(d["wid"], sibling)
        assert [r["id"] for r in rows] == [legacy_post_id] and rows[0]["provenance"] == "legacy_unassigned"
        with pytest.raises(ValueError, match="content_not_found"):
            gen.edit_post(d["wid"], sibling, done["result"]["post_id"], body=text["body"], title="", expected_digest="0" * 64)
        with pytest.raises(ValueError, match="content_not_found"):
            gen.edit_post(d["wid"], sibling, post_id, body=text["body"], title="", expected_digest="0" * 64)


def test_verbose_critic_uses_existing_writer_budget_and_never_approves_itself(content_db):
    from app.db.models import ContentGenerationRun, ContentPost
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    draft = {"body": "Keep bakery order notes beside each order for review.", "title": "", "visual": None}
    verbose = {"score": .99, "verdict": "publish", "problems": [], "unsupported_claims": [],
        "reads_as_ai": False, "claims_reviewed": True,
        "claim_checks": [{"draft_quote": draft["body"], "status": "supported", "evidence": [],
            "reason": "Long explanation. " * 25} for _ in range(11)]}
    d["responses"] = [draft, verbose, draft]
    run, _, quote = start(d, "draft", key="verbose-critic-repair-intent", platform="x")
    work(d, run)
    done = view(d, run["run_id"])
    assert done["state"] == "completed" and done["calls_used"] == 4
    assert 0 < done["consumed_micro"] <= quote["ceiling_micro"]
    assert done["reserved_micro"] == 0
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, run["run_id"])
        first = json.loads(row.document_json)["_runtime"]["operations"]["critic_1"]
        assert first["state"] == "completed" and first["usage"]["settled"] is True
        post = session.get(ContentPost, done["result"]["post_id"])
        assert post.state == "draft" and post.scheduled_for is None
    assert "Exact claim and source references did not validate." in json.loads(
        d["calls"][3][0][-1]["content"])["fix_these"]


@pytest.mark.parametrize("reader", ["get", "recent"])
def test_stale_status_read_cannot_overwrite_worker_completion(content_db, monkeypatch, reader):
    """Pause a real stale SQLAlchemy reader exactly before its former autoflush."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident
    from app.content import generation as gen
    from app.db.models import ContentGenerationRun, Job
    from app.tenancy import bind
    from core.clock import utcnow
    d = content_db
    run, _, _ = start(d)
    with d["factory"]() as session:
        session.get(Job, run["job_id"]).status = "failed_dead"
        session.commit()
    arrived, release = Event(), Event()
    main_thread = get_ident()
    original = gen._view
    def paused_view(row, session):
        if get_ident() == main_thread:
            return original(row, session)
        scalar = session.scalar
        def at_brief_lookup(statement, *args, **kwargs):
            # _view's scalar query follows the cached row/job status evaluation.
            # The old implementation dirtied that row here, then autoflushed it
            # after the worker's fresh completion and overwrote completed.
            arrived.set()
            assert release.wait(20), "bounded reader rendezvous timed out"
            return scalar(statement, *args, **kwargs)
        session.scalar = at_brief_lookup
        return original(row, session)
    monkeypatch.setattr(gen, "_view", paused_view)
    def read_status():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return gen.get(d["wid"], d["vid"], run["run_id"]) if reader == "get" else gen.recent(d["wid"], d["vid"])
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(read_status)
        assert arrived.wait(20), "reader never reached the stale snapshot boundary"
        try:
            # Model an operator-authorized retry of a definitely unstarted job.
            with d["factory"]() as session:
                row = session.get(Job, run["job_id"])
                row.status, row.run_after, row.scheduled_for = "scheduled", utcnow(), utcnow()
                session.commit()
            work(d, run)
        finally:
            release.set()
        stale = future.result(timeout=20)
    stale = stale if reader == "get" else stale["runs"][0]
    assert stale["state"] == "failed"  # Describing an old snapshot is harmless.
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, run["run_id"])
        assert row.state == "completed" and row.error_code is None
        assert len(json.loads(row.document_json)["result"]["topic_ids"]) == 1
    assert view(d, run["run_id"])["state"] == "completed" and len(d["calls"]) == 1


@pytest.mark.parametrize("timing", ["captured_different_route_then_restored", "changed_after_capture_before_check"])
@pytest.mark.parametrize("payer", ["platform", "founder"])
def test_captured_route_must_match_quote_inside_begin(content_db, monkeypatch, timing, payer):
    """A→B→A must not make captured B appear to satisfy the approved A route."""
    from app.content import generation as gen
    from app.llm import founder_provider
    from app.research import metered
    from app.db.models import ContentGenerationRun, CreditAccount, CreditReservation, FounderLLMSetting
    from llm_gateway import sole
    d = content_db
    class Secrets:
        def put(self, **kwargs): return kwargs["value"] + "-ref"
        def get(self, ref, **kwargs): return ref.removesuffix("-ref")
        def forget(self, ref): pass
    monkeypatch.setattr(founder_provider, "secret_store", lambda: Secrets())
    # Same provider/model/endpoint; the changed credential version alone is
    # enough to invalidate the paid route. This catches subtler ABA changes.
    if payer == "founder":
        founder_provider.set_founder_config(workspace_id=d["wid"], provider="azure_foundry", model="DeepSeek-V4-Pro",
            base_url="https://founder.example.test/openai/v1", api_key="founder-key-version-a")
    else:
        monkeypatch.setenv("LLM_BASE_URL", "https://platform-a.example.test/openai/v1")
    run, _, _ = start(d)
    unexpected_dials = []
    async def forbidden_byok(*args, **kwargs):
        unexpected_dials.append(True)
        raise AssertionError("Rejected route cannot dial any provider")
    monkeypatch.setattr(sole, "byok_chat_completion", forbidden_byok)
    reservations = []
    validate = gen.journal.validate_usage
    def capture_hold(row, doc, session, usage):
        reservations.append(usage.get("reservation_id"))
        if payer == "platform":
            hold = session.get(CreditReservation, usage["reservation_id"])
            assert hold.status == "active" and hold.amount_micro > 0
        validate(row, doc, session, usage)
    monkeypatch.setattr(gen.journal, "validate_usage", capture_hold)
    def set_ref(version):
        if payer == "platform":
            monkeypatch.setenv("LLM_BASE_URL", f"https://platform-{version}.example.test/openai/v1")
        else:
            with d["factory"]() as session:
                setting = session.scalar(select(FounderLLMSetting).where(FounderLLMSetting.workspace_id == d["wid"]))
                setting.api_key_ref = "founder-key-version-" + version + "-ref"
                session.commit()
    real_perform = metered.perform
    def swap_at_reservation(*args, **kwargs):
        # synthesize has already captured metadata+secret here, before begin.
        set_ref("a" if timing == "captured_different_route_then_restored" else "b")
        return real_perform(*args, **kwargs)
    monkeypatch.setattr(metered, "perform", swap_at_reservation)
    if timing == "captured_different_route_then_restored":
        set_ref("b")
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "needs_input" and result["error_code"] == "content_resources_changed"
    assert result["consumed_micro"] == result["reserved_micro"] == 0 and not d["calls"] and not unexpected_dials
    assert len(reservations) == 1
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, run["run_id"])
        assert json.loads(row.document_json)["_runtime"]["calls_used"] == 0
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.spent_micro == account.reserved_micro == 0
        if payer == "platform":
            assert session.get(CreditReservation, reservations[0]).status == "released"


@pytest.mark.parametrize("uncertain", [False, True])
def test_terminated_topic_owner_only_blocks_when_outcome_is_uncertain(content_db, uncertain):
    import httpx
    from app.content import generation as gen
    from app.db.models import ContentGenerationRun, Job
    from app.tenancy import bind
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    old, _, _ = start(d, "draft", key="first-topic-claim", platform="x")
    if uncertain:
        d["responses"] = [httpx.ReadTimeout("response lost after provider accepted")]
        work(d, old)
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, old["run_id"])
        # The worker did not commit its terminal domain state before dying.
        row.state = "running" if uncertain else "queued"
        session.get(Job, old["job_id"]).status = "failed_dead"
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        if uncertain:
            with pytest.raises(ValueError, match="content_company_topic_required"):
                gen.quote(d["wid"], d["vid"], {"kind": "draft", "brief_id": d["brief"]["id"], "platform": "x"})
            result = gen.get(d["wid"], d["vid"], old["run_id"])
            assert result["state"] == "needs_input" and result["reserved_micro"] > 0
        else:
            # No GET/reconcile mutation is required to release the stale claim.
            fresh, _, _ = start(d, "draft", key="new-reviewed-topic-intent", platform="x")
            assert fresh["run_id"] != old["run_id"] and fresh["state"] == "queued"
            assert gen.get(d["wid"], d["vid"], old["run_id"])["state"] == "failed"
    with d["factory"]() as session:
        row = session.get(ContentGenerationRun, old["run_id"])
        assert row.state == ("running" if uncertain else "queued")


def test_unsupported_product_claims_are_refused_and_paid_usage_remains_recorded(content_db):
    from app.content import generation as gen
    from app.db.models import ContentGenerationRun, ContentPost
    from app.tenancy import bind
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    body = "The booking product automatically accepts requests and prevents double-booking, keeping every booking confirmed without owner review."
    draft = {"body": body, "title": "", "visual": None}
    critic = {"score": .99, "verdict": "publish", "problems": [], "unsupported_claims": [], "reads_as_ai": False,
        "claims_reviewed": True, "claim_checks": [{"draft_quote": body, "status": "unsupported", "evidence": [],
            "reason": "No supplied source supports automatic acceptance or a reliability guarantee."}]}
    d["responses"] = [draft, critic, draft, critic, draft, critic]
    run, _, quote = start(d, "draft", key="unsupported-product-claim-intent", platform="x")
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "refused" and result["calls_used"] == 6
    assert 0 < result["consumed_micro"] <= quote["ceiling_micro"]
    assert result["reserved_micro"] == 0
    with d["factory"]() as session:
        stored = session.get(ContentGenerationRun, run["run_id"])
        document = json.loads(stored.document_json)
        assert document["_runtime"]["input"]["authoring_contract"] == "content-grounded-v2"
        post = session.get(ContentPost, result["result"]["post_id"])
        assert post.state == "refused" and post.body == body
        assert json.loads(post.critic_json)["claim_checks"][0]["status"] == "unsupported"
        assert document["_runtime"]["operations"]["critic_3"]["usage"]["settled"] is True
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        rows = gen.posts(d["wid"], d["vid"])
        assert any(row["id"] == result["result"]["post_id"] and row["state"] == "refused" for row in rows)
    assert any("Remove or qualify" in item for item in json.loads(d["calls"][3][0][-1]["content"])["fix_these"])


def test_edited_brief_topics_count_matches_draft_quote_scope(content_db):
    from app.content import brief, director, generation
    from app.tenancy import bind
    d = content_db
    planned, _, _ = start(d)
    work(d, planned)
    old = d["brief"]
    assert director.queued_count(d["wid"], venture_id=d["vid"], brief_id=old["id"]) == 1
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        edited = brief.save(workspace_id=d["wid"], venture_id=d["vid"], data={
            "product": {"name": "Order board", "one_liner": "An early intake concept for bakeries"},
            "icp": {"segments": [{"name": "Bakery owners", "pains": ["Order notes get lost"]}]},
            "competitors": [], "voice": {}, "claims": [], "cadence": {}})
        assert director.queued_count(d["wid"], venture_id=d["vid"], brief_id=edited["id"]) == 0
        with pytest.raises(ValueError, match="content_company_topic_required"):
            generation.quote(d["wid"], d["vid"], {"kind": "draft", "brief_id": edited["id"], "platform": "reddit"})
        quote = generation.quote(d["wid"], d["vid"], {"kind": "topics", "brief_id": edited["id"]})
        assert quote["brief_id"] == edited["id"]
    assert director.queued_count(d["wid"], venture_id="other-venture", brief_id=old["id"]) == 0
    assert director.queued_count("other-workspace", venture_id=d["vid"], brief_id=old["id"]) == 0


def test_reddit_quote_skips_promotional_topic_before_paid_execution(content_db):
    from app.content import generation as gen
    from app.db.models import ContentTopic
    from app.tenancy import bind
    d = content_db
    planned, _, _ = start(d, platform='reddit')
    work(d, planned)
    topic_id = view(d, planned['run_id'])['result']['topic_ids'][0]
    with d['factory']() as session:
        value = session.get(ContentTopic, topic_id)
        session.add(ContentTopic(id='promo_topic', workspace_id=value.workspace_id,
            brief_id=value.brief_id, generation_run_id=value.generation_run_id,
            angle='Promote product', hook='Join now', rationale='Promotion',
            icp_segment=value.icp_segment, intent='promo', evidence_json=value.evidence_json,
            score=1.0, status='queued', created_at=value.created_at))
        session.commit()
    before = len(d['calls'])
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        quote = gen.quote(d['wid'], d['vid'], {'kind': 'draft', 'platform': 'reddit'})
        assert quote['topic_id'] == topic_id
        with pytest.raises(ValueError, match='content_company_topic_required'):
            gen.quote(d['wid'], d['vid'], {'kind': 'draft', 'platform': 'reddit', 'topic_id': 'promo_topic'})
        linkedin = gen.quote(d['wid'], d['vid'], {'kind': 'draft', 'platform': 'linkedin'})
        assert linkedin['topic_id'] == 'promo_topic'
    assert len(d['calls']) == before
    with d['factory']() as session:
        row = session.get(ContentTopic, topic_id)
        row.status = 'drafted'
        session.commit()
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        with pytest.raises(ValueError, match='content_company_topic_required'):
            gen.quote(d['wid'], d['vid'], {'kind': 'draft', 'platform': 'reddit'})
        fresh = gen.quote(d['wid'], d['vid'], {'kind': 'topics', 'platform': 'reddit'})
        assert fresh['input_digest'] != gen.quote(d['wid'], d['vid'], {'kind': 'topics'})['input_digest']


@pytest.mark.parametrize('platform', [[], {}, 42])
def test_malformed_topic_platform_is_rejected_before_quote(content_db, platform):
    from app.content.generation import quote
    from app.tenancy import bind
    d = content_db
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        with pytest.raises(ValueError, match='content_platform_invalid'):
            quote(d['wid'], d['vid'], {'kind': 'topics', 'platform': platform})
    assert not d['calls']


def test_unregistered_founder_conversations_refuse_after_paid_bounded_writer_attempts(content_db):
    from app.db.models import ContentGenerationRun, ContentPost
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    body = "I keep hearing that bakery order notes get lost. What is your current workflow? I do not have a finished product and would like to learn without any private customer records."
    draft = {"body": body, "title": "", "visual": None}
    d["responses"] = [draft, draft, draft]
    run, _, quote = start(d, "draft", key="unregistered-founder-research", platform="linkedin")
    work(d, run)
    result = view(d, run["run_id"])
    assert result["state"] == "refused" and result["calls_used"] == 3
    assert 0 < result["consumed_micro"] <= quote["ceiling_micro"] and result["reserved_micro"] == 0
    assert len(d["calls"]) == 4 and all(options["purpose"] != "content.critic" for _, options in d["calls"])
    with d["factory"]() as session:
        document = json.loads(session.get(ContentGenerationRun, run["run_id"]).document_json)
        post = session.get(ContentPost, result["result"]["post_id"])
        assert post.state == "refused" and post.body == body and post.scheduled_for is None
        assert "unregistered_founder_research" in json.loads(post.lint_json)["reasons"]
        assert all(document["_runtime"]["operations"][f"draft_{attempt}"]["usage"]["settled"] is True for attempt in range(1, 4))
    assert "unregistered_founder_research" in str(d["calls"][2][0])
