from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from tests.company_os_harness import load_company_os_app

DOMAINS = ("Warehouse scheduling for dispatch teams", "Trail journals for weekend hikers")


@pytest.mark.parametrize("account", ["invoice-audits.example", "ceramics-club.example"])
def test_redis_timeout_preserves_local_admission_and_blocks(migration_db, monkeypatch, caplog, account):
    from app.connections import composio_pacing as pacing
    from redis.exceptions import TimeoutError as RedisTimeoutError

    url = "redis://pacing-private.invalid"
    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("COMPOSIO_ORG_RPM", "6000")
    monkeypatch.setenv("COMPOSIO_ACCOUNT_RPM", "5")
    monkeypatch.setattr(pacing, "_BUCKETS", {})
    monkeypatch.setattr(pacing, "_REDIS_DOWN_UNTIL", 0, raising=False)
    monkeypatch.setattr(pacing, "_REDIS_WARNING_AT", 0, raising=False)
    clock, calls = [100.0], []
    monkeypatch.setattr(pacing.time, "monotonic", lambda: clock[0])

    class RedisClient:
        def eval(self, *arguments):
            calls.append(arguments)
            if clock[0] < 190:
                raise RedisTimeoutError(f"Timeout connecting to {url}")
            return 0

    monkeypatch.setattr(pacing, "_client", lambda _url: RedisClient())
    caplog.set_level("WARNING", logger=pacing.__name__)
    pacing.acquire(account)
    for _ in range(3):
        pacing.acquire(account, background=True)
    with pytest.raises(pacing.AdmissionLimited):
        pacing.acquire(account, background=True)
    pacing.acquire(account)
    with pytest.raises(pacing.AdmissionLimited):
        pacing.acquire(account)
    assert len(calls) == 1

    blocked_account = f"blocked:{account}"
    pacing.acquire(blocked_account)
    with pytest.raises(pacing.RateLimited):
        pacing.reject(blocked_account, 90)
    with pytest.raises(pacing.AdmissionLimited) as blocked:
        pacing.acquire(blocked_account)
    assert blocked.value.retry_after_s == 90
    clock[0] = 129.0
    pacing.acquire(f"before-probe:{account}", background=True)
    assert len(calls) == 1
    clock[0] = 130.0
    pacing.acquire(f"probe:{account}", background=True)
    assert len(calls) == 2
    assert [record.getMessage() for record in caplog.records] == ["composio_pacing_redis_unavailable"]

    clock[0] = 160.0
    with pytest.raises(pacing.RateLimited):
        pacing.reject(blocked_account, 90)
    with pytest.raises(pacing.AdmissionLimited) as blocked:
        pacing.acquire(blocked_account)
    assert blocked.value.retry_after_s == 90
    assert len(calls) == 3
    assert [record.getMessage() for record in caplog.records] == ["composio_pacing_redis_unavailable"] * 2
    assert url not in caplog.text and "redis://" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)

    clock[0] = 190.0
    buckets = pacing._BUCKETS.copy()
    pacing.acquire(account, background=True)
    assert calls[-1] == (pacing._SCRIPT, 2, *pacing._keys(account), 6000, 5, 1)
    assert len(calls) == 4 and pacing._BUCKETS == buckets


@pytest.mark.parametrize("account", ["invoice-audits.example", "ceramics-club.example"])
def test_background_pacing_preserves_send_capacity(migration_db, monkeypatch, account):
    from app.connections import composio_pacing as pacing

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("COMPOSIO_ORG_RPM", "6000")
    monkeypatch.setenv("COMPOSIO_ACCOUNT_RPM", "5")
    monkeypatch.setattr(pacing, "_BUCKETS", {})
    monkeypatch.setattr(pacing.time, "monotonic", lambda: 100.0)
    for _ in range(4):
        pacing.acquire(account, background=True)
    with pytest.raises(pacing.RateLimited):
        pacing.acquire(account, background=True)
    pacing.acquire(account)
    with pytest.raises(pacing.RateLimited):
        pacing.reject(account, 90)
    with pytest.raises(pacing.RateLimited):
        pacing.acquire(account)


def test_composio_shared_pacing_account_budgets_and_429_queue_retry(migration_db, monkeypatch):
    from app.connections import composio_pacing as pacing
    from app.core import jobs
    from app.db.models import Job
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind
    from types import SimpleNamespace
    factory, scopes = migration_db
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("COMPOSIO_ORG_RPM", "240")
    monkeypatch.setenv("COMPOSIO_ACCOUNT_RPM", "1")
    monkeypatch.setattr(pacing, "_BUCKETS", {})
    monkeypatch.setattr(pacing, "_REDIS_DOWN_UNTIL", 0)
    monkeypatch.setattr(pacing, "_REDIS_WARNING_AT", 0)
    clock = [100.0]
    monkeypatch.setattr(pacing.time, "monotonic", lambda: clock[0])
    for scope in scopes:
        pacing.acquire(scope["account"])
        pacing.acquire(scope["account"])
    with pytest.raises(pacing.RateLimited):
        pacing.acquire("another-account")
    clock[0] += 1
    with pytest.raises(pacing.RateLimited):
        pacing.acquire(scopes[0]["account"])
    pacing.acquire("another-account")
    redis_calls = []
    class RedisClient:
        def eval(self, script, count, *arguments):
            redis_calls.append((count, arguments))
            return 1000 if len(redis_calls) == 2 else 0
    monkeypatch.setenv("REDIS_URL", "redis://pacing-test")
    monkeypatch.setattr(pacing, "_client", lambda url: RedisClient())
    pacing.acquire(scopes[0]["account"])
    with pytest.raises(pacing.RateLimited) as shared:
        pacing.acquire(scopes[1]["account"])
    assert shared.value.retry_after_s == 1
    assert redis_calls[0][0] == redis_calls[1][0] == 2
    assert redis_calls[0][1][0] == redis_calls[1][1][0]
    assert redis_calls[0][1][1] != redis_calls[1][1][1]
    def unavailable(url):
        raise ConnectionError("Redis unavailable")
    monkeypatch.setattr(pacing, "_client", unavailable)
    clock[0] += 60
    pacing.acquire(scopes[0]["account"])
    monkeypatch.delenv("REDIS_URL")
    for scope in scopes:
        for error in ({"status_code": 429, "retry_after": 120}, "Provider rejected the request with HTTP 429"):
            with pytest.raises(pacing.RateLimited):
                pacing.check_response(scope["account"], {"successful": False, "error": error})
        with pytest.raises(pacing.RateLimited) as tool_limited:
            pacing.check_response(scope["account"], {"successful": False, "error": {"status_code": 429}}, "120")
        assert tool_limited.value.retry_after_s == 120
        pacing.check_response(scope["account"], {"successful": True, "data": {"message": "429"}})
        pacing.check_response(scope["account"], {"successful": False, "error": "Message id 429 was not found"})
    monkeypatch.setattr(pacing, "acquire", lambda account: None)
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=429, headers={"Retry-After": "120"})
    monkeypatch.setattr(composio_gmail.httpx, "Client", Client)
    for scope in scopes:
        with pytest.raises(pacing.RateLimited) as limited:
            composio_gmail._request("POST", "/tools/execute/GMAIL_FETCH_EMAILS", json={"connected_account_id": scope["account"]})
        with bind(workspace_id=scope["wid"], venture_id=scope["vid"], actor=scope["owner"]):
            with factory() as session:
                job_id = jobs.enqueue(session, "gtm.gmail.read", {}, workspace_id=scope["wid"], venture_id=scope["vid"], max_attempts=1)
                session.commit()
                leased = jobs.lease(session, "pacing-worker", kinds=["gtm.gmail.read"])[0]
                session.commit()
                moment = datetime.now(timezone.utc)
                assert jobs.fail_job(session, job_id, limited.value, now=moment, worker_id="pacing-worker", attempt=leased.attempts) == "retry_wait"
                session.commit()
                row = session.get(Job, job_id)
                assert row.max_attempts > row.attempts
                assert row.run_after.replace(tzinfo=timezone.utc) >= moment + timedelta(seconds=120)
                row.status, row.locked_by, row.locked_at = "leased", "pacing-worker", moment
                row.attempts, row.max_attempts = 2048, 2048
                session.commit()
                assert jobs.fail_job(session, job_id, limited.value, now=moment, worker_id="pacing-worker", attempt=2048) == "retry_wait"
                session.commit()
                assert session.get(Job, job_id).max_attempts == 2049
    from app.approvals import external
    updates, refunds = [], []
    monkeypatch.setattr(external, "release", lambda hold, *, definitive: refunds.append(definitive))
    monkeypatch.setattr(external, "_set_execution", lambda approval_id, update: updates.append((approval_id, update)))
    for scope in scopes:
        attempt = SimpleNamespace(approval_id="approval-" + scope["owner"], hold=SimpleNamespace(reservation_id="reserved"))
        monkeypatch.setattr(external, "begin_metered_external_execution", lambda **kwargs: attempt)
        def busy():
            raise pacing.AdmissionLimited(120)
        with pytest.raises(pacing.RateLimited):
            external.execute_metered_external_action(workspace_id=scope["wid"], approval_id=attempt.approval_id,
                action="gtm_email_send", provider="composio", target={}, invoke=busy)
    assert refunds == [True, True]
    assert all(update["state"] == "retry_wait" for _, update in updates)
    import asyncio
    from app.autopilot import outreach
    from app.worker import handlers
    async def busy_send(*args, **kwargs):
        raise pacing.AdmissionLimited(120)
    monkeypatch.setattr(outreach, "_send_batch_admitted", busy_send)
    for scope in scopes:
        request = {"draft_ids": ["draft-" + scope["vid"]], "approval_id": "approval-" + scope["owner"]}
        with bind(workspace_id=scope["wid"], venture_id=scope["vid"], actor=scope["owner"]):
            for _ in range(2):
                response = asyncio.run(outreach.send_batch(scope["vid"], request, actor_subject=scope["owner"]))
                assert response["kind"] == "wait" and "retry by itself" in response["body_md"]
            with factory() as session:
                queued = list(session.scalars(select(Job).where(Job.kind == "gtm.outreach.retry", Job.workspace_id == scope["wid"])))
                assert len(queued) == 1
                payload = json.loads(queued[0].payload_json)
                assert payload["request"] == request and payload["_actor_subject"] == scope["owner"]
    retried = []
    async def resumed(venture_id, request, *, actor_subject):
        retried.append((venture_id, request, actor_subject))
    monkeypatch.setattr(outreach, "_send_batch_admitted", resumed)
    for scope in scopes:
        with factory() as session:
            queued = session.scalar(select(Job).where(Job.kind == "gtm.outreach.retry", Job.workspace_id == scope["wid"]))
            payload = json.loads(queued.payload_json) | {"venture_id": scope["vid"]}
        with bind(workspace_id=scope["wid"], venture_id=scope["vid"], actor=scope["owner"]):
            asyncio.run(handlers._outreach_retry(payload))
    assert len(retried) == 2 and {actor for _, _, actor in retried} == {scope["owner"] for scope in scopes}


@pytest.fixture()
def migration_db(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/migration.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.delenv("COMPOSIO_PROJECT", raising=False)
    monkeypatch.setenv("COMPOSIO_API_KEY", "migration-test-key")
    monkeypatch.setenv("COMPOSIO_LINKEDIN_TOOLKIT_VERSION", "20260922_00")
    load_company_os_app()
    from app.state import db, reset_db_factory
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailSendQueue, GtmOutreachSettings, GtmTouch, Person, Venture, Workspace
    from core.clock import utcnow
    reset_db_factory()
    factory, now = db(), utcnow()
    scopes = []
    with factory() as session:
        for index, domain in enumerate(DOMAINS):
            owner, wid, vid, account = f"owner_{index}", f"workspace_{index}", f"venture_{index}", f"mailbox_{index}"
            email = f"you{index}@mail.example"
            session.add(Person(id=owner, display_name="You", created_at=now))
            session.add(Workspace(id=wid, owner_id=owner, name=domain, kind="company", entry_mode="idea", created_at=now, updated_at=now))
            session.add(Venture(id=vid, workspace_id=wid, name=domain, raw_idea=domain, stage="draft", created_at=now, updated_at=now))
            session.add(GtmEmailAccount(id=account, workspace_id=wid, app_user_id=f"gmail:{wid}", provider="gmail",
                email=email, composio_user_id=f"gmail:{wid}", connected_account_id=f"old_{index}", status="active", created_at=now, updated_at=now))
            session.add(GtmOutreachSettings(workspace_id=wid, sender_postal_address="42 Lake Road, Austin, TX 78701", send_enabled=True,
                created_at=now, updated_at=now))
            for day in (1, 2):
                cid, job = f"contact_{index}_{day}", f"queue_{index}_{day}"
                state = {str(day): {"subject": "A question about your workflow", "body": "Would you share your experience?"}}
                session.add(GtmContact(id=cid, workspace_id=wid, email=f"recipient{day}@source{index}.example", email_confidence="risky",
                    source=f"venture:{vid}:research", lawful_basis="legitimate_interest", recipient_geo="US",
                    enrichment_json=json.dumps({"found_at": f"https://source{index}.example/contact"}),
                    sequence_state_json=json.dumps(state), times_emailed=int(day > 1), gmail_thread_id=f"thread_{index}" if day > 1 else None,
                    created_at=now, stage_changed_at=now))
                session.add(GtmEmailSendQueue(id=job, workspace_id=wid, contact_id=cid, email_account_id=account,
                    sequence_day=day, idempotency_key=f"{cid}:{day}", status="queued", attempts=0,
                    scheduled_for=now - timedelta(minutes=1), created_at=now, updated_at=now))
                if day > 1:
                    session.add(GtmTouch(id=f"touch_{index}", workspace_id=wid, venture_id=vid, contact_id=cid, direction="out", channel="email",
                        simulated=0, send_mode="founder_gmail", message_id=f"<sent{index}@mail.example>", provider_thread_id=f"thread_{index}",
                        sent_at=now - timedelta(days=3), created_at=now))
            scopes.append({"owner": owner, "wid": wid, "vid": vid, "account": account, "email": email, "index": index})
        session.commit()
    yield factory, scopes
    reset_db_factory()


@pytest.mark.parametrize("domain", DOMAINS)
def test_auth_config_pin_and_project_scoped_cache_survives_key_rotation(migration_db, monkeypatch, tmp_path, domain):
    from app.gtm.supymem import composio_gmail
    from app.content import composio_social
    cache = tmp_path / "auth-id"
    monkeypatch.setattr(composio_gmail, "_AUTH_CONFIG_CACHE", cache)
    monkeypatch.setenv("COMPOSIO_API_KEY", "old-key")
    composio_gmail._write_auth_config_cache("old-auth")
    assert composio_gmail._read_auth_config_cache() == "old-auth"
    monkeypatch.setenv("COMPOSIO_API_KEY", "new-key")
    assert composio_gmail._read_auth_config_cache() == "old-auth"
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    assert composio_gmail._read_auth_config_cache() == ""
    composio_gmail._write_auth_config_cache("new-auth")
    monkeypatch.setenv("COMPOSIO_API_KEY", "rotated-key")
    assert composio_gmail._read_auth_config_cache() == "new-auth"
    cache.write_text("old-unscoped-id")
    assert composio_gmail._read_auth_config_cache() == ""
    monkeypatch.setenv("COMPOSIO_GMAIL_AUTH_CONFIG_ID", "pinned-custom-auth")
    monkeypatch.setenv("COMPOSIO_LINKEDIN_AUTH_CONFIG_ID", "pinned-social-auth")
    monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: pytest.fail("A pinned auth config must not be replaced"))
    monkeypatch.setattr(composio_social, "_request", lambda *args, **kwargs: pytest.fail("A pinned auth config must not be replaced"))
    assert composio_gmail.ensure_auth_config() == "pinned-custom-auth"
    assert composio_social.ensure_auth_config("linkedin") == "pinned-social-auth"


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("error_kind", ["http404", "invalid_account", "connection404"])
def test_missing_saved_accounts_require_reconnect_without_retry_storm(migration_db, monkeypatch, index, error_kind):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.content import composio_social
    from app.db.models import ContentAccount, GtmEmailAccount
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow
    calls = []
    def gone(*args, **kwargs):
        calls.append(args)
        if error_kind == "invalid_account":
            return {"successful": False, "error": {"slug": "connected_account_invalid"}}
        if error_kind == "connection404":
            raise composio_gmail.DefiniteFailure("Connection not found", status_code=404)
        raise composio_gmail.DefiniteFailure("Connected account not found", status_code=404)
    monkeypatch.setattr(composio_gmail, "_request", gone)
    with pytest.raises(composio_gmail.ReconnectRequired):
        if error_kind == "connection404":
            composio_gmail.fetch_history(composio_user_id=f"gmail:{scope['wid']}", connected_account_id=f"old_{index}", start_history_id="321")
        else:
            composio_gmail.get_gmail_profile(f"gmail:{scope['wid']}", f"old_{index}")
    for _ in range(3):
        assert workers.tick_replies(scope["wid"])["skipped"] == "gmail_reconnect_required"
    assert len(calls) == 1
    with factory() as session:
        assert session.get(GtmEmailAccount, scope["account"]).status == "needs_reconnect"
        session.add(ContentAccount(id=f"social_{index}", workspace_id=scope["wid"], platform="linkedin", external_account=f"person_{index}",
            status="active", extra_json=json.dumps({"auth_via": "composio", "connected_account_id": f"social_old_{index}"}),
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    def social_gone(*args, **kwargs):
        if error_kind == "invalid_account":
            return {"successful": False, "error": {"slug": "connected_account_invalid"}}
        raise composio_social.ComposioSocialError("http_error", "Invalid connected account", status_code=404)
    monkeypatch.setattr(composio_social, "_request", social_gone)
    with pytest.raises(composio_social.ComposioSocialError) as error:
        composio_social.execute_tool("LINKEDIN_GET_MY_INFO", user_id=f"content:{scope['wid']}", connected_account_id=f"social_old_{index}", arguments={})
    assert error.value.code == "needs_reconnect"
    with factory() as session:
        account = session.get(ContentAccount, f"social_{index}")
        assert account.status == "needs_reconnect" and account.last_error is None


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("known_email", [True, False])
def test_queued_first_emails_and_followups_resume_once_with_original_keys(migration_db, monkeypatch, index, known_email):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailSendQueue
    from app.gtm.supymem import composio_gmail, pipeline
    from app.approvals import external
    if not known_email:
        with factory() as session:
            session.get(GtmEmailAccount, scope["account"]).email = None
            session.commit()
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.setattr("app.gtm.localization.experiments.execution_context", lambda wid: {"allowed": True, "mode": "legacy"})
    monkeypatch.setattr("app.gtm.relationships.cadence.company_block", lambda *args: None)
    for _ in range(3):
        result = pipeline.tick_send_queue(scope["wid"])
        assert result["skipped"] == ["gmail_reconnect_required"] and result["remaining"] == 2
    with factory() as session:
        jobs = list(session.scalars(select(GtmEmailSendQueue).where(GtmEmailSendQueue.workspace_id == scope["wid"])))
        keys = {job.id: job.idempotency_key for job in jobs}
        assert all(job.status == "queued" and job.attempts == 0 and job.last_error == "gmail_reconnect_required" for job in jobs)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: {"email": scope["email"]})
    result = pipeline.sync_gmail_accounts(scope["wid"], connections=[{"id": f"new_{index}", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}])
    assert result["synced"] == 1 and result["accounts"][0]["id"] == scope["account"]
    sent = []
    def send(**kwargs):
        assert kwargs["connected_account_id"] == f"new_{index}"
        assert "Your email address is listed publicly on " in kwargs["body"] and "no thanks" in kwargs["body"] and "42 Lake Road" in kwargs["body"]
        sent.append(kwargs)
        return {"provider_message_id": f"message_{len(sent)}", "thread_id": kwargs["thread_id"] or "new_thread"}
    monkeypatch.setattr(composio_gmail, "send_gmail_email", send)
    monkeypatch.setattr(external, "execute_metered_external_action", lambda **kwargs:
        external.OutboundExecution("succeeded", "ok", kwargs["invoke"]()))
    pipeline.tick_send_queue(scope["wid"], limit=2)
    pipeline.tick_send_queue(scope["wid"], limit=2)
    assert len(sent) == 2
    with factory() as session:
        for job_id, key in keys.items():
            job = session.get(GtmEmailSendQueue, job_id)
            assert job.status == "sent" and job.idempotency_key == key and job.attempts == 1
        assert session.get(GtmEmailAccount, scope["account"]).connected_account_id == f"new_{index}"
        assert session.get(GtmContact, f"contact_{index}_1").times_emailed == 1


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("expired_history", [False, True])
def test_reply_detection_resumes_last_checkpoint_after_reconnect(migration_db, monkeypatch, index, expired_history):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.db.models import GtmEmailEvent, GtmContact
    from app.connections.composio_migration import mark_gmail_reconnect
    from app.gtm.supymem import composio_gmail, pipeline, workers
    from core.clock import utcnow
    with factory() as session:
        session.add(GtmEmailEvent(id=f"checkpoint_{index}", workspace_id=scope["wid"], email_account_id=scope["account"],
            event_type="gmail_sync_checkpoint", metadata_json=json.dumps({"history_id": "321"}), created_at=utcnow()))
        session.commit()
    mark_gmail_reconnect(f"old_{index}")
    assert workers.tick_replies(scope["wid"])["skipped"] == "gmail_reconnect_required"
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: {"email": scope["email"], "raw": {"historyId": "654"}})
    pipeline.sync_gmail_accounts(scope["wid"], connections=[{"id": f"new_{index}", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}])
    histories = []
    def history(**kwargs):
        histories.append(kwargs)
        if expired_history:
            def stale(*args, **kwargs):
                raise composio_gmail.DefiniteFailure("Requested entity was not found", status_code=404)
            monkeypatch.setattr(composio_gmail, "_request", stale)
            return composio_gmail.execute_tool("GMAIL_LIST_HISTORY", user_id=kwargs["composio_user_id"],
                connected_account_id=kwargs["connected_account_id"], arguments={})
        return {"historyId": "654", "history": [{"messagesAdded": [{"message": {"id": f"reply_{index}"}}]}]}
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    message = {"id": f"reply_{index}", "threadId": f"thread_{index}",
        "headers": {"From": "a-forwarder@elsewhere.example", "In-Reply-To": f"<sent{index}@mail.example>"}, "snippet": "Yes, interested. Let's talk."}
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: message)
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [message]})
    result = workers.tick_replies(scope["wid"])
    assert result["marked"] == [f"contact_{index}_2"]
    assert histories[0]["start_history_id"] == "321" and histories[0]["connected_account_id"] == f"new_{index}"
    assert workers.tick_replies(scope["wid"])["marked"] == []
    with factory() as session:
        assert session.get(GtmContact, f"contact_{index}_2").replied_at is not None


def test_reconnect_card_is_once_per_founder_across_unrelated_ventures(migration_db, monkeypatch):
    factory, scopes = migration_db
    from app.connections.composio_migration import mark_gmail_reconnect, GMAIL_RECONNECT_TITLE
    from app.db.models import ChatMessage, Venture
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    scope = scopes[0]
    with factory() as session:
        session.add(Venture(id="sibling_venture", workspace_id=scope["wid"], name=DOMAINS[1], raw_idea=DOMAINS[1], stage="draft",
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    for account in scopes:
        for _ in range(3):
            mark_gmail_reconnect(f"old_{account['index']}")
    with factory() as session:
        cards = list(session.scalars(select(ChatMessage).where(ChatMessage.body_md.contains(GMAIL_RECONNECT_TITLE))))
        assert len(cards) == 2
        assert all(json.loads(card.actions_json)[0]["action"] == "connections.connect" for card in cards)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: {"email": scope["email"]})
    for _ in range(2):
        pipeline.sync_gmail_accounts(scope["wid"], connections=[{"id": "new_notice", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}])
    with factory() as session:
        assert len(list(session.scalars(select(ChatMessage).where(ChatMessage.kind == "gmail_reconnected")))) == 1
        assert not session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == scope["vid"],
            ChatMessage.body_md.contains(GMAIL_RECONNECT_TITLE)))
        resolved = session.scalar(select(ChatMessage).where(ChatMessage.kind == "gmail_reconnect_resolved"))
        assert resolved is not None and json.loads(resolved.actions_json) == []


def test_migration_report_is_read_only_counts_both_domains_without_pii(migration_db, monkeypatch, capsys, tmp_path):
    factory, scopes = migration_db
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "scripts/composio/migration_report.py"
    spec = importlib.util.spec_from_file_location("migration_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from app.db.models import ContentAccount, GtmEnrollment, GtmSequence, VentureDocument
    from core.clock import utcnow
    with factory() as session:
        for scope in scopes:
            sequence_id = f"report_sequence_{scope['index']}"
            session.add(GtmSequence(id=sequence_id, workspace_id=scope["wid"], name="Your sequence", created_at=utcnow()))
            session.flush()
            session.add(GtmEnrollment(id=f"report_enrollment_{scope['index']}", workspace_id=scope["wid"],
                sequence_id=sequence_id, contact_id=f"contact_{scope['index']}_1", status="active", created_at=utcnow()))
            session.add(ContentAccount(id=f"report_social_{scope['index']}", workspace_id=scope["wid"], platform="linkedin", external_account="private-profile",
                status="active", extra_json=json.dumps({"connected_account_id": "private-provider-id"}), created_at=utcnow(), updated_at=utcnow()))
            session.add(VentureDocument(id=f"report_draft_{scope['index']}", venture_id=scope["vid"], kind="outreach_draft", title="Your saved email", body_md="Private email body",
                provenance_json=json.dumps({"state": "draft", "channel": "email", "to": scope["email"]}), created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    with factory() as session:
        connection = session.connection()
        connection.execute(text("PRAGMA query_only = ON"))
        report = module.migration_report(connection)
        assert report["founders"] == 2 and report["workspaces"] == 2
        assert report["gmail_connections"] == 2 and report["linkedin_connections"] == 2 and report["queued_sends"] == 6 and report["active_sequences"] == 4
        output = json.dumps(report)
        assert all(scope["wid"] in output and scope["email"] not in output and scope["owner"] not in output for scope in scopes)
        assert all(domain not in output for domain in DOMAINS)
        assert "private-profile" not in output and "private-provider-id" not in output and "Private email body" not in output
    monkeypatch.setenv("COMPANY_OS_DB_URL", str(factory.kw["bind"].url))
    module.main()
    assert json.loads(capsys.readouterr().out) == report
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{missing}")
    with pytest.raises(SystemExit, match="could not read the database"):
        module.main()
    assert not missing.exists()


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
def test_metered_reconnect_pause_reuses_approval_and_settles_only_once(migration_db, monkeypatch, index):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.approvals import external
    from app.credits.policy import set_policy
    from app.db.models import Approval, CreditAccount, CreditReservation
    from core.clock import utcnow
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    set_policy("outbound_provider_rates", {"version": "migration-test", "rates": {"gtm_email_send:composio_gmail": {
        "action": "gtm_email_send", "provider": "composio_gmail", "price_usd": "0.01", "currency": "USD", "source": "Offline test contract"}}}, "test")
    target = {"queue_id": f"queue_{index}_1", "recipient": f"recipient1@source{index}.example", "sequence_day": 1,
        "subject": "A question", "body": DOMAINS[index]}
    with factory() as session:
        session.add(CreditAccount(id=f"wallet_{index}", scope="person", owner_person_id=scope["owner"],
            status="active", granted_micro=1_000_000, spent_micro=0, reserved_micro=0, created_at=utcnow(), updated_at=utcnow()))
        session.add(Approval(id=f"approval_{index}", workspace_id=scope["wid"], kind="external_comms", subject_ref=target["queue_id"],
            summary="Review your email", requested_by="agt_review", risk="high", status="approved", created_at=utcnow(),
            expires_at=utcnow() + timedelta(days=7), detail_json=json.dumps({"external_action": "gtm_email_send",
                "execution_digest": external.action_digest("gtm_email_send", target)})))
        session.commit()
    def paused():
        raise external.ProviderReconnectRequired("gmail_reconnect_required")
    args = {"workspace_id": scope["wid"], "approval_id": f"approval_{index}", "action": "gtm_email_send", "provider": "composio_gmail", "target": target}
    assert external.execute_metered_external_action(**args, invoke=paused).status == "paused"
    with pytest.raises(external.OutboundExecutionError, match="content mismatch"):
        external.execute_metered_external_action(**{**args, "target": {**target, "body": "Changed copy"}}, invoke=lambda: pytest.fail("Changed copy cannot send"))
    sent = []
    assert external.execute_metered_external_action(**args, invoke=lambda: sent.append(target) or {"provider_message_id": "confirmed"}).status == "succeeded"
    with pytest.raises(external.OutboundExecutionError, match="already consumed"):
        external.execute_metered_external_action(**args, invoke=lambda: sent.append(target))
    assert len(sent) == 1
    with factory() as session:
        assert session.get(CreditAccount, f"wallet_{index}").spent_micro == 10_000
        reservations = list(session.scalars(select(CreditReservation).where(CreditReservation.account_id == f"wallet_{index}")))
        assert len(reservations) == 2 and len({row.request_id for row in reservations}) == 1


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("legacy_identity", [False, True])
@pytest.mark.parametrize("reconnections", [1, 2])
def test_scheduled_email_job_pauses_and_resumes_with_same_ids(migration_db, monkeypatch, index, legacy_identity, reconnections):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.db.models import Approval, Job, VentureDocument
    from app.autopilot import outreach
    from app.approvals import external
    from app.connections.composio_migration import mark_gmail_reconnect
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    if legacy_identity:
        from app.db.models import GtmEmailAccount
        with factory() as session:
            session.get(GtmEmailAccount, scope["account"]).composio_user_id = f"legacy_user_{index}"
            session.commit()
    sending_draft = {"doc_id": "scheduled_draft", "contact_id": f"contact_{index}_1", "to": f"recipient1@source{index}.example",
        "subject": "Your question", "body": DOMAINS[index], "day": 1}
    original_target = outreach.journey_send_target(scope["vid"], sending_draft, outreach._mailbox(scope["wid"]))
    with factory() as session:
        session.add(Approval(id="original-approval", workspace_id=scope["wid"], kind="external_comms", subject_ref="scheduled_draft",
            summary="Your reviewed email", requested_by="agt_review", risk="high", status="approved", created_at=utcnow(), expires_at=utcnow() + timedelta(days=1),
            detail_json=json.dumps({"external_action": "gtm_email_send", "target": original_target,
                "execution_digest": external.action_digest("gtm_email_send", original_target)})))
        session.add(Job(id="scheduled_job", workspace_id=scope["wid"], venture_id=scope["vid"], kind="experiment.run", status="pending",
            payload_json="{}", attempts=0, max_attempts=1, idempotency_key="original-job-key", created_at=utcnow()))
        session.add(VentureDocument(id="scheduled_draft", venture_id=scope["vid"], kind="outreach_draft", title="Your scheduled email", created_at=utcnow(), updated_at=utcnow(),
            provenance_json=json.dumps({"state": "draft", "channel": "email", "scheduled_send": {"job_id": "scheduled_job", "approval_id": "original-approval"}})))
        session.commit()
    mark_gmail_reconnect(f"old_{index}")
    with factory() as session:
        assert session.get(Job, "scheduled_job").status == "paused"
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: {"email": scope["email"]})
    pipeline.sync_gmail_accounts(scope["wid"], connections=[{"id": "scheduled_reconnect", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}])
    connection_id = "scheduled_reconnect"
    if reconnections == 2:
        mark_gmail_reconnect(connection_id)
        connection_id = "scheduled_second_reconnect"
        pipeline.sync_gmail_accounts(scope["wid"], connections=[{"id": connection_id, "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}])
    with factory() as session:
        job = session.get(Job, "scheduled_job")
        assert job.status == "pending" and job.idempotency_key == "original-job-key" and job.attempts == 0
        draft = json.loads(session.get(VentureDocument, "scheduled_draft").provenance_json)
        assert draft["scheduled_send"] == {"job_id": "scheduled_job", "approval_id": "original-approval"} and "error" not in draft
    delivered, targets = [], []
    monkeypatch.setattr("app.gtm.relationships.abuse.assert_copy_or_send", lambda *args, **kwargs: None)
    monkeypatch.setattr(outreach, "_send_one", lambda box, *args: delivered.append(box["account"]) or {"provider_message_id": "confirmed"})
    def execute(**kwargs):
        targets.append(kwargs["target"])
        return external.OutboundExecution("succeeded", "ok", kwargs["invoke"]())
    monkeypatch.setattr(external, "execute_metered_external_action", execute)
    outreach._journey_send_one_admitted(scope["vid"], sending_draft, outreach._mailbox(scope["wid"]), "original-approval", None)
    assert targets == [original_target] and delivered == [connection_id]


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
def test_home_reconnect_status_is_db_only_and_workspace_scoped(migration_db, monkeypatch, index):
    _, scopes = migration_db
    from fastapi import HTTPException
    from app.api import ops_api
    from app.gtm.supymem import composio_gmail
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    monkeypatch.setattr(ops_api, "resolve_ops_person", lambda: scopes[index]["owner"])
    monkeypatch.setattr(ops_api.ops_config, "ops_api_key", lambda: None)
    monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: pytest.fail("Home readiness must not call a provider"))
    assert ops_api.gmail_reconnect_status(scopes[index]["wid"], None, None) == {"needs_reconnect": True, "connected": False}
    with pytest.raises(HTTPException) as denied:
        ops_api.gmail_reconnect_status(scopes[1 - index]["wid"], None, None)
    assert denied.value.status_code == 404


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
def test_new_founder_polling_connection_stays_v2_after_refresh_and_key_rotation(migration_db, monkeypatch, index):
    factory, scopes = migration_db
    from app.db.models import ChatMessage, GtmEmailEvent, Person, Venture, Workspace
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    wid, owner, vid = f"new_workspace_{index}", f"new_owner_{index}", f"new_venture_{index}"
    now = utcnow()
    with factory() as session:
        session.add(Person(id=owner, display_name="You", created_at=now))
        session.add(Workspace(id=wid, owner_id=owner, name=DOMAINS[index], kind="company", entry_mode="idea", created_at=now, updated_at=now))
        session.add(Venture(id=vid, workspace_id=wid, name=DOMAINS[index], raw_idea=DOMAINS[index], stage="draft", created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    monkeypatch.setenv("COMPOSIO_GMAIL_AUTH_CONFIG_ID", "your-custom-auth")
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"connected_account_id": "new_connection", "redirect_url": "https://connect.composio.dev/link/new"}
    monkeypatch.setattr(composio_gmail, "_request", request)
    assert pipeline.mail_readiness(wid)["needs_reconnect"] is False
    link = pipeline.start_gmail_connect(wid)
    assert link["auth_config_id"] == "your-custom-auth" and calls[0][2]["json"]["user_id"] == f"gmail:{wid}"
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda user: [
        {"id": "new_connection", "user_id": user, "status": "ACTIVE", "auth_config": {"id": "your-custom-auth"}}])
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": scopes[index]["email"]})
    for key in ("first-key", "rotated-key", "another-key"):
        monkeypatch.setenv("COMPOSIO_API_KEY", key)
        assert pipeline.sync_gmail_accounts(wid)["synced"] == 1
        readiness = pipeline.mail_readiness(wid)
        assert readiness["connected"] and not readiness["needs_reconnect"]
    with factory() as session:
        markers = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.workspace_id == wid, GtmEmailEvent.event_type == "composio_project")))
        assert len(markers) == 1
        data = json.loads(markers[0].metadata_json)
        assert data["project"] == "v2" and data["auth_config_id"] == "your-custom-auth" and data["composio_user_id"] == f"gmail:{wid}"
        assert not session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == vid))


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("status", ["active", "expired"])
def test_v1_polling_cannot_clear_reconnect_and_v2_polling_reuses_saved_queue(migration_db, monkeypatch, index, status):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.db.models import GtmEmailAccount, GtmEmailEvent, GtmEmailSendQueue
    from app.gtm.supymem import composio_gmail, pipeline
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    with factory() as session:
        session.get(GtmEmailAccount, scope["account"]).status = status
        session.commit()
    current = [{"id": f"old_{index}", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}]
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda *args: list(current))
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": scope["email"]})
    assert pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
    assert pipeline.sync_gmail_accounts(scope["wid"])["synced"] == 0
    assert pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
    current[:] = [{"id": f"new_{index}", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE", "auth_config_id": "custom-auth"}]
    assert pipeline.sync_gmail_accounts(scope["wid"])["synced"] == 1
    current.append({"id": f"old_{index}", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"})
    for _ in range(3):
        assert pipeline.sync_gmail_accounts(scope["wid"])["synced"] == 1
        assert not pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
    with factory() as session:
        accounts = list(session.scalars(select(GtmEmailAccount).where(GtmEmailAccount.workspace_id == scope["wid"])))
        assert len(accounts) == 1 and accounts[0].id == scope["account"]
        jobs = list(session.scalars(select(GtmEmailSendQueue).where(GtmEmailSendQueue.email_account_id == scope["account"])))
        assert len(jobs) == 2 and all(job.status == "queued" and job.last_error is None and job.attempts == 0 for job in jobs)
        assert {job.idempotency_key for job in jobs} == {f"contact_{index}_1:1", f"contact_{index}_2:2"}
        marker = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == scope["account"], GtmEmailEvent.event_type == "composio_project"))
        assert json.loads(marker.metadata_json)["previous_connected_account_id"] == f"old_{index}"


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("failure", ["expired", "revoked", "invalid_grant", "unauthorized", "http400", "api_key401"])
def test_token_failure_pauses_once_and_active_poll_cannot_hide_it(migration_db, monkeypatch, index, failure):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.db.models import ChatMessage, GtmEmailAccount, GtmEmailSendQueue
    from app.gtm.supymem import composio_gmail, pipeline, workers
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": scope["email"]})
    connection = {"id": f"current_{index}", "user_id": f"gmail:{scope['wid']}", "status": "ACTIVE"}
    pipeline.sync_gmail_accounts(scope["wid"], connections=[connection])
    calls = []
    def rejected(*args, **kwargs):
        calls.append(args)
        if failure == "api_key401":
            raise composio_gmail.DefiniteFailure("Unauthorized API key", status_code=401)
        if failure == "http400":
            raise composio_gmail.DefiniteFailure("invalid_grant: Token has been expired or revoked", status_code=400)
        return {"successful": False, "error": {"message": {"expired": "Access token expired", "revoked": "OAuth token revoked",
            "invalid_grant": "invalid_grant", "unauthorized": "Unauthorized", "http400": ""}[failure]}}
    monkeypatch.setattr(composio_gmail, "_request", rejected)
    if failure == "api_key401":
        with pytest.raises(composio_gmail.DefiniteFailure) as error:
            composio_gmail.execute_tool("GMAIL_SEND_EMAIL", user_id=connection["user_id"], connected_account_id=connection["id"], arguments={})
        assert not isinstance(error.value, composio_gmail.ReconnectRequired)
        assert pipeline.mail_readiness(scope["wid"])["connected"] and not pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
        return
    with pytest.raises(composio_gmail.ReconnectRequired):
        composio_gmail.execute_tool("GMAIL_SEND_EMAIL", user_id=connection["user_id"], connected_account_id=connection["id"], arguments={})
    monkeypatch.setattr(pipeline.gtm_factory, "apply_mailbox_ramp", lambda *args: None)
    for _ in range(3):
        assert pipeline.tick_send_queue(scope["wid"])["skipped"] == ["gmail_reconnect_required"]
        assert workers.tick_replies(scope["wid"])["skipped"] == "gmail_reconnect_required"
    assert len(calls) == 1
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda *args: [connection])
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: composio_gmail.verify_gmail_profile(user, account))
    assert pipeline.sync_gmail_accounts(scope["wid"])["synced"] == 0
    assert pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
    with factory() as session:
        assert session.get(GtmEmailAccount, scope["account"]).status == "needs_reconnect"
        assert all(job.status == "queued" and job.attempts == 0 and job.last_error == "gmail_reconnect_required" for job in
            session.scalars(select(GtmEmailSendQueue).where(GtmEmailSendQueue.email_account_id == scope["account"])))
        assert len(list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == scope["vid"], ChatMessage.kind == "needs_you")))) == 1


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("status", ["EXPIRED", "REVOKED", "FAILED"])
def test_remote_expiration_pauses_scheduled_work_without_losing_approval(migration_db, monkeypatch, index, status):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.db.models import Job, VentureDocument
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    with factory() as session:
        session.add(Job(id="token-job", workspace_id=scope["wid"], venture_id=scope["vid"], kind="experiment.run", status="pending",
            payload_json="{}", attempts=0, max_attempts=1, idempotency_key="saved-token-job-key", created_at=utcnow()))
        session.add(VentureDocument(id="token-draft", venture_id=scope["vid"], kind="outreach_draft", title="Your email", created_at=utcnow(), updated_at=utcnow(),
            provenance_json=json.dumps({"state": "draft", "channel": "email", "scheduled_send": {"job_id": "token-job", "approval_id": "saved-approval"}})))
        session.commit()
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda *args: [{"id": f"old_{index}", "user_id": f"gmail:{scope['wid']}", "status": status}])
    assert pipeline.sync_gmail_accounts(scope["wid"])["synced"] == 0
    assert pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
    with factory() as session:
        job = session.get(Job, "token-job")
        assert job.status == "paused" and job.idempotency_key == "saved-token-job-key" and job.attempts == 0
        draft = json.loads(session.get(VentureDocument, "token-draft").provenance_json)
        assert draft["error"] == "gmail_reconnect_required" and draft["scheduled_send"]["approval_id"] == "saved-approval"


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
def test_retired_receipt_is_not_reverified_and_cross_workspace_receipt_survives(migration_db, monkeypatch, index):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.content.oauth import attempts, callbacks
    from app.db.models import Workspace
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    sibling = f"sibling_{index}"
    with factory() as session:
        session.add(Workspace(id=sibling, owner_id=scope["owner"], name=DOMAINS[1 - index], kind="company", entry_mode="idea", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": scope["email"]})
    new = {"id": f"new_{index}", "user_id": f"gmail:{sibling}", "status": "ACTIVE", "auth_config_id": "custom-auth"}
    assert pipeline.sync_gmail_accounts(sibling, connections=[new])["synced"] == 1
    receipts = []
    def successful(wid, **kwargs):
        receipts.append(wid)
        return [{"connected_account_id": f"old_{index}", "platform": "gmail", "provider_user_id": f"gmail:{scope['wid']}", "auth_config_id": "old-auth"}] if wid == scope["wid"] else [
            {"connected_account_id": new["id"], "platform": "gmail", "provider_user_id": new["user_id"], "auth_config_id": "custom-auth"}]
    monkeypatch.setattr(attempts, "successful_receipts", successful)
    def verified(receipt, **kwargs):
        assert receipt["connected_account_id"] == new["id"], "The retired project receipt must not be called"
        return new
    monkeypatch.setattr(callbacks, "_verified_connection", verified)
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda *args: [])
    assert pipeline.sync_gmail_accounts(sibling)["synced"] == 1
    assert set(receipts) == {scope["wid"], sibling}
    assert pipeline.mail_readiness(sibling)["connected"] and not pipeline.mail_readiness(sibling)["needs_reconnect"]


@pytest.mark.parametrize("index", [0, 1], ids=DOMAINS)
@pytest.mark.parametrize("outcome", [None, "failed"])
def test_completed_consent_survives_missing_callback_or_transient_profile_failure(migration_db, monkeypatch, index, outcome):
    factory, scopes = migration_db
    scope = scopes[index]
    from app.content.oauth import callbacks
    from app.db.models import ContentOAuthState
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    monkeypatch.setenv("COMPOSIO_PROJECT", "v2")
    account_id = f"recovered_{index}"
    with factory() as session:
        session.add(ContentOAuthState(id=f"bound_{index}", workspace_id=scope["wid"], platform="gmail", actor_subject=scope["owner"],
            auth_method="composio", provider_user_id=f"gmail:{scope['wid']}", auth_config_id="custom-auth", connected_account_id=account_id,
            redirect_uri="https://api.example.test/gmail/callback", outcome=outcome, consumed_at=utcnow() if outcome else None,
            expires_at=utcnow() - timedelta(minutes=1), created_at=utcnow() - timedelta(minutes=20)))
        session.commit()
    requests = []
    def request(method, path, **kwargs):
        requests.append((method, path))
        assert method == "GET" and path == f"/connected_accounts/{account_id}"
        return {"id": account_id, "status": "ACTIVE", "toolkit": {"slug": "gmail"}, "auth_config": {"id": "custom-auth"}}
    monkeypatch.setattr(composio_gmail, "_request", request)
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda *args: [])
    if outcome:
        def unavailable(*args):
            raise RuntimeError("Gmail profile temporarily unavailable")
        monkeypatch.setattr(composio_gmail, "get_gmail_profile", unavailable)
        assert pipeline.sync_gmail_accounts(scope["wid"], connections=[callbacks._verified_connection({
            "platform": "gmail", "workspace_id": scope["wid"], "connected_account_id": account_id, "auth_config_id": "custom-auth", "provider_user_id": f"gmail:{scope['wid']}"})])["synced"] == 0
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": scope["email"]})
    result = pipeline.sync_gmail_accounts(scope["wid"])
    assert result["synced"] == 1 and result["accounts"][0]["id"] == scope["account"]
    assert not pipeline.mail_readiness(scope["wid"])["needs_reconnect"]
    assert requests and all(method == "GET" for method, _ in requests)
