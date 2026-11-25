"""Actual chat/router/store custody; only planner transport and funding are synthetic."""
import asyncio
from contextlib import ExitStack
import importlib
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import pytest
from sqlalchemy import create_engine, event, func, inspect, select

from tests.unit.tasks.test_chat import chat_db as chat_db


@pytest.fixture
def natural(chat_db, tmp_path, monkeypatch):
    from app.chat import idea_bridge, llm_router, router, store
    from app.db.models import Base, WorkspaceMember
    from app.db.supymem_models import ClarifierTurn, FounderRequest
    from app.coding_agents_consent import CodingAgentConsent
    from app.chat.context import ChatContext
    from app.admin import authn
    from app.api import chat_api
    from app.analytics import events, chat_usage
    d = chat_db
    # Keep the existing fake's captured sessionmaker, with a real second-connection
    # database for mounted requests and authority changes during the await.
    old_engine = d["factory"].kw["bind"]
    names = set(inspect(old_engine).get_table_names())
    tables = [table for table in Base.metadata.sorted_tables if table.name in names]
    with old_engine.connect() as connection:
        saved = [(table, [dict(row) for row in connection.execute(select(table)).mappings()]) for table in tables]
    engine = create_engine(f"sqlite:///{tmp_path}/natural-chat.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[*tables, WorkspaceMember.__table__, FounderRequest.__table__, ClarifierTurn.__table__, CodingAgentConsent.__table__])
    with engine.begin() as connection:
        for table, rows in saved:
            if rows:
                connection.execute(table.insert(), rows)
    d["factory"].configure(bind=engine)
    for name in ("app.chat.router", "app.chat.store", "app.chat.requests", "app.api.chat_api",
                 "app.authz.workspace", "app.org.manager", "app.ventures.lifecycle"):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "db", lambda: d["factory"])
    monkeypatch.setattr(router, "assemble", lambda _vid: ChatContext(venture={
        "id": "ven_chat", "workspace_id": "wsp_chat", "name": "Synthetic"}))
    monkeypatch.setattr(router, "resolve_org", lambda _vid: {"agents": []})
    monkeypatch.setattr(idea_bridge, "pending_question", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(authn, "require_signed_caller", lambda: False)
    monkeypatch.setattr(authn, "verify_caller", lambda _request: SimpleNamespace(
        sub="usr_chat", role="user", legacy=False, device_id="synthetic-device"))
    monkeypatch.setattr(events, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_usage, "emit", lambda *_args, **_kwargs: None)
    route_calls = []
    async def ordinary_route(*args, **kwargs):
        route_calls.append((args, kwargs))
        return None
    monkeypatch.setattr(llm_router, "route", ordinary_route)
    monkeypatch.setattr(llm_router, "needs_model", lambda *_args: False)
    app = FastAPI()
    app.include_router(chat_api.router)
    d.update(app=app, router=router, store=store, route_calls=route_calls, engine=engine, monkeypatch=monkeypatch)
    yield d
    d["factory"].configure(bind=old_engine)
    engine.dispose()


async def _post(d, text, *, nonce="natural-fixture-1", private=True):
    import httpx
    body = {"message": text}
    if nonce is not None:
        body["idempotency_key"] = nonce
    if private:
        body["conversation_id"] = "cnv_private"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d["app"]), base_url="http://synthetic.local") as client:
        return await client.post("/ventures/ven_chat/chat", json=body, headers={"X-Subject": "usr_chat"})


@pytest.mark.parametrize("text,action", [("stop everything", "pause"), ("stop this venture", "pause"),
                                        ("cancel the build", "build.cancel")])
def test_private_nonce_stop_executes_the_exact_venture_or_build_once(natural, text, action):
    from app.chat import execute, stop_work
    from app.db.task_models import Task
    from app.tenancy import current

    d = natural
    d["monkeypatch"].setattr(stop_work, "active_build", lambda _vid: ("run_saved", "app_saved"))
    stopped = []
    async def stop(venture_id, name, *, payload=None, actor_subject=None, **kwargs):
        stopped.append((venture_id, name, payload, actor_subject, current().workspace_id))
        return {"kind": "text", "body_md": "Your requested work is stopping. Check its saved status.", "actions": []}
    d["monkeypatch"].setattr(execute, "execute_action", stop)
    response = asyncio.run(_post(d, text))
    assert response.status_code == 200
    assert not any(item.get("confirmation") for item in response.json()["actions"])
    payload = {"run_id": "run_saved"} if action == "build.cancel" else {"stop_everything": True} if text == "stop everything" else {}
    assert stopped == [("ven_chat", action, payload, "usr_chat", "wsp_chat")]
    replay = asyncio.run(_post(d, text))
    assert replay.json() == response.json() and len(stopped) == 1
    assert d["chat"].classify(text) is None and d["chat"].classify("cancel task Saved summary") == "cancel"
    assert _count(d, Task) == 0 and d["calls"] == []


def _direct(d, text, *, nonce="direct-fixture", private=True, actor="usr_chat"):
    from app.tenancy import bind
    with ExitStack() as contexts:
        contexts.enter_context(bind(workspace_id="wsp_chat", venture_id="ven_chat", actor=actor))
        contexts.enter_context(d["conversations"].bind_conversation("ven_chat", "cnv_private" if private else None))
        token = d["store"].CLIENT_REQUEST_ID.set(nonce)
        try:
            return asyncio.run(d["router"].handle_message("ven_chat", text, actor_subject=actor if actor != "anonymous" else None))
        finally:
            d["store"].CLIENT_REQUEST_ID.reset(token)


def _count(d, model, **where):
    with d["factory"]() as session:
        return session.scalar(select(func.count()).select_from(model).filter_by(**where))


def test_mounted_private_task_and_nonce_replay_keep_one_source_one_reply_one_call(natural):
    from app.db.models import ChatMessage, Job
    from app.db.supymem_models import FounderRequest
    from app.db.task_models import Task, TaskRun
    d = natural
    dispatch, dispatched = d["router"]._dispatch_intent, []
    async def capture(*args, **kwargs):
        dispatched.append(kwargs)
        return await dispatch(*args, **kwargs)
    d["monkeypatch"].setattr(d["router"], "_dispatch_intent", capture)
    text = "Do summarize waitlist sign-ups and replies"
    first = asyncio.run(_post(d, text))
    replay = asyncio.run(_post(d, text))
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() and first.json()["actions"]
    assert len(d["calls"]) == 1 and d["route_calls"] == []
    assert _count(d, Task) == _count(d, TaskRun) == _count(d, Job) == 1
    assert _count(d, ChatMessage, role="user") == _count(d, ChatMessage, role="assistant") == 1
    with d["factory"]() as session:
        source = session.scalar(select(ChatMessage).where(ChatMessage.role == "user"))
        task = session.scalar(select(Task))
        assert source.body_md == text and source.author_person_id == "usr_chat"
        assert json.loads(source.citations_json) == [{"client_request_id": "natural-fixture-1"}]
        assert task.goal == text.removeprefix("Do ") and task.create_key.startswith("task-chat:")
        receipt = session.scalar(select(FounderRequest))
        assert receipt.outcome == "completed" and json.loads(receipt.text)["response"] == first.json()
        assert dispatched == [{"actor_subject": "usr_chat", "source_message_id": source.id,
                               "request_id": f"{receipt.id}:act:0"}]


def test_router_replays_saved_assistant_before_regenerating_task_body_or_actions(natural):
    d = natural
    text = "Do summarize waitlist sign-ups and replies"
    first = _direct(d, text)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Saved assistant replay must not regenerate a task reply")
    d["monkeypatch"].setattr(d["chat"], "handle_message", forbidden)
    replay = _direct(d, text)
    assert replay == first and len(d["calls"]) == 1 and d["route_calls"] == []


def test_task_source_uses_exact_accepted_bytes_even_when_display_cleanup_changes_them(natural):
    from app.db.models import ChatMessage
    from app.db.task_models import Task
    text = "Do summarize waitlist sign-ups and replies at http://localhost:7777/proof — preserve punctuation."
    response = asyncio.run(_post(natural, text))
    assert response.status_code == 200
    with natural["factory"]() as session:
        source = session.scalar(select(ChatMessage).where(ChatMessage.role == "user"))
        task = session.scalar(select(Task))
        assert source.body_md == text and task.goal == text.removeprefix("Do ")
        displayed = natural["store"]._ser(source)
        assert displayed["body_md"] != text and "localhost" not in displayed["body_md"]
    assert natural["calls"][0][0]["goal"] == text.removeprefix("Do ")


def test_paused_new_task_refused_but_status_and_stop_reach_saved_tasks(natural):
    from app.db.models import Workspace
    from app.db.task_models import Task
    from app.tasks.contracts import Contract
    d = natural
    with d["factory"]() as session:
        task = d["chat"].service.create(session, workspace_id="wsp_chat", venture_id="ven_chat", owner_person_id="usr_chat",
            contract=Contract(title="Saved summary", goal="Summarize my waitlist", steps=[{"title": "Summary", "skill": "tasks.waitlist_summary"}]), start=False)
        task_id = task.id
        session.get(Workspace, "wsp_chat").settings_json = json.dumps({"venture_pause": {"paused": True}})
        session.commit()
    created = asyncio.run(_post(d, "Do summarize my waitlist", nonce="paused-create"))
    status = asyncio.run(_post(d, "What are you working on?", nonce="paused-status"))
    stopped = asyncio.run(_post(d, "Stop task Saved summary", nonce="paused-stop"))
    assert created.status_code == status.status_code == stopped.status_code == 200
    assert "paused" in created.json()["body_md"].lower()
    assert "Saved summary" in status.json()["body_md"] and "Stopped" in stopped.json()["body_md"]
    assert d["calls"] == d["route_calls"] == [] and _count(d, Task) == 1
    with d["factory"]() as session:
        assert session.get(Task, task_id).state == "canceled"


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive", "blocked", "invalid_settings",
                                    "destination_creator", "destination_visibility", "source_text"])
def test_mounted_scope_loss_during_planning_keeps_funded_history_without_run_or_output(natural, change):
    from app.db.models import ChatConversation, ChatMessage, Job, Venture, Workspace
    from app.db.supymem_models import FounderRequest
    from app.db.task_models import Task, TaskRun
    d = natural
    def mutate(_task_id):
        with d["factory"]() as session:
            workspace = session.get(Workspace, "wsp_chat")
            if change == "owner":
                workspace.owner_id = "usr_other"
            elif change == "workspace_archive":
                workspace.archived_at = d["clock"][0]
            elif change == "venture_archive":
                session.get(Venture, "ven_chat").archived_at = d["clock"][0]
            elif change in {"blocked", "invalid_settings"}:
                workspace.settings_json = '{"blocked":true}' if change == "blocked" else "invalid JSON"
            elif change == "destination_creator":
                session.get(ChatConversation, "cnv_private").creator_person_id = "usr_other"
            elif change == "destination_visibility":
                session.get(ChatConversation, "cnv_private").visibility = "legacy_shared"
            else:
                session.scalar(select(ChatMessage).where(ChatMessage.role == "user")).body_md = "A different source"
            session.commit()
    d["mode"]["during"] = mutate
    response = asyncio.run(_post(d, "Do summarize waitlist sign-ups and replies"))
    assert response.status_code == 409 and response.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(d["calls"]) == 1 and d["route_calls"] == []
    assert _count(d, Task) == 1 and _count(d, TaskRun) == _count(d, Job) == _count(d, ChatMessage, role="assistant") == 0
    with d["factory"]() as session:
        journal = json.loads(session.scalar(select(Task)).scope_json)["_planning"]
        assert journal["state"] == "responded" and journal["billing"]
        assert session.scalar(select(FounderRequest)).outcome == "unknown"


def test_handled_task_exception_cannot_enter_generic_recovery_or_another_model(natural):
    from app.db.models import ChatMessage
    from app.db.supymem_models import FounderRequest
    d = natural
    async def failed(*_args, **_kwargs):
        raise RuntimeError("Synthetic Task handler failed after an unknown response")
    d["monkeypatch"].setattr(d["chat"], "handle_message", failed)
    response = asyncio.run(_post(d, "Do summarize my waitlist"))
    assert response.status_code == 409 and _count(d, ChatMessage, role="assistant") == 0
    assert d["calls"] == d["route_calls"] == []
    with d["factory"]() as session:
        assert session.scalar(select(FounderRequest)).outcome == "unknown"


def test_unknown_plan_delivers_only_saved_status_and_nonce_replay_never_redials(natural):
    from app.db.models import ChatMessage, Job
    from app.db.task_models import Task, TaskRun
    d = natural
    d["mode"]["value"] = "timeout"
    text = "Do summarize waitlist sign-ups and replies"
    first = asyncio.run(_post(d, text))
    replay = asyncio.run(_post(d, text))
    status = asyncio.run(_post(d, "What are you working on?", nonce="unknown-status"))
    assert first.status_code == replay.status_code == status.status_code == 200
    assert first.json() == replay.json() and first.json()["kind"] == "needs_you"
    assert "has not been confirmed" in first.json()["body_md"]
    for reply in (first, status):
        assert "Status: Plan awaiting confirmation." in reply.json()["body_md"]
        assert "Deadline estimate: being checked." in reply.json()["body_md"]
        assert "Status: Working." not in reply.json()["body_md"]
    assert "Private provider body" not in first.json()["body_md"]
    assert len(d["calls"]) == 1 and d["route_calls"] == []
    assert _count(d, Task) == 1 and _count(d, TaskRun) == _count(d, Job) == 0
    assert _count(d, ChatMessage, role="user") == _count(d, ChatMessage, role="assistant") == 2
    with d["factory"]() as session:
        task = session.scalar(select(Task))
        journal = json.loads(task.scope_json)["_planning"]
        assert journal["state"] == "uncertain" and "contract" not in journal
        assert task.state == "input_required"


def test_inflight_plan_status_is_truthful_without_changing_its_working_journal(natural):
    from app.db.task_models import Task, TaskRun
    from app.tasks import intake
    d = natural
    with d["factory"]() as session:
        task, should_plan = intake.begin(session, workspace_id="wsp_chat", venture_id="ven_chat", owner_person_id="usr_chat",
            create_key="synthetic-inflight-plan", body_input={"goal": "Summarize my waitlist", "timezone": "Asia/Kolkata", "start": False}, accepted_zone="Asia/Kolkata")
        task_id, saved_scope = task.id, task.scope_json
        assert should_plan and task.state == "working"
        session.commit()
    response = asyncio.run(_post(d, "What are you working on?", nonce="inflight-status"))
    assert response.status_code == 200
    assert "Status: Plan awaiting confirmation." in response.json()["body_md"]
    assert "Deadline estimate: being checked." in response.json()["body_md"]
    assert "Status: Working." not in response.json()["body_md"]
    assert d["calls"] == [] and _count(d, TaskRun) == 0
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        assert task.state == "working" and task.scope_json == saved_scope


@pytest.mark.parametrize("domain", ["Parts inventory for mechanics", "Cooking lessons for families"])
def test_pause_during_await_is_rechecked_before_run_and_still_allows_fenced_status(natural, domain):
    from app.db.models import Workspace, Job
    from app.db.task_models import Task, TaskRun
    from app.ventures import idea_page, state_brief
    d = natural
    def pause(_task_id):
        with d["factory"]() as session:
            session.get(Workspace, "wsp_chat").name = domain
            session.get(Workspace, "wsp_chat").settings_json = json.dumps({"venture_pause": {"paused": True}})
            session.commit()
    d["mode"]["during"] = pause
    response = asyncio.run(_post(d, "Do summarize waitlist sign-ups and replies"))
    assert response.status_code == 200 and "paused" in response.json()["body_md"].lower()
    assert len(d["calls"]) == 1 and _count(d, TaskRun) == 0
    d["monkeypatch"].setattr(idea_page, "db", lambda: d["factory"])
    def forbidden_refresh(*_args, **_kwargs):
        pytest.fail("Paused work must not read state or call a provider")
    d["monkeypatch"].setattr(state_brief, "get_venture_state_brief", forbidden_refresh)
    asyncio.run(idea_page.refresh({"venture_id": "ven_chat", "_requested": True}))
    with d["factory"]() as session:
        assert not idea_page.schedule(session, "wsp_chat", "ven_chat", actor="usr_chat", requested=True)
        assert session.scalars(select(Job.kind)).all() == [], domain
    with d["factory"]() as session:
        journal = json.loads(session.scalar(select(Task)).scope_json)["_planning"]
        assert journal["state"] == "responded" and journal["billing"]


@pytest.mark.parametrize("private,nonce,actor", [(False, "legacy", "usr_chat"), (True, None, "usr_chat"), (False, "anonymous", "anonymous")])
def test_missing_private_actor_or_nonce_uses_ordinary_routing(natural, private, nonce, actor):
    from app.db.task_models import Task
    d = natural
    async def ordinary(*_args, **_kwargs):
        return {"body_md": "Ordinary route", "kind": "text", "actions": [], "citations": []}
    d["monkeypatch"].setattr(d["router"], "_dispatch_intent", ordinary)
    _direct(d, "Do summarize my waitlist", private=private, nonce=nonce, actor=actor)
    assert len(d["route_calls"]) == 1 and d["calls"] == [] and _count(d, Task) == 0


@pytest.mark.parametrize("text", ["Do summarize ven_foreign's waitlist", "Do send emails to 100 people", "Do delete all the files", "Do not summarize my waitlist"])
def test_existing_foreign_bulk_consequential_and_negation_guards_precede_task_admission(natural, text):
    from app.db.task_models import Task
    d = natural
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Existing refusal must precede Task admission")
    d["monkeypatch"].setattr(d["chat"], "handle_message", forbidden)
    # Negation stays with ordinary routing; its real deterministic guard still wins.
    response = asyncio.run(_post(d, text))
    assert response.status_code == 200 and _count(d, Task) == 0 and d["calls"] == []
    assert not any(action.get("action") == "mac.task.prepare" for action in response.json()["actions"])


@pytest.mark.parametrize("text, label", [
    ("Do review my existing website https://example.com", "Review existing website"),
    ("Do import contacts from my CSV", "Import contacts"),
])
def test_main_asset_review_precedes_task_admission(natural, text, label):
    from app.db.task_models import Task

    d = natural
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Existing asset review must precede Task admission")
    d["monkeypatch"].setattr(d["chat"], "handle_message", forbidden)
    response = asyncio.run(_post(d, text))
    assert response.status_code == 200
    assert response.json()["actions"][0]["label"] == label
    assert d["calls"] == [] and _count(d, Task) == 0


@pytest.mark.parametrize("gate", ["secret", "memory", "stale_question", "active_question", "product", "browser", "mac", "send_revocation"])
@pytest.mark.parametrize("idea", ["Fleet route scheduling for dispatch teams", "Family recipes for home cooks"])
def test_existing_high_priority_entry_paths_never_enter_task_handler(natural, gate, idea):
    from app.chat import idea_bridge, memory_commands, browser_prepare
    from app.core.local_mode import is_local
    from app.core import local_owner
    from app.db.models import Person, Venture, WorkspaceMember
    from app.db.task_models import Task
    from app.idea import service as idea_service
    from app.secrets import service as secrets
    from app.build import device_orders
    d = natural
    with d["factory"]() as session:
        session.get(Venture, "ven_chat").raw_idea = idea
        session.commit()
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Higher priority router path cannot enter Tasks")
    d["monkeypatch"].setattr(d["chat"], "handle_message", forbidden)
    extra = {}
    owner = None
    if gate == "secret":
        text = "Do summarize my waitlist with sk-proj-synthetic-not-a-real-key"
        d["monkeypatch"].setattr(secrets, "request_secret", lambda *_args, **_kwargs: {"form": {"label": "the test credential"}})
    elif gate == "memory":
        text = "Remember: every Friday summarize my waitlist."
        async def memory(*_args):
            return {"body_md": "Synthetic saved memory", "kind": "memory_list", "citations": []}
        d["monkeypatch"].setattr(memory_commands, "dispatch", memory)
    elif gate in {"stale_question", "active_question"}:
        text = "Every Friday summarize my waitlist"
        extra = {"reply_to_question": True, "question_id": "old-question" if gate == "stale_question" else "current-question"}
        d["monkeypatch"].setattr(idea_bridge, "pending_question", lambda *_args, **_kwargs: ("idea-synthetic", "current-question"))
        d["monkeypatch"].setattr(idea_service, "post_message", lambda *_args, **_kwargs: {"session": {"workspace_id": "wsp_chat"}})
    elif gate == "product":
        text = "Build a product using my Codex"
        if is_local():
            from desktop_runtime import agent_status

            with d["factory"]() as session:
                owner = session.get(Person, "usr_chat")
                session.add(WorkspaceMember(id="natural-build-owner", workspace_id="wsp_chat", person_id=owner.id,
                    role="owner", created_at=d["clock"][0]))
                session.commit()
            d["monkeypatch"].setattr(agent_status, "statuses", list)
        else:
            d["monkeypatch"].setattr(device_orders, "coding_loop_enabled", lambda *_args: False)
    elif gate == "browser":
        text = "Open https://example.com in Chrome"
        d["monkeypatch"].setenv(browser_prepare.FLAG, "1")
        d["monkeypatch"].setenv(browser_prepare.PIN, "a" * 64)
        d["monkeypatch"].setattr(browser_prepare, "prepare", lambda *_args: "Synthetic reviewed browser path")
    elif gate == "mac":
        text = "Use my Mac to inspect this application"
    else:
        text = "Stop sending emails"
        async def revoke(_vid, action, **_kwargs):
            assert action == "autopilot.send_off"
            return {"body_md": "Sending stopped", "kind": "text", "actions": [], "citations": []}
        d["monkeypatch"].setattr(d["router"], "execute_action", revoke)
    from app.tenancy import bind
    with ExitStack() as contexts:
        contexts.enter_context(bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"))
        contexts.enter_context(d["conversations"].bind_conversation("ven_chat", "cnv_private"))
        contexts.callback(d["store"].CLIENT_REQUEST_ID.reset, d["store"].CLIENT_REQUEST_ID.set("priority-fixture"))
        if owner is not None:
            contexts.callback(local_owner._OWNER.reset, local_owner._OWNER.set(owner))
        reply = asyncio.run(d["router"].handle_message("ven_chat", text, actor_subject="usr_chat", **extra))
    assert reply["body_md"] and d["calls"] == [] and _count(d, Task) == 0
    if owner is not None:
        assert not reply.get("action_started") and reply["actions"][0]["href"] == "/settings/local#coding-tools"


def test_writer_fence_runs_after_serialization_before_lookup_and_insert(natural):
    from app.tenancy import bind
    from app.db.models import ChatMessage
    d = natural
    statements, calls = [], []
    def statement(_connection, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)
    event.listen(d["engine"], "before_cursor_execute", statement)
    def authority(session, scope):
        assert session.in_transaction() and statements[-1] == "BEGIN IMMEDIATE"
        assert scope.id == "cnv_private"
        calls.append(id(session))
    try:
        with bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"), d["conversations"].bind_conversation("ven_chat", "cnv_private"):
            first = d["store"].append_message("ven_chat", role="assistant", body_md="Saved reply", idempotency_key="fenced", output_authority=authority)
            assert len(calls) == 2 and calls[0] == calls[1]
            replay = d["store"].idempotent_message("ven_chat", "assistant", "fenced", output_authority=authority)
            assert replay == first
            def denied(*_args):
                raise HTTPException(404, "synthetic_authority_denied")
            with pytest.raises(HTTPException):
                d["store"].append_message("ven_chat", role="assistant", body_md="Saved reply", idempotency_key="fenced", output_authority=denied)
        assert len(calls) == 3 and _count(d, ChatMessage, role="assistant") == 1
    finally:
        event.remove(d["engine"], "before_cursor_execute", statement)


@pytest.mark.parametrize("change", ["source_text", "source_role", "source_delete", "destination_creator", "destination_visibility", "destination_delete"])
def test_source_proof_rechecks_database_after_preloaded_row_changes_without_clobbering_identity(natural, change):
    from app.db.models import ChatConversation, ChatMessage
    from app.tenancy import bind
    d = natural
    text = "What are you working on?"
    with bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"), d["conversations"].bind_conversation("ven_chat", "cnv_private"):
        saved = d["store"].append_message("ven_chat", role="user", body_md=text, preserve_exact=True)
        with d["factory"]() as reader:
            cached_source = reader.get(ChatMessage, saved["id"])
            cached_destination = reader.get(ChatConversation, "cnv_private")
            assert d["chat"]._source(reader, "ven_chat", text, saved["id"])[:2] == ("wsp_chat", "usr_chat")
            with d["factory"]() as writer:
                source = writer.get(ChatMessage, saved["id"])
                destination = writer.get(ChatConversation, "cnv_private")
                if change == "source_text":
                    source.body_md = "A replacement instruction"
                elif change == "source_role":
                    source.role = "assistant"
                elif change == "source_delete":
                    writer.delete(source)
                elif change == "destination_creator":
                    destination.creator_person_id = "usr_other"
                elif change == "destination_visibility":
                    destination.visibility = "legacy_shared"
                else:
                    writer.delete(destination)
                writer.commit()
            with pytest.raises(PermissionError, match="task_chat_source_invalid"):
                d["chat"]._source(reader, "ven_chat", text, saved["id"])
            assert cached_source.body_md == text and cached_source.role == "user"
            assert cached_destination.creator_person_id == "usr_chat" and cached_destination.visibility == "private"
    assert d["calls"] == []


@pytest.mark.parametrize("idea", ["Fleet maintenance for depot teams", "Watering reminders for home gardeners"])
def test_main_chat_routes_never_return_a_hidden_page_link(natural, idea):
    """Chat-first regression: replies from POST /chat, /chat/confirm and GET /chat carry no href to a hidden page."""
    import httpx
    from app.db.models import ChatMessage, Venture
    from core.clock import utcnow
    from tests.unit.chat.hidden_pages import hidden_hrefs

    d = natural
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("asset and help replies precede Task admission")
    d["monkeypatch"].setattr(d["chat"], "handle_message", forbidden)
    replies = []
    for index, text in enumerate(["Do import contacts from my CSV", "Do review my existing website https://example.com",
                                  "How do I pause this venture?"]):
        response = asyncio.run(_post(d, text, nonce=f"hidden-scan-{index}"))
        assert response.status_code == 200, (text, response.text)
        replies.append(response.json())
    with d["factory"]() as session:  # a reply saved before the chat-first sweep
        session.get(Venture, "ven_chat").raw_idea = idea
        session.add(ChatMessage(id="msg_legacy", venture_id="ven_chat", workspace_id="wsp_chat", role="assistant", kind="navigation",
                                body_md="Legacy", created_at=utcnow(),
                                actions_json=json.dumps([{"label": "Open settings", "href": "/v/ven_chat/settings"}])))
        session.commit()

    async def history():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d["app"]), base_url="http://synthetic.local") as client:
            return await client.get("/ventures/ven_chat/chat", headers={"X-Subject": "usr_chat"})
    items = asyncio.run(history()).json()["items"]
    assert [r["actions"][0]["label"] for r in replies[:2]] == ["Import contacts", "Review existing website"]
    assert replies[2]["actions"][0]["confirmation"]["action"] == "pause"
    assert [(r.get("actions") or [{}])[0].get("href") for r in replies + items if hidden_hrefs(r)] == []
    legacy = next(item for item in items if item["id"] == "msg_legacy")
    assert legacy["actions"] == [{"label": "Open settings", "href": "/v/ven_chat/settings", "kind": "recovery_link"}]
