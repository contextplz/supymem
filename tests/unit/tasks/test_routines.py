"""Simulated-clock routines across independent business and consumer founders."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import json
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, func, select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.company_os_harness import configure_local_test_model


@pytest.fixture()
def routine_env(lifecycle, monkeypatch):
    from app.chat import conversations, store
    from app.credits import policy
    from app.db.models import Approval, ChatConversation, ChatMessage, CreditPolicy, EventLog, GtmContact, GtmTouch, Person, Venture, VentureDocument, Workspace, WorkspaceMember
    from app.db.supymem_models import VentureWaitlistEntry
    from app.tasks import planner, routines
    from app.tasks.contracts import Contract
    from app.ventures import lifecycle as venture_lifecycle

    factory = lifecycle["factory"]
    engine = factory.kw["bind"]
    for model in (Approval, ChatConversation, ChatMessage, CreditPolicy, EventLog, GtmContact, GtmTouch,
                  VentureDocument, WorkspaceMember, VentureWaitlistEntry):
        model.__table__.create(engine, checkfirst=True)
    flags = {"routines_v1": True, "approvals_policy_v1": True}
    monkeypatch.setattr(policy, "get_policy", lambda key: flags if key == "feature_flags" else None)
    monkeypatch.setattr(routines, "db", lambda: factory)
    monkeypatch.setattr(conversations, "db", lambda: factory)
    monkeypatch.setattr(venture_lifecycle, "db", lambda: factory)
    cases = [
        {"domain": "inventory analysis for logistics teams", "zone": "America/New_York"},
        {"domain": "meal planning for families", "zone": "Asia/Kolkata"},
    ]
    with factory() as session:
        now = lifecycle["clock"][0].replace(tzinfo=None)
        for index, case in enumerate(cases):
            case.update(actor=f"usr_routine_{index}", workspace=f"ws_routine_{index}", venture=f"ven_routine_{index}", conversation=f"cnv_routine_{index}")
            session.add(Person(id=case["actor"], display_name="Test founder", timezone=case["zone"], created_at=now))
            session.flush()
            session.add(Workspace(id=case["workspace"], owner_id=case["actor"], kind="company", entry_mode="idea",
                name=case["domain"], created_at=now, updated_at=now))
            session.flush()
            session.add(Venture(id=case["venture"], workspace_id=case["workspace"], name=case["domain"], raw_idea=case["domain"], stage="draft", created_at=now, updated_at=now))
            session.add(WorkspaceMember(id=f"member_routine_{index}", workspace_id=case["workspace"], person_id=case["actor"], role="owner", can_approve=1, created_at=now))
            session.flush()
            session.add(ChatConversation(id=case["conversation"], workspace_id=case["workspace"], venture_id=case["venture"],
                creator_person_id=case["actor"], visibility="private", title="Routine tests", creation_key=case["conversation"], created_at=now, updated_at=now))
        session.commit()
    plans, posts = [], {}

    async def preview(goal, **kwargs):
        from core.time_context import time_context
        assert f"founder timezone: {kwargs['timezone']}." in time_context()
        plans.append((goal, kwargs))
        return Contract(title=goal[:160], goal=goal, done_test={"type": "deterministic", "spec": {"predicate": "all_steps_verified"}},
            steps=[{"title": "Summarize saved replies", "skill": "tasks.waitlist_summary", "done_test": {"type": "deterministic", "spec": {"predicate": "workflow_receipt"}}}])

    def append(venture_id, **kwargs):
        case = next(case for case in cases if case["venture"] == venture_id)
        scope = conversations.current_conversation()
        with factory() as session:
            kwargs["output_authority"](session, scope)
        assert kwargs["conversation_id"] == case["conversation"]
        posts.setdefault(kwargs["idempotency_key"], (venture_id, kwargs))
        return {"id": kwargs["idempotency_key"]}

    original_append = store.append_message
    monkeypatch.setattr(planner, "preview", preview)
    monkeypatch.setattr(store, "append_message", append)
    lifecycle.update(routines=routines, cases=cases, plans=plans, posts=posts, flags=flags,
                     original_append=original_append)
    return lifecycle


@pytest.fixture(params=[0, 1], ids=["business", "consumer"])
def local_routine_env(routine_env, request, monkeypatch, tmp_path):
    from app.chat import conversations, store
    from app.core import local_secrets, providers_local
    from app.worker.handlers import HANDLERS

    env = routine_env
    path = tmp_path / "local-routines.sqlite"
    with env["factory"].kw["bind"].connect() as source, sqlite3.connect(path) as target:
        source.connection.driver_connection.backup(target)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    env["factory"].configure(bind=engine)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    providers_local.save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
                                  models={"chat": "local-chat"}, api_key="")
    local_secrets.update_settings("spend", {"monthly_spend_cap": "1"}, {})
    local_secrets.update_settings("capabilities", {"routines": True, "background_work": True}, {})
    env["flags"].update(routines_v1=False, approvals_policy_v1=False)
    importlib.reload(conversations)
    monkeypatch.setattr(conversations, "db", lambda: env["factory"])
    monkeypatch.setattr(store, "db", lambda: env["factory"])
    monkeypatch.setattr(store, "append_message", env["original_append"])
    monkeypatch.setattr(env["worker"], "_run_maintenance", lambda: None)
    monkeypatch.setitem(HANDLERS, "routines.fire", env["routines"].fire)
    monkeypatch.setitem(HANDLERS, "routines.report", env["routines"].report)
    monkeypatch.setitem(HANDLERS, "tasks.execute", env["execution"].execute)
    env.update(case=env["cases"][request.param], settings=local_secrets)
    yield env
    engine.dispose()


def set_routines_enabled(env, value):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    env["flags"]["routines_v1"] = value
    if is_local():
        update_settings("capabilities", {"routines": value}, {})


def local_worker_job(env, kind):
    with env["factory"]() as session:
        leased = env["jobs"].lease(session, "local-routine-worker", kinds=[kind], now=env["clock"][0], n=1)
        session.commit()
    assert len(leased) == 1
    asyncio.run(env["worker"]._run_one(env["factory"], leased[0], set()))
    return leased[0].id


def test_local_routine_changed_during_lock_keeps_404_with_active_list_recovery(local_routine_env, monkeypatch):
    from app.api import handoffs_api

    env, case = local_routine_env, local_routine_env["case"]
    item = create(env, case)
    original = handoffs_api._locked_task
    def canceled(session, venture_id, identifier):
        task = original(session, venture_id, identifier)
        task.state = "canceled"
        return task
    monkeypatch.setattr(handoffs_api, "_locked_task", canceled)
    with founder(case), env["factory"]() as session:
        with pytest.raises(HTTPException) as error:
            env["routines"].owned(session, case["venture"], item["id"])
        assert error.value.status_code == 404
        assert error.value.detail == "Open your venture's Routines screen, then choose an active routine from your saved routines."


@pytest.mark.parametrize("entry", ["chat", "api"])
def test_local_routine_timer_dispatches_and_saves_private_result(local_routine_env, entry, caplog):
    from app.api import routines_api
    from app.db.models import ChatMessage, Job, VentureDocument
    from app.db.task_models import Task, TaskRun

    env, case = local_routine_env, local_routine_env["case"]
    with founder(case):
        if entry == "chat":
            reply = asyncio.run(env["routines"].action(case["venture"], "routines.create",
                                {"goal": "every Friday summarise replies"}))
            assert reply["actions"] == []
            assert "standing approval" not in reply["body_md"]
            assert "credit" not in reply["body_md"].lower()
        else:
            asyncio.run(routines_api.create_routine(case["venture"], routines_api.RoutineBody(
                        goal="every Friday summarise replies")))
        item = env["routines"].list_routines(case["venture"])[0]
        assert asyncio.run(env["routines"].action(case["venture"], "routines.list", {}))["actions"] == []
    env["clock"][0] = datetime.fromisoformat(item["next_fire_at"])
    env["worker"]._run_scheduler()
    fire_id = local_worker_job(env, "routines.fire")
    local_worker_job(env, "tasks.execute")
    with env["factory"]() as session:
        assert session.get(Job, fire_id).status == "succeeded"
        child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
        assert child.state == "completed"
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
        document = session.get(VentureDocument, "task-report:" + run.id)
        assert document.venture_id == case["venture"]
        assert "replies" in document.body_md.lower()
        assert session.get(Task, item["id"]).next_fire_at > env["clock"][0].replace(tzinfo=None)
    env["clock"][0] += timedelta(seconds=30)
    report_id = local_worker_job(env, "routines.report")
    with env["factory"]() as session:
        assert session.get(Job, report_id).status == "succeeded", [
            getattr(record, "exception_message", "") for record in caplog.records if record.message == "job failed"]
        messages = session.scalars(select(ChatMessage)).all()
        assert len(messages) == 1
        assert (messages[0].venture_id, messages[0].conversation_id) == (case["venture"], case["conversation"])
        assert "replies" in messages[0].body_md.lower()
        assert not any("confirmation" in action for action in json.loads(messages[0].actions_json))


@pytest.mark.parametrize("skill,goal", [("research.deep", "research my idea"),
    ("gtm.journey.prepare", "find customers and prepare drafts"),
    ("tasks.reply_draft", "draft a reply for review"), ("tasks.waitlist_summary", "summarise replies")])
def test_local_requested_preparation_needs_no_hosted_approval(local_routine_env, monkeypatch, skill, goal):
    from app.approvals import standing
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import planner
    from app.tasks.contracts import Contract

    env, case = local_routine_env, local_routine_env["case"]
    monkeypatch.setattr(standing, "auto_approve", lambda **kwargs: pytest.fail("Consulted a hosted approval"))
    async def preview(goal, **kwargs):
        return Contract(title=goal, goal=goal, steps=[{"title": "Requested preparation", "skill": skill}])

    monkeypatch.setattr(planner, "preview", preview)
    item = create(env, case, "every Friday " + goal)
    fire(env, case, item)
    with founder(case), env["factory"]() as session:
        child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
        step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        assert env["routines"].admit_step(session, child, run, step)


@pytest.mark.parametrize("blocked", ["routines", "spend"])
def test_local_worker_keeps_blocked_routine_durable_with_next_step(local_routine_env, blocked):
    from app.core.local_capabilities import blocked_message
    from app.db.models import Job
    from app.db.task_models import Task

    env, case = local_routine_env, local_routine_env["case"]
    item = create(env, case)
    if blocked == "routines":
        env["settings"].update_settings("capabilities", {"routines": False}, {})
        with founder(case), pytest.raises(HTTPException) as stopped:
            asyncio.run(env["routines"].create(case["venture"], "every Friday summarise replies"))
        assert stopped.value.detail == blocked_message("routines")
    else:
        env["settings"].update_settings("spend", {"monthly_spend_cap": "0"}, {})
    env["clock"][0] = datetime.fromisoformat(item["next_fire_at"])
    env["worker"]._run_scheduler()
    identifier = local_worker_job(env, "routines.fire")
    with env["factory"]() as session:
        job = session.get(Job, identifier)
        assert job.status == "retry_wait" and job.attempts == 0
        assert "Settings" in job.last_error or "Keys and connections" in job.last_error
        if blocked == "routines":
            assert job.last_error == blocked_message("routines")
        assert session.get(Task, item["id"]).next_fire_at == env["clock"][0].replace(tzinfo=None)
        assert not session.scalar(select(Task.id).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
    env["settings"].update_settings("capabilities", {"routines": True}, {})
    env["settings"].update_settings("spend", {"monthly_spend_cap": "1"}, {})
    env["clock"][0] += timedelta(seconds=60)
    local_worker_job(env, "routines.fire")
    with env["factory"]() as session:
        assert session.scalar(select(Task.id).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))


@pytest.mark.parametrize("skill", ["pagebuilder.publish", "device.work_order", "task.run"])
def test_local_routine_rejects_unbounded_actions_before_saving(local_routine_env, monkeypatch, skill):
    from app.db.task_models import Task
    from app.tasks import planner
    from app.tasks.contracts import Contract

    env, case = local_routine_env, local_routine_env["case"]

    async def preview(goal, **kwargs):
        return Contract(title=goal, goal=goal, steps=[{"title": "Requested preparation", "skill": skill}])

    monkeypatch.setattr(planner, "preview", preview)
    with pytest.raises(ValueError, match="private.*chat"):
        create(env, case)
    with env["factory"]() as session:
        assert not session.scalar(select(Task.id).where(Task.create_key.startswith("routine:")))


@pytest.mark.parametrize("skill,name,goal", [("research.deep", "research", "research my idea"),
    ("gtm.journey.prepare", "find_customers", "find customers and prepare drafts"),
    ("tasks.reply_draft", "content", "draft a reply for review")])
def test_local_preparation_capability_off_stops_due_and_queued_work(local_routine_env, monkeypatch, skill, name, goal):
    from app.core.local_capabilities import blocked_message
    from app.db.models import ChatMessage, VentureDocument
    from app.db.task_models import Task
    from app.tasks import planner
    from app.tasks.contracts import Contract

    env, case = local_routine_env, local_routine_env["case"]

    async def preview(goal, **kwargs):
        return Contract(title=goal, goal=goal, steps=[{"title": "Requested preparation", "skill": skill}])

    monkeypatch.setattr(planner, "preview", preview)
    item = create(env, case, "every Friday " + goal)
    fire(env, case, item)
    env["settings"].update_settings("capabilities", {name: False}, {})
    local_worker_job(env, "tasks.execute")
    with env["factory"]() as session:
        child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
        assert child.state == "input_required"
        assert json.loads(child.blocker_json)["unblock_action"] == blocked_message(name)
        assert not session.scalar(select(VentureDocument.id))
    env["clock"][0] += timedelta(seconds=30)
    local_worker_job(env, "routines.report")
    with env["factory"]() as session:
        message = session.scalar(select(ChatMessage))
        assert blocked_message(name) in message.body_md
        assert not any("confirmation" in action for action in json.loads(message.actions_json or "[]"))
        env["service"].cancel(session, session.get(Task, child.id))
        session.commit()
    fire(env, case, item)
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))) == 1
        assert sum(blocked_message(name) in message.body_md for message in session.scalars(select(ChatMessage))) == 2


@pytest.mark.parametrize("changed", ["steps", "budget"])
def test_local_running_routine_keeps_the_saved_plan_and_budget(local_routine_env, changed):
    from app.db.models import VentureDocument
    from app.db.task_models import Task

    env, case = local_routine_env, local_routine_env["case"]
    item = create(env, case)
    fire(env, case, item)
    with env["factory"]() as session:
        parent = session.get(Task, item["id"])
        if changed == "steps":
            scope = json.loads(parent.scope_json)
            scope["steps"][0]["title"] = "Changed preparation"
            parent.scope_json = json.dumps(scope)
        else:
            budget = json.loads(parent.budget_json)
            budget["cost_micro"] += 1
            parent.budget_json = json.dumps(budget)
        session.commit()
    local_worker_job(env, "tasks.execute")
    with env["factory"]() as session:
        child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
        assert child.state == "input_required"
        assert "private chat" in json.loads(child.blocker_json)["unblock_action"]
        assert not session.scalar(select(VentureDocument.id))


@contextmanager
def founder(case):
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    with bind(workspace_id=case["workspace"], venture_id=case["venture"], actor=case["actor"]), bind_conversation(case["venture"], case["conversation"]):
        yield


def create(env, case, request="every Friday summarise replies", **kwargs):
    with founder(case):
        return asyncio.run(env["routines"].create(case["venture"], request, **kwargs))


def payload_for(env, identifier, kind="routines.fire"):
    from app.db.models import Job
    with env["factory"]() as session:
        jobs = session.scalars(select(Job).where(Job.kind == kind, Job.cancel_requested.is_(False)).order_by(Job.created_at.desc(), Job.id)).all()
        return next(json.loads(job.payload_json) for job in jobs if json.loads(job.payload_json)["routine_id"] == identifier)


def fire(env, case, item):
    payload = payload_for(env, item["id"])
    env["clock"][0] = datetime.fromisoformat(payload["due_at"])
    with founder(case):
        asyncio.run(env["routines"].fire(payload))
    return payload


def test_two_founders_have_separate_chat_schedules_leases_and_deduplicated_runs(routine_env):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    env = routine_env
    items = [create(env, case) for case in env["cases"]]
    assert [item["next_fire_at"] for item in items] == ["2026-10-09T13:00:00+00:00", "2026-10-09T03:30:00+00:00"]
    assert all(item["timezone"] == case["zone"] for item, case in zip(items, env["cases"]))
    for item, case in zip(items, env["cases"]):
        with founder(case):
            assert [saved["id"] for saved in env["routines"].list_routines(case["venture"])] == [item["id"]]
            with env["factory"]() as session:
                assert env["service"].start_run(session, session.get(Task, item["id"]), "manual") is None
                assert session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.task_id == item["id"])) == 0
            with pytest.raises(HTTPException):
                env["routines"].change(case["venture"], items[1 - env["cases"].index(case)]["id"])
        payload = fire(env, case, item)
        with founder(case):
            asyncio.run(env["routines"].fire(payload))
        with env["factory"]() as session:
            parent = session.get(Task, item["id"])
            children = session.scalars(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))).all()
            assert len(children) == 1
            assert children[0].owner_person_id == case["actor"]
            assert session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.task_id == children[0].id)) == 1
            assert parent.next_fire_at > env["clock"][0].replace(tzinfo=None)
            next_job = session.get(Job, json.loads(parent.trigger_json)["fire_job_id"])
            assert next_job.run_after == parent.next_fire_at
            assert json.loads(next_job.payload_json)["_actor_subject"] == case["actor"]
    with env["factory"]() as session:
        now = max(datetime.fromisoformat(item["next_fire_at"]) for item in items)
        claimed = env["jobs"].lease(session, "routine-worker-one", kinds=["routines.fire"], now=now, n=10)
        session.commit()
        assert len(claimed) == 2
    with env["factory"]() as session:
        assert env["jobs"].lease(session, "routine-worker-two", kinds=["routines.fire"], now=now, n=10) == []


def test_transient_start_failure_retries_the_same_occurrence_once(routine_env, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task
    env = routine_env
    original = env["routines"].service.create
    for case in env["cases"]:
        item = create(env, case)
        pending = payload_for(env, item["id"])
        env["clock"][0] = datetime.fromisoformat(pending["due_at"])
        attempts = []

        def interrupted(*args, **kwargs):
            child = original(*args, **kwargs)
            attempts.append(child.id)
            if len(attempts) == 1:
                raise RuntimeError("temporary storage interruption")
            return child

        with monkeypatch.context() as patch, founder(case):
            patch.setattr(env["routines"].service, "create", interrupted)
            with pytest.raises(RuntimeError, match="temporary storage interruption"):
                asyncio.run(env["routines"].fire(pending))
            with env["factory"]() as session:
                parent = session.get(Task, item["id"])
                assert parent.next_fire_at == env["clock"][0].replace(tzinfo=None)
                assert session.get(Job, json.loads(parent.trigger_json)["fire_job_id"]).max_attempts > 1
                assert session.scalar(select(func.count()).select_from(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))) == 0
                assert session.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key.startswith(f"routine-fire:{item['id']}:"))) == 1
            asyncio.run(env["routines"].fire(pending))
            asyncio.run(env["routines"].fire(pending))
        with env["factory"]() as session:
            assert session.scalar(select(func.count()).select_from(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))) == 1
            assert session.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key.startswith(f"routine-report:{item['id']}:"))) == 1
            assert session.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key.startswith(f"routine-fire:{item['id']}:"))) == 2
            assert session.get(Task, item["id"]).next_fire_at > env["clock"][0].replace(tzinfo=None)
        assert len(attempts) == 2


def test_worker_executes_each_founders_leased_routine_once(routine_env, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task
    from app.worker.handlers import HANDLERS
    env = routine_env
    monkeypatch.setitem(HANDLERS, "routines.fire", env["routines"].fire)
    for case in env["cases"]:
        item = create(env, case)
        env["clock"][0] = datetime.fromisoformat(item["next_fire_at"])
        with env["factory"]() as session:
            leased = env["jobs"].lease(session, "routine-worker", kinds=["routines.fire"], now=env["clock"][0], n=10)
            session.commit()
        assert len(leased) == 1
        with founder(case):
            asyncio.run(env["worker"]._run_one(env["factory"], leased[0], set()))
            asyncio.run(env["worker"]._run_one(env["factory"], leased[0], set()))
        with env["factory"]() as session:
            assert session.get(Job, leased[0].id).status == "succeeded"
            children = session.scalars(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))).all()
            assert len(children) == 1 and children[0].owner_person_id == case["actor"]


@pytest.mark.parametrize("zone,instruction,expected", [
    ("America/New_York", "every Sunday at 2:30 summarise replies", "2026-03-15T06:30:00+00:00"),
    ("Europe/Berlin", "every Sunday at 2:30 summarise replies", "2026-04-05T00:30:00+00:00"),
])
def test_local_schedule_skips_dst_gaps_with_a_simulated_clock(routine_env, zone, instruction, expected):
    env = routine_env
    env["clock"][0] = datetime(2026, 3, 7 if zone == "America/New_York" else 28, 12, tzinfo=timezone.utc)
    for case in env["cases"]:
        item = create(env, case, instruction, timezone=zone, quiet={"start": "22:00", "end": "01:00"})
        assert item["next_fire_at"] == expected


def test_routine_api_controls_validate_and_preserve_founder_scope(routine_env):
    from app.api import routines_api
    env = routine_env
    for case in env["cases"]:
        with founder(case):
            item = asyncio.run(routines_api.create_routine(case["venture"], routines_api.RoutineBody(
                goal="every morning summarise replies")))
            assert routines_api.list_routines(case["venture"])["items"][0]["id"] == item["id"]
            with pytest.raises(HTTPException) as invalid:
                routines_api.save_settings(case["venture"], item["id"], routines_api.RoutineSettings(
                    timezone="Not/AZone", quiet_hours={"start": "22:00", "end": "08:00"}))
            assert invalid.value.status_code == 422
            saved = routines_api.save_settings(case["venture"], item["id"], routines_api.RoutineSettings(
                timezone="UTC", quiet_hours={"start": "21:00", "end": "07:00"}))
            assert saved["timezone"] == "UTC" and saved["quiet_hours"]["end"] == "07:00"
            other = env["cases"][1 - env["cases"].index(case)]
            with founder(other), pytest.raises(HTTPException):
                routines_api.delete_routine(case["venture"], item["id"])
            assert routines_api.pause_routine(case["venture"], item["id"])["state"] == "paused"
            assert routines_api.delete_routine(case["venture"], item["id"])["state"] == "canceled"
            assert routines_api.list_routines(case["venture"])["items"] == []


@pytest.mark.parametrize("zone,moment,end,expected", [
    ("America/New_York", "2026-11-01T06:15:00+00:00", "01:30", "2026-11-01T06:30:00+00:00"),
    ("Europe/Berlin", "2026-10-25T01:15:00+00:00", "02:30", "2026-10-25T01:30:00+00:00"),
])
def test_quiet_hours_end_in_the_current_dst_fold_not_an_earlier_occurrence(routine_env, zone, moment, end, expected):
    env = routine_env
    for case in env["cases"]:
        env["clock"][0] = datetime.fromisoformat(moment)
        trigger = {"tz": zone, "quiet_hours": {"start": "00:00", "end": end}}
        actual = env["routines"].outside_quiet(env["clock"][0], trigger)
        assert actual.replace(tzinfo=timezone.utc).isoformat() == expected


def test_quiet_hours_hold_schedule_and_late_delivery_and_settings_replace_the_job(routine_env):
    from app.db.models import Job
    from app.db.task_models import Task
    env = routine_env
    for case in env["cases"]:
        env["clock"][0] = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        item = create(env, case, "every Friday at 23 summarise replies")
        assert datetime.fromisoformat(item["next_fire_at"]).astimezone(ZoneInfo(case["zone"])).hour == 8
        old = payload_for(env, item["id"])
        with founder(case):
            changed = env["routines"].settings(case["venture"], item["id"], timezone="UTC", quiet={"start": "21:00", "end": "07:00"})
        assert changed["next_fire_at"] == "2026-10-10T07:00:00+00:00"
        with env["factory"]() as session:
            jobs = session.scalars(select(Job).where(Job.kind == "routines.fire")).all()
            assert next(job for job in jobs if json.loads(job.payload_json) == old).cancel_requested
        env["clock"][0] = datetime(2026, 10, 10, 23, tzinfo=timezone.utc)
        current = payload_for(env, item["id"])
        with founder(case):
            asyncio.run(env["routines"].fire(current))
        with env["factory"]() as session:
            assert session.get(Task, item["id"]).next_fire_at == datetime(2026, 10, 11, 7)
        assert len(env["plans"]) == env["cases"].index(case) + 1


def test_quiet_hours_defer_new_steps_without_extending_approved_budgets(routine_env, monkeypatch):
    from app.approvals import standing
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import execution
    env, approvals = routine_env, []
    for case in env["cases"]:
        item = create(env, case)
        with founder(case):
            env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
        fire(env, case, item)
        with env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            run_id, started, deadline = run.id, run.started_at, run.deadline_at
        local = env["clock"][0].astimezone(ZoneInfo(case["zone"])).replace(hour=22, minute=0)
        env["clock"][0] = local.astimezone(timezone.utc)
        monkeypatch.setattr(standing, "auto_approve", lambda **kwargs: approvals.append(kwargs) or object())
        with founder(case):
            asyncio.run(execution.execute({"workspace_id": case["workspace"], "venture_id": case["venture"], "task_run_id": run_id}))
        with env["factory"]() as session:
            run = session.get(TaskRun, run_id)
            held_job = session.get(Job, run.control_job_id)
            held = held_job.run_after.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(case["zone"]))
            assert held.hour == 8 and held.date() == local.date() + timedelta(days=1)
            assert (run.started_at, run.deadline_at) == (started, deadline)
            assert all(step.state == "submitted" for step in session.scalars(select(TaskStep).where(TaskStep.run_id == run_id)))
            assert not approvals
        with founder(case):
            env["routines"].change(case["venture"], item["id"])
        with env["factory"]() as session:
            assert session.get(Job, held_job.id).cancel_requested


def test_same_time_settings_edits_replace_canceled_jobs_and_fence_already_leased_generations(routine_env):
    from app.db.models import Job
    from app.db.task_models import Task
    env = routine_env
    for case in env["cases"]:
        env["clock"][0] = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        item = create(env, case)
        old = payload_for(env, item["id"])
        with founder(case):
            changed = env["routines"].settings(case["venture"], item["id"], timezone=case["zone"], quiet={"start": "21:00", "end": "07:00"})
        assert changed["next_fire_at"] == item["next_fire_at"]
        current = payload_for(env, item["id"])
        assert current["generation"] != old["generation"]
        env["clock"][0] = datetime.fromisoformat(current["due_at"])
        with founder(case):
            asyncio.run(env["routines"].fire(old))
            assert len(env["plans"]) == env["cases"].index(case) + 1
            asyncio.run(env["routines"].fire(current))
        with env["factory"]() as session:
            child_count = session.scalar(select(func.count()).select_from(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            assert child_count == 1
            assert any(job.cancel_requested for job in session.scalars(select(Job).where(Job.kind == "routines.fire"))
                if json.loads(job.payload_json)["routine_id"] == item["id"])


def test_settings_outside_chat_retain_the_original_private_job_partition(routine_env, monkeypatch):
    from app.chat import conversations
    from app.tenancy import bind
    env = routine_env
    for case in env["cases"]:
        item = create(env, case)

        def capture(venture_id, workspace_id):
            scope = conversations.current_conversation()
            assert scope.id == case["conversation"] and scope.visibility == "private"
            assert scope.actor_id == case["actor"] and scope.workspace_id == workspace_id and scope.venture_id == venture_id
            return {"schema_version": 1, "conversation_id": scope.id, "venture_id": venture_id,
                    "workspace_id": workspace_id, "actor_id": scope.actor_id}

        monkeypatch.setattr(conversations, "capture_job_conversation", capture)
        with bind(workspace_id=case["workspace"], venture_id=case["venture"], actor=case["actor"]):
            assert conversations.current_conversation() is None
            env["routines"].settings(case["venture"], item["id"], timezone=case["zone"], quiet={"start": "21:00", "end": "07:00"})
            assert conversations.current_conversation() is None
        payload = payload_for(env, item["id"])
        assert payload["_chat_conversation"]["conversation_id"] == case["conversation"]
        monkeypatch.setattr(conversations, "capture_job_conversation", lambda *args: None)


@pytest.mark.parametrize("operation", ["pause", "delete"])
def test_pause_or_delete_immediately_cancels_children_and_already_leased_jobs(routine_env, operation):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    env = routine_env
    for case in env["cases"]:
        item = create(env, case)
        payload = fire(env, case, item)
        report_payload = payload_for(env, item["id"], "routines.report")
        with founder(case):
            result = env["routines"].change(case["venture"], item["id"], delete=operation == "delete")
            asyncio.run(env["routines"].fire(payload))
            env["routines"].report(report_payload)
        assert result["state"] == ("paused" if operation == "pause" else "canceled")
        with env["factory"]() as session:
            children = session.scalars(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))).all()
            assert all(child.state == "canceled" for child in children)
            for child in children:
                run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
                assert run.state == "canceled"
                assert all(step.state == "canceled" for step in session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)))
                assert session.get(Job, run.control_job_id).cancel_requested
            assert session.get(Task, item["id"]).next_fire_at is None
        with founder(case):
            listed = env["routines"].list_routines(case["venture"])
            assert bool(listed) == (operation == "pause")
    assert not env["posts"]


def test_hard_rules_never_auto_run_even_if_a_standing_provider_returns_approval(routine_env, monkeypatch):
    from app.approvals import standing
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.contracts import Contract
    from app.core.local_mode import is_local
    env, auto_calls = routine_env, []
    monkeypatch.setattr(standing, "auto_approve", lambda **kwargs: auto_calls.append(kwargs) or object())
    for case in env["cases"]:
        parent = create(env, case)
        for skill in ("pagebuilder.publish", "device.work_order", "task.run"):
            with founder(case), env["factory"]() as session:
                child = env["service"].create(session, workspace_id=case["workspace"], venture_id=case["venture"], owner_person_id=case["actor"],
                    contract=Contract(title="Exact action review", goal="Review your next action", steps=[{"title": "Review", "skill": skill}]), start=False)
                scope = json.loads(child.scope_json)
                scope["routine_id"] = parent["id"]
                child.scope_json = json.dumps(scope)
                run = env["service"].start_run(session, child, "initial")
                step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
                assert not env["routines"].admit_step(session, child, run, step)
                session.commit()
                payload = {"task_run_id": run.id, "workspace_id": case["workspace"], "venture_id": case["venture"]}
            with founder(case):
                asyncio.run(env["execution"].execute(payload))
            with env["factory"]() as session:
                assert session.get(Task, child.id).state == ("input_required" if is_local() else "approval_required")
                assert session.get(TaskStep, step.id).state == ("submitted" if is_local() else "approval_required")
                if is_local():
                    assert "private chat" in json.loads(session.get(Task, child.id).blocker_json)["unblock_action"]
    assert auto_calls == []


def test_real_standing_approvals_are_owner_scoped_expiring_and_revocable(routine_env, monkeypatch):
    from app.approvals import standing
    from app.db.models import Approval
    from app.db.task_models import Task, TaskRun, TaskStep
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    env = routine_env
    for case in env["cases"]:
        item = create(env, case)
        fire(env, case, item)
        with founder(case), env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
            assert not env["routines"].admit_step(session, child, run, step)
        with founder(case):
            env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
            with env["factory"]() as session:
                assert env["routines"].admit_step(session, session.get(Task, child.id), session.get(TaskRun, run.id), session.get(TaskStep, step.id))
                session.commit()
            grant = standing.list_grants(case["workspace"])[0]
            standing.revoke(grant.id, case["actor"])
            with env["factory"]() as session:
                assert not env["routines"].admit_step(session, session.get(Task, child.id), session.get(TaskRun, run.id), session.get(TaskStep, step.id))


            env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
            grant = standing.list_grants(case["workspace"])[0]
            with env["factory"]() as session:
                session.get(Approval, grant.id).expires_at = env["clock"][0].replace(tzinfo=None) - timedelta(seconds=1)
                session.commit()
            with env["factory"]() as session:
                assert not env["routines"].admit_step(session, session.get(Task, child.id), session.get(TaskRun, run.id), session.get(TaskStep, step.id))


def test_goal_review_cards_reuse_live_routine_grants_but_never_authorize_hard_actions(routine_env, monkeypatch):
    from app.chat import confirmations, store
    from app.chat.execute import _approve_standing_card
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import goals, planner
    from app.tasks.contracts import Contract
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    env, dispatched = routine_env, []
    env["flags"]["goals_v1"] = True

    async def auto_dispatch(venture_id, message, actor):
        dispatched.append((venture_id, message, actor))
        return message

    monkeypatch.setattr(confirmations, "auto_dispatch", auto_dispatch)
    monkeypatch.setattr(store, "append_message", lambda venture_id, **kwargs: {"id": "goal-review:" + venture_id, "actions": kwargs["actions"]})
    for case in env["cases"]:
        async def preview(goal, **kwargs):
            return Contract(title=goal[:160], goal=goal,
                done_test={"type": "deterministic", "spec": {"predicate": "all_steps_verified"}},
                steps=[{"title": "Find current public sources", "skill": "gtm.find", "subgoal": "Find public sources for " + case["domain"],
                    "inputs": {"query": case["domain"]}, "done_test": {"type": "deterministic", "spec": {"predicate": "workflow_receipt"}}}])

        monkeypatch.setattr(planner, "preview", preview)
        item = create(env, case, "every Monday at 9, Research public sources for " + case["domain"])
        with founder(case):
            env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
        fire(env, case, item)
        with env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        with founder(case):
            card = {"action": "gtm.find", "confirmation": {"action": "gtm.find", "resulting_state": {"quote_fingerprint": "saved-quote", "ceiling_micro": 1000}}}
            asyncio.run(goals.park(step.id, {"body_md": "Review your current public-source search."}, card))
            assert dispatched[-1][0] == case["venture"] and dispatched[-1][2] == case["actor"]
            payload = dispatched[-1][1]["actions"][0]["confirmation"]["resulting_state"]
            grant_id = env["routines"].matching_card_grant(case["workspace"], case["venture"], case["actor"], "gtm.find", payload)
            assert grant_id
            with env["factory"]() as session:
                _approve_standing_card(case["venture"], "gtm.find", payload, case["actor"], grant_id, message_id=dispatched[-1][1]["id"], session=session)
                for hard_action in ("experiment.run", "outreach.send", "pagebuilder.publish"):
                    assert env["routines"].reviewed_card_action(session, case["venture"], case["actor"], hard_action, payload) is None
                assert env["routines"].reviewed_card_action(session, case["venture"], case["actor"], "gtm.find", {**payload, "ceiling_micro": 50_000_001}) is None
                assert env["routines"].reviewed_card_action(session, case["venture"], "another-founder", "gtm.find", payload) is None
                parent = session.get(Task, item["id"])
                trigger = parent.trigger_json
                values = json.loads(trigger)
                values["quiet_hours"] = {"start": "09:00", "end": "10:00"}
                parent.trigger_json = json.dumps(values)
                assert env["routines"].reviewed_card_action(session, case["venture"], case["actor"], "gtm.find", payload) is None
                parent.trigger_json = trigger
                session.commit()


def test_disabling_routines_discards_delayed_goal_reviews_for_both_founders(routine_env, monkeypatch):
    configure_local_test_model()
    from app.chat import store
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import goals, planner
    from app.tasks.contracts import Contract
    env, posted = routine_env, []
    env["flags"]["goals_v1"] = True
    monkeypatch.setattr(store, "append_message", lambda *args, **kwargs: posted.append(kwargs))
    for case in env["cases"]:
        async def preview(goal, **kwargs):
            return Contract(title=goal[:160], goal=goal,
                done_test={"type": "deterministic", "spec": {"predicate": "all_steps_verified"}},
                steps=[{"title": "Find current public sources", "skill": "gtm.find", "subgoal": "Find public sources for " + case["domain"],
                    "inputs": {"query": case["domain"]}, "done_test": {"type": "deterministic", "spec": {"predicate": "workflow_receipt"}}}])

        monkeypatch.setattr(planner, "preview", preview)
        set_routines_enabled(env, True)
        item = create(env, case, "every Monday at 9, Research public sources for " + case["domain"])
        fire(env, case, item)
        with env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        set_routines_enabled(env, False)
        with founder(case):
            asyncio.run(goals.park(step.id, {"body_md": "Your public-source search is ready for review."},
                {"confirmation": {"action": "gtm.find", "resulting_state": {"query": case["domain"]}}}))
        with env["factory"]() as session:
            assert session.get(Task, child.id).state == "canceled"
            assert session.get(TaskRun, run.id).state == "canceled"
            assert session.get(TaskStep, step.id).state == "canceled"
    assert not posted


def test_changed_routine_definitions_need_a_fresh_saved_approval_card(routine_env):
    from app.db.task_models import Task
    from app.core.local_mode import is_local
    env = routine_env
    for case in env["cases"]:
        item = create(env, case)
        with env["factory"]() as session:
            session.get(Task, item["id"]).goal = "summarize my waitlist and outreach replies"
            session.commit()
        with founder(case):
            with pytest.raises(ValueError, match="Your routine changed"):
                env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
            reply = asyncio.run(env["routines"].action(case["venture"], "routines.list", {}))
            if is_local():
                assert reply["actions"] == []
                current = env["routines"].list_routines(case["venture"])[0]
                payload = {"routine_id": current["id"], "approval_digest": current["approval_digest"]}
            else:
                payload = reply["actions"][0]["confirmation"]["resulting_state"]
            assert payload["approval_digest"] != item["approval_digest"]
            approved = asyncio.run(env["routines"].action(case["venture"], "routines.approve", payload))
            assert "standing approval" in approved["body_md"]


@pytest.mark.parametrize("changed", ["steps", "budget"])
def test_changed_saved_steps_and_budget_require_a_fresh_standing_approval(routine_env, changed):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.core.local_mode import is_local
    env = routine_env
    for case in env["cases"]:
        item = create(env, case)
        with founder(case):
            env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
        with env["factory"]() as session:
            parent = session.get(Task, item["id"])
            if changed == "steps":
                scope = json.loads(parent.scope_json)
                scope["steps"][0]["title"] = "Summarize current saved replies"
                parent.scope_json = json.dumps(scope)
            else:
                budget = json.loads(parent.budget_json)
                budget["tokens"] -= 1
                parent.budget_json = json.dumps(budget)
            session.commit()
        fire(env, case, item)
        with founder(case), env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
            assert env["routines"].admit_step(session, child, run, step) is is_local()
        with founder(case):
            with pytest.raises(ValueError, match="Your routine changed"):
                env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
            refreshed = env["routines"].list_routines(case["venture"])[0]
            env["routines"].approve(case["venture"], item["id"], refreshed["approval_digest"])
            with env["factory"]() as session:
                assert env["routines"].admit_step(session, session.get(Task, child.id), session.get(TaskRun, run.id), session.get(TaskStep, step.id))


def test_results_return_to_the_originating_private_chat_once_with_fresh_one_shot_runs(routine_env):
    from app.db.task_models import Task, TaskRun
    env = routine_env
    for case in env["cases"]:
        env["clock"][0] = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        item = create(env, case)
        with founder(case):
            env["routines"].approve(case["venture"], item["id"], item["approval_digest"])
        fire(env, case, item)
        with env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
        with founder(case):
            asyncio.run(env["execution"].execute({"task_run_id": run.id, "workspace_id": case["workspace"], "venture_id": case["venture"]}))
            report = payload_for(env, item["id"], "routines.report")
            env["routines"].report(report)
            env["routines"].report(report)
        assert f"routine-result:{child.id}" in env["posts"]
        assert "replies" in env["posts"][f"routine-result:{child.id}"][1]["body_md"].lower()
        fire(env, case, item)
        with env["factory"]() as session:
            assert session.scalar(select(func.count()).select_from(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))) == 2
    assert len(env["posts"]) == 2
    assert len(env["plans"]) == 2


def test_flag_off_preserves_chat_config_and_stops_queued_execution(routine_env):
    from app import ui_config
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.core.local_mode import is_local
    env = routine_env
    items = [create(env, case) for case in env["cases"]]
    for item, case in zip(items, env["cases"]):
        fire(env, case, item)
    set_routines_enabled(env, False)
    env["flags"].clear()
    from app.tasks import planner
    from app.tasks.contracts import Contract
    for case in env["cases"]:
        selected = Contract(title=case["domain"], goal="summarise replies", steps=[{"title": "Summary", "skill": "tasks.waitlist_summary"}])
        assert planner.validate_contract(selected, goal=selected.goal).steps[0].skill == "task.run"
    assert ui_config.ui_config(env["cases"][0]["actor"]) == {"founder_ui": "chat"}
    assert env["routines"].command("every Friday summarise replies") is None
    for item, case in zip(items, env["cases"]):
        with founder(case):
            asyncio.run(env["routines"].fire(payload_for(env, item["id"])))
            with pytest.raises(HTTPException) as error:
                env["routines"].list_routines(case["venture"])
            assert error.value.status_code == (403 if is_local() else 404)
            if is_local():
                assert "Settings > Capabilities" in error.value.detail
            with env["factory"]() as session:
                child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
                run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
                step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
                assert not env["routines"].admit_step(session, child, run, step)
    assert not env["posts"]


def test_disabling_and_reenabling_skips_work_without_losing_the_next_schedule(routine_env):
    from app.db.models import Job
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskRun
    env = routine_env
    items = [create(env, case) for case in env["cases"]]
    for item, case in zip(items, env["cases"]):
        fire(env, case, item)
    set_routines_enabled(env, False)
    for item, case in zip(items, env["cases"]):
        pending = payload_for(env, item["id"])
        env["clock"][0] = datetime.fromisoformat(pending["due_at"])
        with founder(case):
            asyncio.run(env["routines"].fire(pending))
            with env["factory"]() as session:
                child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
                run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            asyncio.run(env["execution"].execute({"task_run_id": run.id, "workspace_id": case["workspace"], "venture_id": case["venture"]}))
        with env["factory"]() as session:
            assert session.get(Task, child.id).state == "canceled"
            parent = session.get(Task, item["id"])
            assert parent.next_fire_at > env["clock"][0].replace(tzinfo=None)
            assert not session.get(Job, json.loads(parent.trigger_json)["fire_job_id"]).cancel_requested
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Notice)) == 0
    set_routines_enabled(env, True)
    for item, case in zip(items, env["cases"]):
        fire(env, case, item)
        with env["factory"]() as session:
            assert session.scalar(select(func.count()).select_from(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:"))) == 2
    assert len(env["plans"]) == 2 and not env["posts"]


def test_invalid_schedules_and_quiet_hours_are_rejected_before_planning(routine_env):
    env = routine_env
    for case in env["cases"]:
        for changes in ({"timezone": "No/Such_Zone"}, {"quiet": {"start": "22:00", "end": "22:00"}},
                        {"quiet": {"start": "25:00", "end": "08:00"}}, {"quiet": {"start": "22:00"}}):
            with pytest.raises(ValueError):
                create(env, case, **changes)
        with pytest.raises(ValueError):
            create(env, case, "every Friday at 27 summarise replies")
    assert env["plans"] == []


def test_foreign_job_payloads_and_legacy_shared_chat_cannot_gain_routine_authority(routine_env, monkeypatch):
    from app.approvals import standing
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tenancy import bind
    env = routine_env
    items = [create(env, case) for case in env["cases"]]
    checks = []
    monkeypatch.setattr(standing, "auto_approve", lambda **kwargs: checks.append(kwargs) or object())
    for item, case in zip(items, env["cases"]):
        payload = payload_for(env, item["id"])
        foreign = env["cases"][1 - env["cases"].index(case)]
        payload["_actor_subject"] = foreign["actor"]
        with founder(foreign), pytest.raises(PermissionError):
            asyncio.run(env["routines"].fire(payload))
        fire(env, case, item)
        with founder(case), env["factory"]() as session:
            child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
            step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
            scope = json.loads(child.scope_json)
            scope["routine_id"] = items[1 - env["cases"].index(case)]["id"]
            child.scope_json = json.dumps(scope)
            assert not env["routines"].admit_step(session, child, run, step)
        with bind(workspace_id=case["workspace"], venture_id=case["venture"], actor=case["actor"]), pytest.raises(PermissionError):
            asyncio.run(env["routines"].create(case["venture"], "every Friday summarise replies"))
    assert checks == []


def test_chat_controls_prefer_exact_titles_and_accept_sentence_punctuation(routine_env):
    from app.db.task_models import Task
    env = routine_env
    for case in env["cases"]:
        plain, punctuated = create(env, case), create(env, case)
        with env["factory"]() as session:
            session.get(Task, plain["id"]).title = "Saved digest"
            session.get(Task, punctuated["id"]).title = "Saved digest."
            session.commit()
        with founder(case):
            name, payload = env["routines"].command("pause routine Saved digest.")
            asyncio.run(env["routines"].action(case["venture"], name, payload))
            saved = {item["id"]: item for item in env["routines"].list_routines(case["venture"])}
            assert saved[plain["id"]]["state"] == "submitted"
            assert saved[punctuated["id"]]["state"] == "paused"
            name, payload = env["routines"].command(f"delete routine {plain['id']}.")
            asyncio.run(env["routines"].action(case["venture"], name, payload))
            assert [item["id"] for item in env["routines"].list_routines(case["venture"])] == [punctuated["id"]]
            with pytest.raises(HTTPException):
                env["routines"].change(case["venture"], {"invalid": "identifier"})


def test_shared_chat_cannot_disclose_or_control_private_routines(routine_env):
    from app.tenancy import bind
    env = routine_env
    for case in env["cases"]:
        item = create(env, case)
        with bind(workspace_id=case["workspace"], venture_id=case["venture"], actor=case["actor"]):
            for name, payload in (("routines.list", {}), ("routines.pause", {"routine_id": item["id"]}),
                    ("routines.delete", {"routine_id": item["id"]}),
                    ("routines.approve", {"routine_id": item["id"], "approval_digest": item["approval_digest"]}),
                    ("routines.create", {"goal": "every Friday summarise replies"})):
                with pytest.raises(HTTPException) as denied:
                    asyncio.run(env["routines"].action(case["venture"], name, payload))
                assert denied.value.status_code == 403
        with founder(case):
            assert env["routines"].list_routines(case["venture"])[0]["state"] == "submitted"
    assert len(env["plans"]) == len(env["cases"])


def test_chat_commands_approval_confirmation_and_additive_migration_are_registered(routine_env, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat.actions import get_action
    from app.chat.execute import _execute_action
    from app.core.local_mode import is_local
    env = routine_env
    for case in env["cases"]:
        with founder(case):
            reply = asyncio.run(env["routines"].action(case["venture"], "routines.create", {"goal": "every Friday summarise replies"}))
            if is_local():
                assert reply["actions"] == []
            else:
                assert reply["actions"][0]["confirmation"]["action"] == "routines.approve"
                with pytest.raises(ValueError, match="chat_confirmation_required"):
                    asyncio.run(_execute_action(case["venture"], "routines.approve", payload=reply["actions"][0]["confirmation"]["resulting_state"]))
        for text, name in (("list my routines", "routines.list"), ("pause routine Saved work", "routines.pause"),
                           ("delete routine Saved work", "routines.delete")):
            assert env["routines"].command(text)[0] == name
    for name in ("routines.create", "routines.list", "routines.pause", "routines.delete", "routines.approve"):
        assert get_action(name).name == name and ACTION_SLUG[name] == "settings"
    assert get_action("routines.approve").governed_capability == get_action("work.start").governed_capability
    migration = importlib.import_module("app.db.migrations.versions.s063routines")
    assert migration.revision == "s063routines" and migration.down_revision == "s062spend"
    calls = []
    monkeypatch.setattr(migration, "op", SimpleNamespace(create_index=lambda *args, **kwargs: calls.append((args, kwargs)),
        drop_index=lambda *args, **kwargs: calls.append((args, kwargs)),
        get_bind=lambda: SimpleNamespace(scalar=lambda statement: False)))
    migration.upgrade()
    migration.downgrade()
    assert calls[0][0][1] == "tasks" and calls[1][0][0] == calls[0][0][0]


@pytest.mark.parametrize("skill", ["gtm.find", "outreach.draft"])
def test_local_native_goal_routine_dispatches_prepared_work_without_a_card(local_routine_env, monkeypatch, skill):
    from app.chat import execute
    from app.db.models import ChatMessage, Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import goals, planner
    from app.tasks.contracts import Contract

    env, case, calls = local_routine_env, local_routine_env["case"], []
    assert goals.enabled()
    inputs = {"query": "Find customers for " + case["domain"]} if skill == "gtm.find" else {"contact_id": "saved-contact"}
    prepared = {"run": True, **inputs}
    card = {"kind": "confirmation", "body_md": "Your requested preparation is ready.",
        "actions": [{"confirmation": {"action": skill, "resulting_state": prepared}}]}

    async def preview(goal, **kwargs):
        return Contract(title=goal, goal=goal, steps=[{"title": goal, "skill": skill, "subgoal": goal, "inputs": inputs}])

    async def find_reply(venture_id, query, *, start_requested):
        assert not start_requested
        return card

    async def execute_action(venture_id, action, *, payload, actor_subject):
        if payload.get("run") is not True:
            return card
        calls.append((venture_id, action, payload, actor_subject))
        with env["factory"]() as session:
            identifier = env["jobs"].enqueue(session, "gtm.journey.prepare" if action == "gtm.find" else action,
                inputs, workspace_id=case["workspace"], venture_id=venture_id)
            session.commit()
        return {"kind": "text", "job_id": identifier}

    monkeypatch.setattr(planner, "preview", preview)
    monkeypatch.setattr(execute, "find_customers_reply", find_reply)
    monkeypatch.setattr(execute, "execute_action", execute_action)
    item = create(env, case, "every Friday " + ("find customers" if skill == "gtm.find" else "prepare drafts for my saved contact"))
    fire(env, case, item)
    local_worker_job(env, "tasks.execute")
    assert calls == [(case["venture"], skill, prepared, case["actor"])]
    with env["factory"]() as session:
        child = session.scalar(select(Task).where(Task.create_key.startswith(f"routine-run:{item['id']}:")))
        step = session.scalar(select(TaskStep).where(TaskStep.run_id.in_(select(TaskRun.id).where(TaskRun.task_id == child.id))))
        assert step.job_id and step.state == "verifying"
        assert session.get(Job, step.job_id).venture_id == case["venture"]
        assert not session.scalar(select(ChatMessage.id))
