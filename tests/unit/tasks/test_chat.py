"""Synthetic private source rows and real task/outbox writes; planner transport only is fake."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def chat_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app import state
    from app.chat import conversations
    from app.db.models import Base, ChatConversation, ChatMessage, CreditPolicy, Job, Person, Venture, VentureDocument, Workspace
    from app.db.supymem_models import Experiment, IdeaRevision, IdeaSession, Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import chat
    from core.clock import set_clock
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, VentureDocument, IdeaSession, IdeaRevision, Experiment,
        ChatConversation, ChatMessage, CreditPolicy, Job, Notice, Task, TaskRun, TaskStep, TaskEvent)])
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(state, "_factory", factory)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(chat, "db", lambda: factory)
    monkeypatch.setattr(conversations, "db", lambda: factory)
    clock = [datetime(2026, 10, 8, 12, tzinfo=timezone.utc)]
    set_clock(lambda: clock[0])
    with factory() as session:
        now = clock[0]
        for person in ("usr_chat", "usr_other"):
            session.add(Person(id=person, display_name="Synthetic founder", timezone="Asia/Kolkata", created_at=now))
        session.flush()
        session.add(Workspace(id="wsp_chat", owner_id="usr_chat", name="Synthetic", kind="company", entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_chat", workspace_id="wsp_chat", name="Synthetic", raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.flush()
        session.add(ChatConversation(id="cnv_private", venture_id="ven_chat", workspace_id="wsp_chat", creator_person_id="usr_chat",
            visibility="private", title="Synthetic private chat", creation_key="synthetic-private", created_at=now, updated_at=now))
        session.commit()
    calls, mode = [], {"skill": "tasks.waitlist_summary", "owner_change": False, "value": "ok", "during": None}
    transport = ModuleType("app.credits.metered")
    from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt
    recovery = install_recovery(monkeypatch)

    async def llm(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append((payload, kwargs))
        with factory() as session:
            task = next(row for row in session.scalars(select(Task))
                if json.loads(row.scope_json).get("_planning", {}).get("request_id") == kwargs["request_id"])
            journal = json.loads(task.scope_json)["_planning"]
            assert journal["state"] == "calling" and journal["start"] is False
            assert journal["source_key"] == task.create_key and journal["owner_person_id"] == "usr_chat"
            assert session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.task_id == task.id)) == 0
            task_id = task.id
        if mode["during"]:
            mode["during"](task_id)
        if mode["value"] == "timeout":
            raise TimeoutError("Private provider body must not be persisted")
        if mode["value"] == "calling_crash":
            raise KeyboardInterrupt()
        if mode["owner_change"]:
            with factory() as session:
                session.get(Workspace, "wsp_chat").owner_id = "usr_other"
                session.commit()
        from app.tasks import goals
        contract = {"title": "Saved summary", "goal": payload["goal"], "steps": [{"title": "Prepare the requested result",
            "skill": mode["skill"], **({"subgoal": payload["goal"]} if goals.enabled() else {})}]}
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(contract)))])
        receipt = synthetic_receipt(kwargs)
        checkpoint(response, kwargs, receipt)
        recovery.confirm(receipt)
        if mode["value"] == "after_exit_error":
            from app.credits.errors import CreditsUnavailable
            raise CreditsUnavailable("Synthetic metering exit failed after the funded response checkpoint")
        return response

    transport.llm = llm
    monkeypatch.setitem(sys.modules, transport.__name__, transport)
    yield {"factory": factory, "chat": chat, "conversations": conversations, "clock": clock, "calls": calls, "mode": mode, "recovery": recovery}
    set_clock(None)
    engine.dispose()


def source(d, text, *, id="msg_source", **changes):
    from app.db.models import ChatMessage
    with d["factory"]() as session:
        session.add(ChatMessage(**{"id": id, "venture_id": "ven_chat", "workspace_id": "wsp_chat", "conversation_id": "cnv_private",
            "author_person_id": "usr_chat", "role": "user", "kind": "text", "body_md": text,
            "created_at": d["clock"][0], **changes}))
        session.commit()
    return id


def call(d, text, source_id="msg_source"):
    from app.tenancy import bind
    with bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"), d["conversations"].bind_conversation("ven_chat", "cnv_private"):
        return asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id=source_id))


def count(d, model):
    with d["factory"]() as session:
        return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("idea", ["Shift planning for repair shops", "Trail reminders for weekend hikers"])
def test_cached_db_alias_uses_the_injected_task_factory(chat_db, idea):
    from app.db.models import Venture
    from app.workspaces.service import db as workspace_db

    with chat_db["factory"]() as session:
        session.get(Venture, "ven_chat").raw_idea = idea
        session.commit()
    assert workspace_db() is chat_db["factory"]
    with workspace_db()() as session:
        assert session.get(Venture, "ven_chat").raw_idea == idea


@pytest.mark.parametrize("text", ["Do you know my audience?", "Do we have a website", "Do research my venture?", "When can you finish", "Every founder needs a website", "hello"])
def test_questions_and_ordinary_messages_are_not_task_mutation_candidates(chat_db, text):
    assert chat_db["chat"].recognizes(text) is False
    assert asyncio.run(chat_db["chat"].handle_message("ven_chat", text, source_message_id="missing")) is None
    assert chat_db["calls"] == []


@pytest.mark.parametrize("idea", ["Rental inspections for property managers", "Yoga classes for beginners"])
def test_read_only_one_time_creates_real_outbox_once_from_saved_private_source(chat_db, idea):
    from app.db.models import ChatMessage, Job, Venture
    from app.db.task_models import Task
    from app.tasks import goals
    d = chat_db
    with d["factory"]() as session:
        session.get(Venture, "ven_chat").raw_idea = idea
        session.commit()
    text = "Do summarize waitlist sign-ups and replies"
    source(d, text)
    first = call(d, text)
    d["clock"][0] += timedelta(days=1)
    replay = call(d, text)
    assert len(d["calls"]) == 1 and d["calls"][0][1]["request_id"].startswith("task-plan:")
    assert count(d, Task) == count(d, Job) == count(d, ChatMessage) == 1
    assert ("Deadline:" if goals.enabled() else "Deadline estimate:") in first["body_md"]
    assert "IST" in first["body_md"] and 'Say "stop task Saved summary"' in first["body_md"]
    assert "already saved" in replay["body_md"]
    with d["factory"]() as session:
        task, job = session.scalar(select(Task)), session.scalar(select(Job))
        assert first["actions"] == [{"label": "Open Now", "href": "/v/ven_chat/now", "style": "primary",
                                     **({"task_id": task.id} if goals.enabled() else {})}]
        assert task.kind == "one_time" and job.kind == "tasks.execute"
        payload = json.loads(job.payload_json)
        assert payload["_actor_subject"] == "usr_chat" and payload["_chat_conversation"]["conversation_id"] == "cnv_private"


def test_explicit_frequency_uses_saved_timezone_and_code_owned_trigger(chat_db):
    from app.db.models import Job
    from app.db.task_models import Task
    text = "Every Friday at 8, summarize waitlist sign-ups and replies"
    source(chat_db, text)
    reply = call(chat_db, text)
    assert "Deadline estimate:" in reply["body_md"] and count(chat_db, Job) == 0
    with chat_db["factory"]() as session:
        task = session.scalar(select(Task))
        trigger = json.loads(task.trigger_json)
        assert task.kind == "scheduled" and trigger["tz"] == "Asia/Kolkata"
        assert "BYDAY=FR" in trigger["rrule"] and "BYHOUR=8" in trigger["rrule"]


@pytest.mark.parametrize("audience", ["repair shop managers", "weekend gardeners"])
def test_saved_task_replay_is_my_update_not_a_handoff(chat_db, audience):
    text = f"Every Friday at 8, summarize waitlist sign-ups and replies for {audience}"
    source(chat_db, text)
    call(chat_db, text)
    reply = call(chat_db, text)
    assert "I've already saved your task." in reply["body_md"]
    assert audience in reply["body_md"]
    assert "handoff" not in reply["body_md"].lower()


def test_unconfirmed_schedule_timezone_asks_once_before_model_or_task_write(chat_db):
    from app.db.models import Person
    from app.db.task_models import Task
    with chat_db["factory"]() as session:
        session.get(Person, "usr_chat").timezone = None
        session.commit()
    text = "Every Friday, summarize waitlist sign-ups and replies"
    source(chat_db, text)
    reply = call(chat_db, text)
    assert reply["body_md"].count("?") == 1 and "unconfirmed default" in reply["body_md"]
    assert chat_db["calls"] == [] and count(chat_db, Task) == 0


@pytest.mark.parametrize("audience", ["Freight dispatch teams", "Families planning nature walks"])
def test_consequential_plan_does_not_start_a_run_or_consequential_job(chat_db, audience):
    from app.db.models import Job, Venture
    from app.db.task_models import Task, TaskRun
    from app.core.local_mode import is_local
    with chat_db["factory"]() as session:
        session.get(Venture, "ven_chat").raw_idea = audience
        session.commit()
    chat_db["mode"]["skill"] = "pagebuilder.publish"
    text = "Do publish my website"
    source(chat_db, text)
    reply = call(chat_db, text)
    assert ("No work has started" if is_local() else "Nothing will be sent, posted, bought or deleted") in reply["body_md"]
    if is_local():
        from app.core.local_capabilities import blocked_message

        assert blocked_message("publish_pages") in reply["body_md"]
        assert "approval" not in reply["body_md"].lower()
    assert count(chat_db, TaskRun) == count(chat_db, Job) == 0
    with chat_db["factory"]() as session:
        task = session.scalar(select(Task))
        assert task.state == ("input_required" if is_local() else "approval_required")
        assert chat_db["chat"].service.action(task)["href"] == ("/v/ven_chat/tasks" if is_local() else "/v/ven_chat/site")


def test_status_and_stop_use_actual_owned_rows_without_model_or_duplicate_source_write(chat_db):
    from app.db.models import ChatMessage
    from app.db.task_models import Task
    from app.tasks import goals
    d = chat_db
    create = "Do summarize waitlist sign-ups and replies"
    source(d, create, id="msg_create")
    call(d, create, "msg_create")
    calls = len(d["calls"])
    status = "What are you working on?"
    source(d, status, id="msg_status")
    reply = call(d, status, "msg_status")
    assert "Saved summary" in reply["body_md"] and ("Deadline:" if goals.enabled() else "Deadline estimate:") in reply["body_md"]
    stop = "Stop task Saved summary"
    source(d, stop, id="msg_stop")
    assert "Stopped" in call(d, stop, "msg_stop")["body_md"]
    # The old stop's immutable receipt cannot cancel a later task with the same title.
    source(d, create, id="msg_new")
    call(d, create, "msg_new")
    assert "already saved" in call(d, stop, "msg_stop")["body_md"]
    assert len(d["calls"]) == calls + 1 and count(d, ChatMessage) == 4
    with d["factory"]() as session:
        assert sorted(session.scalars(select(Task.state)).all()) == ["canceled", "working"]


def test_ambiguous_exact_title_stops_nothing_and_asks_one_question(chat_db):
    from app.db.task_models import Task
    d = chat_db
    create = "Do summarize waitlist sign-ups and replies"
    for source_id in ("msg_one", "msg_two"):
        source(d, create, id=source_id)
        call(d, create, source_id)
    text = "Cancel task Saved summary"
    source(d, text, id="msg_cancel")
    reply = call(d, text, "msg_cancel")
    assert reply["kind"] == "needs_you" and reply["body_md"].count("?") == 1
    with d["factory"]() as session:
        assert set(session.scalars(select(Task.state)).all()) == {"working"}


@pytest.mark.parametrize("changes", [{"role": "assistant"}, {"author_person_id": "usr_other"}, {"conversation_id": None},
    {"workspace_id": "foreign"}, {"venture_id": "foreign"}, {"body_md": "Do a different task"}])
def test_forged_or_retired_source_fields_cannot_authorize_planning(chat_db, changes):
    from app.db.task_models import Task
    text = "Do summarize waitlist sign-ups and replies"
    source(chat_db, text, **changes)
    reply = call(chat_db, text)
    assert reply["kind"] == "needs_you" and "could not verify" in reply["body_md"]
    assert chat_db["calls"] == [] and count(chat_db, Task) == 0


def test_owner_change_after_planning_cannot_apply_or_start_saved_intent(chat_db):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    chat_db["mode"]["owner_change"] = True
    text = "Do summarize waitlist sign-ups and replies"
    source(chat_db, text)
    reply = call(chat_db, text)
    assert reply["kind"] == "needs_you" and count(chat_db, Task) == 1
    assert count(chat_db, TaskRun) == count(chat_db, Job) == 0
    with chat_db["factory"]() as session:
        journal = json.loads(session.scalar(select(Task)).scope_json)["_planning"]
        assert journal["state"] == "responded" and journal["billing"]["usage"]["task_invocation"] is None


@pytest.mark.parametrize("change", ["owner", "archive"])
def test_meter_exit_error_after_custody_loss_reconciles_saved_chat_response_before_refusing_apply(chat_db, change):
    from app.db.models import Job, Workspace
    from app.db.task_models import Task, TaskRun
    d, text = chat_db, "Do summarize waitlist sign-ups and replies"
    source(d, text)
    def retire_scope(task_id):
        with d["factory"]() as session:
            workspace = session.get(Workspace, "wsp_chat")
            if change == "owner":
                workspace.owner_id = "usr_other"
            else:
                workspace.archived_at = d["clock"][0]
            session.commit()
    d["mode"].update(during=retire_scope, value="after_exit_error")
    assert call(d, text)["kind"] == "needs_you"
    assert len(d["recovery"].calls) == 1  # Original funded cleanup preceded the fresh private-source guard.
    assert call(d, text)["kind"] == "needs_you" and len(d["calls"]) == 1
    assert count(d, TaskRun) == count(d, Job) == 0
    with d["factory"]() as session:
        task = session.scalar(select(Task))
        assert task.state == "working" and json.loads(task.scope_json)["_planning"]["state"] == "responded"


def test_secret_like_request_never_reaches_planner_transport(chat_db):
    text = "Do use " + "sk-" + "x" * 24
    source(chat_db, text)
    reply = call(chat_db, text)
    assert chat_db["calls"] == [] and "sk-" not in reply["body_md"]


@pytest.mark.parametrize("mode", ["timeout", "calling_crash"])
def test_unknown_or_crashed_planner_intent_never_redials_or_starts(chat_db, mode):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    d, text = chat_db, "Do summarize waitlist sign-ups and replies"
    source(d, text)
    d["mode"]["value"] = mode
    if mode == "calling_crash":
        with pytest.raises(KeyboardInterrupt):
            call(d, text)
    else:
        assert call(d, text)["kind"] == "needs_you"
    replay = call(d, text)
    assert replay["kind"] == "needs_you" and "not be repeated automatically" in replay["body_md"]
    assert count(d, Task) == 1 and count(d, TaskRun) == count(d, Job) == 0 and len(d["calls"]) == 1
    with d["factory"]() as session:
        task = session.scalar(select(Task))
        assert json.loads(task.scope_json)["_planning"]["state"] == ("calling" if mode == "calling_crash" else "uncertain")
        assert "Private provider body" not in task.scope_json + (task.blocker_json or "")


def test_committed_response_crash_recovers_same_task_and_one_initial_run(chat_db, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    d, text = chat_db, "Do summarize waitlist sign-ups and replies"
    source(d, text)
    original = d["chat"].intake.recover
    monkeypatch.setattr(d["chat"].intake, "recover", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        call(d, text)
    with d["factory"]() as session:
        task = session.scalar(select(Task))
        assert json.loads(task.scope_json)["_planning"]["state"] == "responded"
    assert count(d, TaskRun) == count(d, Job) == 0
    monkeypatch.setattr(d["chat"].intake, "recover", original)
    d["clock"][0] += timedelta(days=1)
    assert call(d, text)["actions"][0]["label"] == "Open Now"
    assert count(d, Task) == count(d, TaskRun) == count(d, Job) == 1 and len(d["calls"]) == 1
    assert "already saved" in call(d, text)["body_md"] and count(d, TaskRun) == 1


@pytest.mark.parametrize("state", ["paused", "canceled"])
def test_founder_stop_committed_during_planning_never_starts_work(chat_db, state):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    from app.tasks.locks import lock_task
    d, text = chat_db, "Do summarize waitlist sign-ups and replies"
    source(d, text)
    def stop(task_id):
        with d["factory"]() as session:
            task = lock_task(session, task_id)
            if state == "canceled":
                d["chat"].service.cancel(session, task)
            else:
                task.state = "paused"
                d["chat"].service.event(session, task, "paused", "Paused during planning")
            session.commit()
    d["mode"]["during"] = stop
    call(d, text)
    call(d, text)
    with d["factory"]() as session:
        task = session.scalar(select(Task))
        assert task.state == state and json.loads(task.scope_json)["_planning"]["state"] == "complete"
    assert count(d, TaskRun) == count(d, Job) == 0 and len(d["calls"]) == 1


@pytest.mark.parametrize("field", ["body_md", "conversation_id"])
def test_private_source_changed_after_planner_response_cannot_apply_or_replay(chat_db, field):
    from app.db.models import ChatMessage, Job
    from app.db.task_models import TaskRun
    d, text = chat_db, "Do summarize waitlist sign-ups and replies"
    source(d, text)
    def change_source(task_id):
        with d["factory"]() as session:
            setattr(session.get(ChatMessage, "msg_source"), field, "changed source")
            session.commit()
    d["mode"]["during"] = change_source
    assert call(d, text)["kind"] == "needs_you"
    assert call(d, text)["kind"] == "needs_you"
    assert count(d, TaskRun) == count(d, Job) == 0 and len(d["calls"]) == 1


def test_timezone_changed_during_planning_does_not_change_frozen_trigger_or_redial(chat_db):
    from app.db.models import Person
    from app.db.task_models import Task
    d, text = chat_db, "Every Friday at 8, summarize waitlist sign-ups and replies"
    source(d, text)
    def change_zone(task_id):
        with d["factory"]() as session:
            session.get(Person, "usr_chat").timezone = "America/New_York"
            session.commit()
    d["mode"]["during"] = change_zone
    call(d, text)
    call(d, text)
    with d["factory"]() as session:
        task = session.scalar(select(Task))
        assert json.loads(task.scope_json)["_planning"]["accepted_zone"] == json.loads(task.trigger_json)["tz"] == "Asia/Kolkata"
    assert len(d["calls"]) == 1


def test_old_non_intake_task_returns_owned_current_summary_without_paid_call(chat_db):
    from app.tasks.contracts import Contract
    from app.tenancy import bind
    d, text = chat_db, "Do summarize waitlist sign-ups and replies"
    source(d, text)
    with bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"), d["conversations"].bind_conversation("ven_chat", "cnv_private"), d["factory"]() as session:
        key = d["chat"]._source(session, "ven_chat", text, "msg_source")[3]
        d["chat"].service.create(session, workspace_id="wsp_chat", venture_id="ven_chat", owner_person_id="usr_chat",
            contract=Contract(title="Earlier summary", goal="Summarize waitlist sign-ups and replies", steps=[{"title": "Summarize", "skill": "tasks.waitlist_summary"}]), start=False, create_key=key)
        session.commit()
    reply = call(d, text)
    assert "Earlier summary" in reply["body_md"] and "already saved" in reply["body_md"] and d["calls"] == []
