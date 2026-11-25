"""Confirmed experiments finish outside HTTP and tolerate partial public evidence."""
import json

import httpx
import pytest
from sqlalchemy import select
from tests.company_os_harness import local_owner_clients

from tests.unit.test_autonomous_experiments import credit_meter, market, run_jobs, venture
from tests.unit.gtm.test_journey import journey_db


def confirm(client, venture_id, spec):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    action = autonomous.prepare(venture_id, spec["id"])["actions"][0]
    card = {"resulting_state": action["payload"]} if is_local() else action["confirmation"]
    reply = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": action["action"] if is_local() else "experiment.prepare",
        "payload": action["payload"] if is_local() else {"experiment_id": spec["id"]}})
    assert reply.status_code == 200, reply.text
    assert reply.json()["kind"] == "experiment_running", reply.text
    assert not any(action.get("confirmation") for action in reply.json()["actions"])
    return reply.json(), card


def test_local_worker_saves_provider_request_ids_for_budget_receipts(market, monkeypatch):
    import asyncio
    from app.core.providers_local import save_settings
    from app.credits import metered
    from app.experiments import autonomous, belief_autopilot
    from app.state import db
    from app.db.supymem_models import Experiment

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
        models={"chat": "local-test-model"})
    _client, venture_id, workspace_id, _view, _landing, provider, calls, *_rest = market
    original_search = provider.search

    async def search(query, **options):
        return await metered.call(lambda: original_search(query, **options), purpose="experiment.search",
            provider=provider.name, estimate_micro=0, actual_micro=lambda _result: 0,
            workspace_id=workspace_id, venture_id=venture_id)

    monkeypatch.setattr(provider, "search", search)
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        row.context = json.dumps({**json.loads(row.context), "request_ids": ["previous_call"]})
        session.commit()
    action = autonomous.prepare(venture_id, spec["id"])["actions"][0]
    assert asyncio.run(autonomous.run(venture_id, action["payload"]))["kind"] == "experiment_running"
    run_jobs()
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        assert row.state == "concluded"
        requests = context["request_ids"]
        assert len(requests) == len(set(requests)) == sum(call[0] == "search" for call in calls)
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, requests) == 0
        assert belief_autopilot._receipt_total(session, "another_workspace", venture_id, requests) is None


def test_stale_sweep_preserves_pending_terminal_state(market):
    from datetime import timedelta
    from app.core.jobs import enqueue, lease, reclaim_stale
    from app.db.models import Job
    from app.state import db
    from core.clock import utcnow

    _client, venture_id, workspace_id, view, *_rest = market
    with db()() as session:
        job_id = enqueue(session, "idea.page_refresh", {"domain": view["icp"]["who"]},
            venture_id=venture_id, workspace_id=workspace_id)
        session.commit()
        claimed, = lease(session, "lost-reader", kinds=["idea.page_refresh"])
        assert claimed.id == job_id
        session.get(Job, job_id).locked_at = utcnow() - timedelta(minutes=5)
        session.commit()
    with db()() as session:
        job = session.get(Job, job_id)
        job.status, job.locked_by, job.locked_at = "failed_dead", None, None
        assert reclaim_stale(session) == 0
        assert job.status == "failed_dead" and job.attempts == claimed.attempts
        assert reclaim_stale(session) == 0


def fail():
    request = httpx.Request("POST", "https://search.example/scrape")
    raise httpx.HTTPStatusError("Source unavailable", request=request, response=httpx.Response(500, request=request))


@pytest.mark.parametrize("fails", [False, True], ids=["result", "failure-retry"])
def test_confirm_queues_work_and_worker_posts_one_terminal_message(market, credit_meter, monkeypatch, fails):
    from app.core.local_mode import is_local
    from app.chat.store import list_messages
    from app.db.models import Job
    from app.experiments import autonomous
    from app.state import db

    client, venture_id, _workspace_id, _view, _landing, provider, calls, _hits, _app_id = market
    search = provider.search
    if fails:
        async def unavailable(*args, **kwargs):
            fail()
        monkeypatch.setattr(provider, "search", unavailable)
    spec = autonomous.available(venture_id)[0]
    reply, card = confirm(client, venture_id, spec)
    assert f"Your {spec['title'].lower()} is queued. Check your experiments for progress; I will post the result here." in reply["body_md"]
    assert reply["kind"] == "experiment_running" and not calls and not credit_meter
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))
        assert len(jobs) == 1 and jobs[0].status in {"pending", "scheduled"} and jobs[0].max_attempts == 1
    repeated = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.prepare", "payload": {"experiment_id": spec["id"]}})
    assert repeated.status_code == 200, repeated.text
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))) == 1
    first = run_jobs()
    terminal = [item for item in list_messages(venture_id) if item["kind"] in {"experiment_result", "experiment_blocked"}]
    assert len(terminal) == 1
    if fails:
        from app.chat.experiments import list_reply

        assert "could not retrieve any sources" in terminal[0]["body_md"]
        retry = next(action for action in terminal[0]["actions"] if action["label"] == "Retry")
        assert retry["action"] == "experiment.prepare"
        assert not credit_meter if is_local() else credit_meter[-1].actual_micro == 0
        assert "failed, ready to retry" in list_reply(venture_id)["body_md"]
        assert card["resulting_state"]["ceiling_micro"] == 0 if is_local() else credit_meter[-1].estimate_micro == card["resulting_state"]["ceiling_micro"]
        monkeypatch.setattr(provider, "search", search)
        again, retry_card = confirm(client, venture_id, spec)
        assert again["kind"] == "experiment_running"
        assert retry_card["resulting_state"]["ceiling_micro"] == card["resulting_state"]["ceiling_micro"]
        second = run_jobs()
        assert first.payload["run_id"] != second.payload["run_id"]
        assert not credit_meter if is_local() else credit_meter[0].request_id != credit_meter[1].request_id
        assert sum(item["kind"] == "experiment_result" for item in list_messages(venture_id)) == 1
    else:
        assert terminal[0]["citations"]
        assert not credit_meter if is_local() else credit_meter[-1].actual_micro == 9


@pytest.mark.parametrize("failure", ["scrape", "search", "all"], ids=["partial-page", "partial-search", "nothing-succeeded"])
def test_source_failures_continue_and_charge_only_confirmed_usage(market, credit_meter, monkeypatch, failure):
    from app.core.local_mode import is_local
    from app.chat.store import list_messages
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    client, venture_id, _workspace_id, _view, _landing, provider, calls, _hits, _app_id = market
    method = "scrape" if failure == "scrape" else "search"
    original = getattr(provider, method)
    attempts = 0

    async def intermittent(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure == "all" or attempts == 1:
            fail()
        return await original(*args, **kwargs)

    monkeypatch.setattr(provider, method, intermittent)
    spec = autonomous.available(venture_id)[0]
    confirm(client, venture_id, spec)
    run_jobs()
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        assert "pending_result" not in context
    assert not credit_meter if is_local() else credit_meter[-1].settle_result is not None
    if failure == "all":
        assert attempts == 3
        assert not credit_meter if is_local() else credit_meter[-1].actual_micro == 0
        assert context["retryable"] and context["error"] and not context.get("result")
        assert sum(item["kind"] == "experiment_blocked" for item in list_messages(venture_id)) == 1
    else:
        result = context["result"]
        assert row.state == "concluded" and result["failed_sources"] == 1
        broadened = is_local() and failure == "scrape"
        expected = 2 if broadened else 3
        assert f"found {expected} first-person complaints" in result["insight"] and "source check" not in result["insight"]
        assert len(result["sources"]) == expected
        assert result["actual_micro"] == 0 if is_local() else result["actual_micro"] == credit_meter[-1].actual_micro == 8
        assert len(calls) == (10 if broadened else 8) and attempts == (6 if failure == "scrape" else 3)
        if broadened:
            assert all(action == "search" and params == [{"tbs": "qdr:y"}] for action, _, *params in calls[-2:])
        assert sum(item["kind"] == "experiment_result" for item in list_messages(venture_id)) == 1


@pytest.mark.parametrize("request_kind", ["find", "email", "show"])
def test_request_shaped_problem_uses_reviewed_pain_and_distinct_gap_method(market, credit_meter, request_kind):
    from app.chat.store import list_messages
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous
    from app.state import db
    from app.ventures import known
    from core.clock import utcnow
    from core.ids import new_id

    client, venture_id, workspace_id, view, _landing, _provider, calls, _hits, _app_id = market
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    commands = {"find": f"Find me people in {audience}", "email": f"People in {audience} I can email", "show": f"Show me prospects in {audience}"}
    view["icp"]["problem"] = commands[request_kind]
    with db()() as session:
        idea_id, revision_id = new_id("isd"), new_id("rev")
        session.add(IdeaSession(id=idea_id, venture_id=venture_id, workspace_id=workspace_id,
            case="know", state="confirmed", current_revision_id=revision_id, confirmed_revision_id=revision_id,
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id=revision_id, session_id=idea_id, venture_id=venture_id, workspace_id=workspace_id,
            rev=1, source="founder", structured=json.dumps({"problem": "Problem: " + problem, "customer": audience}), created_at=utcnow()))
        session.commit()
    specs = autonomous.available(venture_id)
    demand, gaps = (next(spec for spec in specs if spec["scan"] == scan) for scan in ("demand", "competitors"))
    for spec in (demand, gaps):
        assert spec["icp"]["who"] == audience and spec["icp"]["problem"] == problem
        assert problem.casefold() in spec["hypothesis"].casefold() and audience.casefold() in spec["hypothesis"].casefold()
        assert commands[request_kind] not in spec["hypothesis"]
        assert all(commands[request_kind] not in query for query in spec["queries"])
    assert demand["method"] != gaps["method"] and "alternative offerings" in gaps["method"]
    reply = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.prepare", "payload": {"experiment_id": gaps["id"]}})
    assert reply.status_code == 200, reply.text
    assert reply.status_code == 200 and reply.json()["kind"] == "experiment_running", reply.text
    assert not any(action.get("confirmation") for action in reply.json()["actions"])
    assert not calls and not credit_meter
    run_jobs()
    result = next(item for item in list_messages(venture_id) if item["kind"] == "experiment_result")
    assert problem in result["body_md"] and commands[request_kind] not in result["body_md"] and calls
    snapshot = autonomous._snapshot(venture_id)
    changed = {**snapshot, "idea_problem": commands[request_kind]}
    runnable = autonomous.select_runnable(changed, _provider)
    if known.enabled():
        assert runnable and all(spec["icp"]["problem"] == problem for spec in runnable)
    else:
        assert runnable == []
    icp = {**snapshot["journey"]["icp"], "problem": commands[request_kind], "known_checks": {
        **snapshot["journey"]["icp"].get("known_checks", {}),
        "problem": {"status": "missing", "value": None, "source": None}}}
    assert autonomous.select_runnable({**changed, "idea_offer": None,
        "journey": {**snapshot["journey"], "icp": icp}}, _provider) == []


def test_show_experiments_lists_runnable_work_in_chat_for_each_audience(market):
    from app.chat.intent import classify_intent

    client, venture_id, _workspace_id, view, _landing, _provider, calls, _hits, _app_id = market
    assert classify_intent("show my experiments") == "experiment_list"
    reply = client.post(f"/ventures/{venture_id}/chat", json={"message": "show my experiments"})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["kind"] == "experiments" and "Your experiments:" in body["body_md"]
    assert view["icp"]["who"] in body["body_md"] and view["icp"]["problem"] in body["body_md"]
    assert any(action.get("action") == "experiment.prepare" for action in body["actions"])
    assert not any(action.get("href") for action in body["actions"]) and not calls


@pytest.mark.parametrize("failure", ["fatal", "watchdog"])
def test_lost_experiment_worker_blocks_once_without_replaying_effects(market, failure):
    from datetime import timedelta
    from app.chat.store import list_messages
    from app.core.jobs import FatalError, fail_job, lease, reclaim_stale
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db
    from core.clock import utcnow

    client, venture_id, _workspace_id, _view, _landing, _provider, calls, *_rest = market
    spec = autonomous.available(venture_id)[0]
    confirm(client, venture_id, spec)
    with db()() as session:
        claimed, = lease(session, "lost-worker", kinds=[autonomous.KIND])
        job = session.get(Job, claimed.id)
        job.max_attempts = 3
        job.locked_at = utcnow() - timedelta(minutes=5)
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        row.context = json.dumps({**context, "worker_started": True, "pending_result": {"actual_micro": 9}})
        session.commit()
    with db()() as session:
        if failure == "fatal":
            assert fail_job(session, claimed.id, FatalError("experiment_outcome_needs_recovery"),
                            worker_id="lost-worker", attempt=claimed.attempts) == "failed_dead"
        else:
            assert reclaim_stale(session) == 1
        assert reclaim_stale(session) == 0
        session.commit()
        context = json.loads(session.get(Experiment, spec["id"]).context)
        assert context["error"] and context["outreach_state"] == "blocked" and not context["retryable"]
        assert context["pending_result"] == {"actual_micro": 9} and not context.get("result")
        assert session.get(Job, claimed.id).status == "failed_dead"
        assert lease(session, "replacement", kinds=[autonomous.KIND]) == []
    saved = next(item for item in autonomous.available(venture_id) if item["id"] == spec["id"])
    assert saved["state"] == "blocked" and not saved["retryable"]
    terminal = [item for item in list_messages(venture_id) if item["kind"] == "experiment_blocked"]
    assert len(terminal) == 1 and "Nothing was repeated automatically" in terminal[0]["body_md"]
    assert any(action.get("action") == "experiment.run_review" or action.get("confirmation", {}).get("action") == "experiment.run_review" for action in terminal[0]["actions"])
    assert not any(action.get("action") == "experiment.prepare" for action in terminal[0]["actions"]) and not calls


def test_uncertain_experiment_offers_credit_recovery_and_separate_manual_test(market, credit_meter, monkeypatch):
    import asyncio
    from app.core.local_mode import is_local
    from app.chat.experiments import list_reply
    from app.chat.store import list_messages
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db

    client, venture_id, _workspace_id, _view, _landing, _provider, calls, *_rest = market

    async def interrupted(*_args):
        raise RuntimeError("worker stopped before settlement")

    monkeypatch.setattr(autonomous, "_scan", interrupted)
    spec = autonomous.available(venture_id)[0]
    _reply, card = confirm(client, venture_id, spec)
    run_jobs()
    terminal = next(item for item in list_messages(venture_id) if item["kind"] == "experiment_blocked")
    for reply in (terminal, autonomous.prepare(venture_id, spec["id"]), list_reply(venture_id),
                  asyncio.run(autonomous.run(venture_id, card["resulting_state"]))):
        assert ("same work is not repeated" if is_local() else "same work is not charged twice") in reply["body_md"]
        assert any(action.get("href") == (f"/v/{venture_id}/experiments" if is_local() else "/account")
            or is_local() and action.get("action") == "experiment.list" for action in reply["actions"])
        assert not any(action.get("action") == "experiment.prepare" and action.get("payload", {}).get("experiment_id") == spec["id"] for action in reply["actions"])
    manual = next(action for action in terminal["actions"] if action.get("action") == "experiment.run_review" or action.get("confirmation"))
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.run_review",
        "payload": manual.get("payload") or manual["confirmation"]["resulting_state"],
        **({} if is_local() else {"message_id": terminal["id"]})})
    assert response.status_code == 200, response.text
    assert ledger.list_for_venture(venture_id)[0]["state"] == "proposed"
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))) == 1
        context = json.loads(session.get(Experiment, spec["id"]).context)
        assert context["error"] and not context["retryable"] and not context.get("result")
    assert not calls


@pytest.mark.parametrize("runner", ["scan", "outreach_split"])
@pytest.mark.parametrize("collision", [False, True], ids=["experiment-token", "validation-token-collision"])
def test_experiment_job_scope_uses_its_own_domain_and_keeps_tenant_404s(journey_db, monkeypatch, runner, collision):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.authz.http import journey_resources
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.db.supymem_models import Experiment, IdeaRevision, IdeaSession, ValidationRun
    from app.experiments import ledger
    from app.worker import handlers, main
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    monkeypatch.setattr(handlers, "hosted_mode", lambda: True)
    tenants = []
    for audience, problem in (("factory maintenance teams", "missed equipment inspections"),
                              ("amateur musicians", "organizing practice sessions")):
        actor = ensure_subject_person(new_id("usr"))
        workspace_id = create_workspace(kind="company", name=audience, owner_id=actor).id
        venture_id, idea_id, revision_id, validation_id = (new_id(prefix) for prefix in ("ven", "isd", "rev", "val"))
        now = utcnow()
        with journey_db["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=workspace_id, name=audience, raw_idea=problem,
                stage="draft", created_at=now, updated_at=now))
            session.add(IdeaSession(id=idea_id, workspace_id=workspace_id, venture_id=venture_id,
                case="know", created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id=revision_id, session_id=idea_id, workspace_id=workspace_id,
                venture_id=venture_id, rev=1, source="founder", created_at=now))
            session.flush()
            session.add(ValidationRun(id=validation_id, session_id=idea_id, revision_id=revision_id,
                workspace_id=workspace_id, venture_id=venture_id, method="demand", created_at=now))
            session.commit()
        tenants.append({"actor": actor, "workspace_id": workspace_id, "venture_id": venture_id,
                        "validation_id": validation_id, "experiment_id": new_id("exp")})
    for index, tenant in enumerate(tenants):
        tenant["run_id"] = tenants[1 - index]["validation_id"] if collision else new_id("exr")
        with journey_db["factory"]() as session:
            session.add(Experiment(id=tenant["experiment_id"], workspace_id=tenant["workspace_id"],
                venture_id=tenant["venture_id"], kind=ledger.KIND,
                context=json.dumps({"runner": "system", "run_id": tenant["run_id"], "spec": {"runner": runner}})))
            session.commit()
    for index, tenant in enumerate(tenants):
        payload = {"experiment_id": tenant["experiment_id"], "run_id": tenant["run_id"], "_actor_subject": tenant["actor"]}
        job = SimpleNamespace(kind="experiment.run", workspace_id=tenant["workspace_id"],
                              venture_id=tenant["venture_id"], payload=payload)
        scope = main._job_scope(job)
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (tenant["workspace_id"], tenant["venture_id"], tenant["actor"])
        assert journey_resources({"run_id": tenant["validation_id"]}) == [(tenant["workspace_id"], tenant["venture_id"])]
        job.kind, job.payload = "idea.validate", {"run_id": tenant["validation_id"], "_actor_subject": tenant["actor"]}
        assert main._job_scope(job).venture_id == tenant["venture_id"]
        job.kind, job.payload = "experiment.beliefs.tick", {"experiment_id": tenant["experiment_id"], "_actor_subject": tenant["actor"]}
        assert main._job_scope(job).venture_id == tenant["venture_id"]
        foreign = tenants[1 - index]
        for kind, forged in (("experiment.run", {**payload, "run_id": new_id("exr")}),
                             ("experiment.run", {**payload, "experiment_id": foreign["experiment_id"], "run_id": foreign["run_id"]}),
                             ("experiment.run", {**payload, "_actor_subject": foreign["actor"]}),
                             ("experiment.beliefs.tick", {"experiment_id": foreign["experiment_id"], "_actor_subject": tenant["actor"]}),
                             ("idea.validate", {"run_id": foreign["validation_id"], "_actor_subject": tenant["actor"]})):
            job.kind, job.payload = kind, forged
            with pytest.raises(HTTPException) as rejected:
                main._job_scope(job)
            assert rejected.value.status_code == 404
        job.kind, job.payload = "experiment.run", payload
        job.venture_id = None
        with pytest.raises(main.FatalError, match="job_scope_missing"):
            main._job_scope(job)
