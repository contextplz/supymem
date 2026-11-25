"""Named navigation is account-scoped and never starts a venture or a provider call."""
from sqlalchemy import select
import pytest

from tests.integration.test_entry_chat_api import client


@pytest.fixture(autouse=True)
def hosted_account_navigation(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def test_existing_venture_navigation_never_proposes_a_new_venture(client, monkeypatch):
    from app.db.models import ChatMessage, Person, Venture, Workspace
    from app.db.supymem_models import EntryChatProposal
    from app.entry import chat_start
    from app.entry import service
    from app.chat.conversations import create
    from app.intake import application
    from app.state import db

    def forbidden(*args, **kwargs):
        raise AssertionError("Navigation must not classify, spend credits, or check new-venture quota")

    monkeypatch.setattr(application, "interpret", forbidden)
    monkeypatch.setattr(chat_start, "_classify", forbidden)
    monkeypatch.setattr(chat_start, "_assert_can_start", forbidden)
    founders = [
        ("usr_freight", "Ledger+Harbor", "Reconcile invoices for freight dispatchers"),
        ("usr_garden", "The Garden Picnic", "Share family picnic recipes"),
    ]
    now = chat_start.utcnow()
    with db()() as session:
        for actor, name, idea in founders:
            session.add(Person(id=actor, display_name=actor, created_at=now))
            for suffix in ("current", "other"):
                workspace = f"wsp_{actor}_{suffix}"
                session.add(Workspace(id=workspace, kind="company", name=suffix, entry_mode="idea", owner_id=actor,
                                      created_at=now, updated_at=now))
                session.add(Venture(id=f"ven_{actor}_{suffix}", workspace_id=workspace,
                    name=name if suffix == "other" else f"{name} Studio", raw_idea=idea,
                    stage="idea", created_at=now, updated_at=now))
        session.commit()

    for actor, name, idea in founders:
        current, other = f"ven_{actor}_current", f"ven_{actor}_other"
        with service.venture_scope(current, actor_id=actor, workspace_id=f"wsp_{actor}_current"):
            conversation = create(current, idempotency_key="switch-test")["id"]
        for text in (f"switch to {name.lower()} and tell me where that one is at", f"open {name}",
                     f"go to {name}", f'show me "{name}"', f"how is {name} doing"):
            for target in (None, current):
                payload = {"text": text, "request_id": f"{text}:{target}"}
                if target:
                    payload["target_venture_id"] = target
                    payload["conversation_id"] = conversation
                response = client.post("/entry/chat/preview", headers={"X-Subject": actor}, json=payload)
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["status"] == "reply" and "proposal" not in result
                assert name in result["body_md"] and "\n" not in result["body_md"]
                assert result["actions"] == [{"label": f"Open {name}", "href": f"/v/{other}?chat=1", "style": "primary"}]
                assert chat_start.preview(text=text, actor_id=actor, target_venture_id=target) == result
                assert client.post("/entry/chat/preview", headers={"X-Subject": actor}, json=payload).json() == result
        for everyday in ("show me my leads", "open the landing page preview", "how is the search doing"):
            assert chat_start._venture_switch(everyday, actor) is None
        foreign_actor, foreign_name, _ = next(founder for founder in founders if founder[0] != actor)
        for missing in (foreign_name, f"{name}Copy", "Unlisted Workshop"):
            response = client.post("/entry/chat/preview", headers={"X-Subject": actor},
                                   json={"text": f"switch to {missing}", "target_venture_id": current})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "reply" and "couldn't find" in result["body_md"]
            assert {action["href"] for action in result["actions"]} == {f"/v/{current}?chat=1", f"/v/{other}?chat=1"}
            assert foreign_name not in str(result) and foreign_actor not in str(result)
        denied = client.post("/entry/chat/preview", headers={"X-Subject": actor}, json={
            "text": f"switch to {name}", "target_venture_id": f"ven_{foreign_actor}_current"})
        assert denied.status_code == 404
        denied_conversation = client.post("/entry/chat/preview", headers={"X-Subject": actor}, json={
            "text": f"open {name}", "target_venture_id": other, "conversation_id": conversation})
        assert denied_conversation.status_code == 404
        with db()() as session:
            saved = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation)).all()
            assert len(saved) == 10
            assert {message.role for message in saved} == {"user", "assistant"}
            assert all(message.author_person_id == actor for message in saved if message.role == "user")
            assert all(message.venture_id == current for message in saved)
    with db()() as session:
        assert session.scalars(select(EntryChatProposal)).all() == []
        ventures = session.scalars(select(Venture)).all()
        assert len(ventures) == 4
        assert {venture.raw_idea for venture in ventures} == {founder[2] for founder in founders}


def test_venture_chat_reuses_account_navigation_before_screen_routing(client, monkeypatch):
    import asyncio
    from app.chat import router
    from app.chat.other_venture import other_venture_reply
    from app.chat.context import ChatContext
    from app.db.models import Person, Venture, Workspace
    from app.entry import chat_start, service
    from app.state import db

    actor, workspace = "usr_navigation", "wsp_navigation"
    ventures = [("ven_dispatch", "Cargo Ledger", "Reconcile freight invoices"),
                ("ven_picnic", "Picnic Basket", "Share family recipes")]
    now = chat_start.utcnow()
    with db()() as session:
        session.add(Person(id=actor, display_name="Founder", created_at=now))
        session.add(Workspace(id=workspace, kind="company", name="Your ventures", entry_mode="idea",
                              owner_id=actor, created_at=now, updated_at=now))
        for venture_id, name, idea in ventures:
            session.add(Venture(id=venture_id, workspace_id=workspace, name=name, raw_idea=idea,
                                stage="idea", created_at=now, updated_at=now))
        session.commit()

    previews = []

    async def preview(venture_id, action, **kwargs):
        assert action == "site.preview"
        previews.append(venture_id)
        return {"kind": "navigation", "body_md": "Review your landing page.",
                "actions": [{"href": f"/v/{venture_id}/site"}], "citations": []}

    monkeypatch.setattr(router, "execute_action", preview)
    for current_id, current_name, _ in ventures:
        other_id, other_name, _ = next(venture for venture in ventures if venture[0] != current_id)
        context = ChatContext(venture={"id": current_id, "name": current_name, "workspace_id": workspace})
        with service.venture_scope(current_id, actor_id=actor, workspace_id=workspace):
            for prefix in ("switch to", "open", "go to"):
                text = f"{prefix} {other_name.lower()}"
                expected = chat_start._venture_switch(text, actor)
                reply = asyncio.run(router._dispatch_intent(current_id, context, "open_screen", text, None, actor))
                assert reply["kind"] == "navigation"
                assert reply["body_md"] == expected["body_md"]
                assert reply["actions"] == [{"label": f"Open {other_name}", "href": f"/v/{other_id}?chat=1", "style": "primary"}]
            for text in ("open my leads", "open the landing page"):
                assert chat_start._venture_switch(text, actor) is None
                assert other_venture_reply(current_id, text, actor) is None
            # Reading the other venture's status from this venture's scope must not trip the brief's scope guard.
            status = other_venture_reply(current_id, f"how is {other_name} doing", actor)
            assert status["kind"] == "navigation" and status["body_md"].startswith(f"{other_name}:")
            assert status["actions"][0]["href"] == f"/v/{other_id}"
            for text, href in (("open gtm", f"/gtm?venture={current_id}"),
                               ("open website", f"/v/{current_id}/site")):
                reply = asyncio.run(router._dispatch_intent(current_id, context, "open_screen", text, None, actor))
                assert reply["actions"][0]["href"] == href
    assert previews == [venture[0] for venture in ventures]
