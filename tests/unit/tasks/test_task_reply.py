"""Pure routing candidates and server-only same-transaction output authority."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from tests.unit.tasks.test_chat import chat_db as chat_db, source


@pytest.mark.parametrize("text,intent", [("Do summarize my waitlist", "change"), ("Every Friday summarize my waitlist", "change"),
    ("What are you working on?", "status"), ("Stop task Saved summary", "cancel"), ("Do you know my audience?", None), ("hello", None)])
def test_pure_classification_uses_only_existing_router_intents(chat_db, text, intent):
    assert chat_db["chat"].classify(text) == intent and chat_db["calls"] == []


def _contexts(d):
    from contextlib import ExitStack
    from app.tenancy import bind
    contexts = ExitStack()
    contexts.enter_context(bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"))
    contexts.enter_context(d["conversations"].bind_conversation("ven_chat", "cnv_private"))
    return contexts


def test_server_reply_is_dict_compatible_but_json_cannot_carry_output_authority(chat_db):
    d = chat_db
    text = "What are you working on?"
    source(d, text)
    with _contexts(d):
        reply = asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id="msg_source"))
        assert isinstance(reply, d["chat"].TaskReply) and isinstance(reply, dict)
        assert not isinstance(dict(reply), d["chat"].TaskReply)
        assert "output_authority" not in reply and "assistant_key" not in reply
        assert reply.assistant_key.startswith("task-reply:")
        with d["factory"]() as session:
            reply.output_authority(session, d["conversations"].current_conversation())
    assert d["calls"] == []


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive", "blocked", "invalid_settings",
    "destination_visibility", "destination_creator", "source_text", "source_partition"])
def test_live_output_fence_refuses_changed_source_or_private_destination(chat_db, change):
    from app.db.models import ChatConversation, ChatMessage, Venture, Workspace
    d = chat_db
    text = "What are you working on?"
    source(d, text)
    with _contexts(d):
        reply = asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id="msg_source"))
        destination = d["conversations"].current_conversation()
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
            elif change == "destination_visibility":
                session.get(ChatConversation, destination.id).visibility = "legacy_shared"
            elif change == "destination_creator":
                session.get(ChatConversation, destination.id).creator_person_id = "usr_other"
            elif change == "source_text":
                session.get(ChatMessage, "msg_source").body_md = "A different request"
            else:
                session.get(ChatMessage, "msg_source").conversation_id = "cnv_other"
            session.commit()
        with d["factory"]() as session, pytest.raises(HTTPException) as denied:
            reply.output_authority(session, destination)
        assert denied.value.status_code == 404
    assert d["calls"] == []


def test_output_fence_denies_a_different_resolved_conversation_even_for_same_actor(chat_db):
    d = chat_db
    text = "What are you working on?"
    source(d, text)
    with _contexts(d):
        reply = asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id="msg_source"))
        destination = d["conversations"].current_conversation()
        other = SimpleNamespace(**{**vars(destination), "id": "cnv_different"})
        with d["factory"]() as session, pytest.raises(HTTPException):
            reply.output_authority(session, other)


def test_pause_during_planning_preserves_funded_plan_without_initial_work(chat_db):
    from app.db.models import Job, Workspace
    from app.db.task_models import Task, TaskRun
    d = chat_db
    text = "Do summarize my waitlist"
    source(d, text)
    def pause(_task_id):
        with d["factory"]() as session:
            session.get(Workspace, "wsp_chat").settings_json = json.dumps({"venture_pause": {"paused": True}})
            session.commit()
    d["mode"]["during"] = pause
    with _contexts(d):
        reply = asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id="msg_source"))
        assert isinstance(reply, d["chat"].TaskReply) and "paused" in reply["body_md"].lower()
        with d["factory"]() as session:
            reply.output_authority(session, d["conversations"].current_conversation())
            task = session.scalar(select(Task))
            assert json.loads(task.scope_json)["_planning"]["state"] == "responded"
            assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
            assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert len(d["calls"]) == 1


@pytest.mark.parametrize("idea", ["Track supplier invoices for factories", "Plan weekend walks for families"])
def test_status_remains_available_while_venture_paused(chat_db, idea):
    from app.db.models import Venture, Workspace
    d = chat_db
    text = "What are you working on?"
    source(d, text)
    with d["factory"]() as session:
        session.get(Venture, "ven_chat").raw_idea = idea
        session.get(Workspace, "wsp_chat").settings_json = json.dumps({"venture_pause": {"paused": True}})
        session.commit()
    with _contexts(d):
        reply = asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id="msg_source"))
        with d["factory"]() as session:
            reply.output_authority(session, d["conversations"].current_conversation())
        assert "no active" in reply["body_md"].lower()
        assert "Saved results remain available" in reply["body_md"]
    assert d["calls"] == []


def test_stop_remains_available_while_venture_paused(chat_db):
    from app.db.models import Workspace
    from app.db.task_models import Task
    from app.tasks.contracts import Contract
    d = chat_db
    text = "Stop task Saved summary"
    source(d, text)
    with d["factory"]() as session:
        task = d["chat"].service.create(session, workspace_id="wsp_chat", venture_id="ven_chat", owner_person_id="usr_chat",
            contract=Contract(title="Saved summary", goal="Summarize my waitlist", steps=[{"title": "Summary", "skill": "tasks.waitlist_summary"}]), start=False)
        task_id = task.id
        session.get(Workspace, "wsp_chat").settings_json = json.dumps({"venture_pause": {"paused": True}})
        session.commit()
    with _contexts(d):
        reply = asyncio.run(d["chat"].handle_message("ven_chat", text, source_message_id="msg_source"))
        with d["factory"]() as session:
            reply.output_authority(session, d["conversations"].current_conversation())
            assert session.get(Task, task_id).state == "canceled"
        assert "Stopped" in reply["body_md"]
    assert d["calls"] == []
