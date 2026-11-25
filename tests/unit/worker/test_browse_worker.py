"""One owned, budgeted background job; no work or data shared between founders."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
import pytest
from sqlalchemy import select

from tests.unit.tools.test_browse import browse_env, http_fixture, install_site, modules, site


def test_browse_kind_registered_on_the_existing_worker(browse_env, site, monkeypatch):
    from app.worker import handlers
    from app.worker.catalog import enabled_kinds
    from app.analytics.feature_catalog import ACTION_SLUG, job_feature
    env = browse_env
    assert env.browse.KIND in enabled_kinds()
    assert ACTION_SLUG["browse_web"] == "browse" and job_feature(env.browse.KIND) == "browse"
    monkeypatch.setattr(handlers, "HANDLERS", {})
    monkeypatch.setattr(handlers, "enabled_kinds", lambda: {env.browse.KIND})
    monkeypatch.setattr("app.worker.catalog.enabled_kinds", lambda: {env.browse.KIND})
    handlers.install_default_handlers()
    assert set(handlers.HANDLERS) == {env.browse.KIND}
    install_site(env, site, {"/": "Done"})
    async def dispatch(payload):
        await handlers.dispatch(env.browse.KIND, payload)
    runtime = env.run(env.start(site.url), handler=dispatch)
    assert len(runtime["notes"]) == 1 and len(env.summaries) == 1


def test_per_founder_admission_does_not_block_another_founder(browse_env, site):
    env = browse_env
    env.bounds["founder_concurrency"] = 1
    first = env.start(site.url)
    assert env.start(site.other).startswith("Your browse tasks are still running")
    second = env.start(site.other, founder=1)
    assert first.startswith("job_") and second.startswith("job_") and first != second
    assert not env.http.requests


def test_concurrency_two_and_configured_bounds_are_saved(browse_env, site):
    from app.db.models import Job
    env = browse_env
    jobs = [env.start(site.url), env.start(site.other)]
    assert env.start(site.url).startswith("Your browse tasks are still running")
    for job_id in jobs:
        assert env.document(job_id)["limits"] == env.bounds
        with env.factory() as session:
            row = session.get(Job, job_id)
            assert row.max_attempts == 1 and row.timeout_s < 400


@pytest.mark.parametrize("concurrency", [1, 2])
def test_simultaneous_admission_cannot_exceed_founder_slots(browse_env, site, concurrency):
    from app.db.models import Job
    env = browse_env
    env.bounds["founder_concurrency"] = concurrency
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda index: env.start(site.url + f"/{index}"), range(3)))
    assert sum(result.startswith("job_") for result in results) == concurrency
    with env.factory() as session:
        assert len(session.scalars(select(Job).where(Job.kind == env.browse.KIND)).all()) == concurrency


def test_tool_starts_one_durable_job_without_using_the_turn_budget(browse_env, site):
    from app.db.models import Job
    env = browse_env
    env.ctx.founder_urls = [site.url]
    with env.bind(workspace_id="workspace-0", venture_id="venture-0", actor="person-0"):
        result = asyncio.run(env.tools.execute_tool("browse_web", {"url": site.url, "goal": site.title}, env.ctx))
    assert result.startswith("Browse task") and not env.http.requests
    assert env.ctx.budget_micro == 0 and env.ctx.spent_micro == 0
    with env.factory() as session:
        rows = session.scalars(select(Job).where(Job.kind == env.browse.KIND)).all()
        assert len(rows) == 1 and rows[0].status == "scheduled"


@pytest.mark.parametrize("change", ["worker", "attempt", "scope", "cancel"])
def test_stale_or_cancelled_worker_cannot_read_or_spend(browse_env, site, change):
    from app.db.models import Job
    env = browse_env
    payload = env.claim(env.start(site.url))
    if change == "worker":
        payload["_worker_id"] = "another-worker"
    elif change == "attempt":
        payload["_job_attempt"] = 2
    elif change == "scope":
        payload["venture_id"] = "venture-1"
    else:
        with env.factory() as session:
            session.get(Job, payload["_job_id"]).cancel_requested = True
            session.commit()
    with pytest.raises((env.browse.FatalError, PermissionError, HTTPException)):
        asyncio.run(env.browse.handle(payload))
    assert not env.http.requests and not env.summaries


def test_private_conversation_receives_only_its_own_progress_and_result(browse_env, site):
    env = browse_env
    install_site(env, site, {"/": "Done"})
    with env.bind(workspace_id="workspace-0", venture_id="venture-0", actor="person-0"):
        first = env.conversations.create("venture-0", idempotency_key="first")["id"]
        other = env.conversations.create("venture-0", idempotency_key="other")["id"]
    job_id = env.start(site.url, conversation_id=first)
    env.run(job_id)
    assert env.messages() and all(message.conversation_id == first for message in env.messages())
    assert not any(message.conversation_id == other for message in env.messages())


def test_a_saved_private_job_cannot_adopt_a_different_conversation(browse_env, site):
    env = browse_env
    with env.bind(workspace_id="workspace-0", venture_id="venture-0", actor="person-0"):
        first = env.conversations.create("venture-0", idempotency_key="first")["id"]
        other = env.conversations.create("venture-0", idempotency_key="other")["id"]
        payload = env.claim(env.start(site.url, conversation_id=first))
        with env.conversations.bind_conversation("venture-0", other):
            with pytest.raises(env.browse.FatalError, match="browse_conversation_changed"):
                asyncio.run(env.browse.handle(payload))
    assert not env.http.requests and all(message.conversation_id == first for message in env.messages())


def test_revoked_request_work_permission_stops_the_job(browse_env, site):
    from app.db.models import WorkspaceMember
    env = browse_env
    job_id = env.start(site.url)
    with env.factory() as session:
        session.get(WorkspaceMember, "member-0").role = "viewer"
        session.commit()
    with pytest.raises(PermissionError, match="browse_authority_revoked"):
        env.run(job_id)
    assert not env.http.requests and not env.summaries


def test_replayed_intent_uses_the_same_job_and_progress(browse_env, site):
    from app.chat.store import CLIENT_REQUEST_ID
    env = browse_env
    token = CLIENT_REQUEST_ID.set("same-request")
    try:
        first = env.start(site.url)
        assert env.start(site.url) == first
        assert len(env.messages()) == 1
    finally:
        CLIENT_REQUEST_ID.reset(token)


def test_repeated_handler_cannot_replay_provider_spend(browse_env, site):
    env = browse_env
    install_site(env, site, {"/": "Done"})
    job_id = env.start(site.url)
    env.run(job_id)
    requests, calls = len(env.http.requests), len(env.summaries)
    with pytest.raises(env.browse.FatalError, match="browse_outcome_unknown"):
        env.run(job_id)
    assert len(env.http.requests) == requests and len(env.summaries) == calls

