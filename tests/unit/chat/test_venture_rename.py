import asyncio

import pytest

from tests.unit.chat.test_audit_chat_fixes import env, _chat, _saved_clarification


@pytest.mark.parametrize("idea,old_name,new_name,audience", [
    ("A dispatch scheduler for commercial refrigeration repair teams", "Atlas Dispatch", "Relay Desk", "repair dispatchers"),
    ("A walking journal for families exploring local nature trails", "Willow Trails", "Moss Journal", "families planning walks"),
])
def test_venture_rename_preserves_questions_and_manager_identity(env, monkeypatch, idea, old_name, new_name, audience):
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.api.ventures import RenameVentureBody, rename_venture
    from app.chat import llm_router
    from app.chat.actions import get_action
    from app.chat.execute import execute_action
    from app.chat.idea_bridge import pending_question
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import Venture, Workspace, WorkspaceMember
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.org.manager import manager_name
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    client, _, _ = env
    created = client.post("/ventures", json={"name": old_name, "raw_idea": idea})
    assert created.status_code == 201, created.text
    venture_id, workspace_id = created.json()["id"], created.json()["workspace_id"]

    async def forbidden_route(*args, **kwargs):
        raise AssertionError("An explicit rename must not use a model router")

    original_route = llm_router.route
    monkeypatch.setattr(llm_router, "route", forbidden_route)
    action = get_action("venture.rename")
    assert action.required_permission == "request_work" and not action.consequential
    assert classify_intent("rename it today") != "venture.rename"

    requests = [
        f"rename this venture to {new_name}", f"rename my company to {new_name}",
        f"call this venture {new_name}", f"change the venture name to {new_name}",
        f'Please rename this venture to "{new_name}".',
    ]
    for text in requests:
        assert client.patch(f"/ventures/{venture_id}", json={"name": old_name}).status_code == 200
        assert classify_intent(text) == "venture.rename"
        assert split_speech_acts(text) == [("venture.rename", text)]
        reply = _chat(client, venture_id, text)
        assert reply["kind"] == "venture_renamed", reply
        assert f"Renamed {old_name} to {new_name}." in reply["body_md"]
        assert reply["actions"] == []
        assert client.get(f"/ventures/{venture_id}").json()["name"] == new_name
        assert manager_name(venture_id) == "Charlie"
        with db()() as session:
            assert session.get(Workspace, workspace_id).name == new_name

    keyword_name = f"Brainstorm {audience} Users"
    reply = _chat(client, venture_id, f"rename this venture to {keyword_name}")
    assert reply["kind"] == "venture_renamed", reply
    assert f"Renamed {new_name} to {keyword_name}." in reply["body_md"]
    assert client.get(f"/ventures/{venture_id}").json()["name"] == keyword_name

    session_id, question_id, _ = _saved_clarification((client, venture_id, workspace_id), chat_entry=True,
        question=f"Who among {audience} would use this first?", field="first_user_path")
    answer = f"I would ask {audience} because they already look for this help every week"
    assert pending_question(venture_id, answer) == (session_id, question_id)
    long_name = "A Long Descriptive Name For Your New Project"
    for replacement in (old_name, long_name):
        text = f"rename this venture to {replacement}"
        assert pending_question(venture_id, text) is None
        assert pending_question(venture_id, text, force=True) is None
        assert pending_question(venture_id, text, revise=True) is None
        reply = _chat(client, venture_id, text)
        assert reply["kind"] == "venture_renamed", reply
        assert pending_question(venture_id, answer) == (session_id, question_id)
        with db()() as session:
            latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
                                    .order_by(ClarifierTurn.turn.desc()).limit(1))
            assert latest.id == question_id and latest.role == "agent"
            assert session.get(IdeaSession, session_id).state not in {"confirmed", "abandoned"}
            assert not session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id,
                ClarifierTurn.role == "founder", ClarifierTurn.text == text)).all()

    for invalid in ("", "Q", " " * 3, "Z" * 81, "Untitled", "ven_invalid", "Unfinished..."):
        rejected = client.patch(f"/ventures/{venture_id}", json={"name": invalid})
        assert rejected.status_code == 422, rejected.text
        reply = _chat(client, venture_id, f"rename this venture to {invalid}")
        assert reply["kind"] == "text" and "Your venture name was not changed." in reply["body_md"], reply
        assert client.get(f"/ventures/{venture_id}").json()["name"] == long_name
        assert pending_question(venture_id, answer) == (session_id, question_id)

    for site_edit in (f"rename the headline to {new_name}", f"rename it to {new_name}", f"rename the button to {new_name}"):
        assert classify_intent(site_edit) != "venture.rename"
    text = f"rename yourself to {new_name}"
    assert classify_intent(text) == "manager.rename"
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    monkeypatch.setattr(llm_router, "route", original_route)
    reply = _chat(client, venture_id, text)
    assert reply["kind"] != "confirmation"
    assert manager_name(venture_id) == new_name
    assert pending_question(venture_id, answer) == (session_id, question_id)

    with pytest.raises(HTTPException) as denied:
        rename_venture(venture_id, RenameVentureBody(name=new_name), "unrelated_account")
    assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        asyncio.run(execute_action(venture_id, "venture.rename", payload={"name": new_name}, actor_subject="unrelated_account"))
    assert denied.value.status_code == 404
    from app.authz.workspace import ensure_subject_person

    viewer = ensure_subject_person("read_only_account")
    with db()() as session:
        session.add(WorkspaceMember(id=new_id("wsm"), workspace_id=workspace_id, person_id=viewer, role="viewer", created_at=utcnow()))
        session.commit()
    with pytest.raises(PermissionError, match="request_work_required"):
        asyncio.run(execute_action(venture_id, "venture.rename", payload={"name": new_name}, actor_subject=viewer))
    with db()() as session:
        assert session.get(Venture, venture_id).name == long_name
