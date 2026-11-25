"""Chat can preview, rewrite, waitlist, test and publish the landing page; public/paid steps need a confirm."""

from __future__ import annotations

import json

import pytest
from tests.company_os_harness import company_os_test_client, load_company_os_app, local_chat as local_chat
from tests.unit.test_autonomous_experiments import confirm_saved_idea


@pytest.mark.parametrize("idea", ["Equipment leasing for laboratories", "Local swim lessons for children"])
@pytest.mark.parametrize("state", ["ready", "running", "failed"])
@pytest.mark.asyncio
async def test_local_page_chat_links_to_checked_preview_export_latest_order_and_retry(env, monkeypatch, idea, state):
    from app.chat import site_actions
    from app.pagebuilder import api

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(site_actions, "_venture", lambda _: ("workspace", {"idea_summary": idea}, idea))
    monkeypatch.setattr(site_actions, "_saved_page_builds", lambda *_: ["latest", "saved"])
    monkeypatch.setattr(site_actions, "_live_site", lambda *_: None)
    monkeypatch.setattr("app.chat.execute.local_capability_reply", lambda _name: None)
    views = {"latest": {"job_id": "latest", "state": state, "stage": "building", "progress": .5,
                        "watch_url": "/v/venture/device-work-orders/order", "order_id": "order",
                        "error_reason": f"Your signup button for {idea} is missing."},
             "saved": {"job_id": "saved", "state": "ready", "stage": "ready", "progress": 1}}
    monkeypatch.setattr(api, "_view", lambda row: views[row])
    card = await site_actions._preview("venture", {}, "owner")
    selected = "latest" if state == "ready" else "saved"
    assert any(action.get("href") == "/v/venture/device-work-orders/order" for action in card["actions"])
    assert any(action.get("href") == f"/api/ventures/venture/pagebuilder/jobs/{selected}/preview" for action in card["actions"])
    assert any(action.get("href") == f"/api/ventures/venture/pagebuilder/jobs/{selected}/source" for action in card["actions"])
    assert not any(action.get("href", "").endswith(("/site", "/builds")) for action in card["actions"])
    if state == "failed":
        assert any(action.get("label") == "Retry" and action.get("action") == "pagebuilder.build" for action in card["actions"])
        assert next(action["payload"] for action in card["actions"] if action.get("label") == "Retry") == {"retry_job_id": "latest"}
        assert views["latest"]["error_reason"] in card["body_md"]
        legacy = {"kind": "site_preview", "actions": [{"label": "Retry", "action": "pagebuilder.build", "payload": {"prepare": True}}]}
        monkeypatch.setattr(site_actions, "ready_page_actions", lambda _: [])
        assert site_actions.refresh_page_actions([legacy], "venture")[0]["actions"] == [
            {"label": "Check your page status", "action": "site.preview"}]


@pytest.mark.parametrize("idea", ["Equipment leasing for laboratories", "Local swim lessons for children"])
def test_local_page_recovery_stays_in_chat(env, monkeypatch, idea):
    from app.chat import site_actions

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    card = site_actions._reply(f"Open Website to review your saved page for {idea}.",
                              [{"label": "Open Website", "href": "/v/venture/site"}])
    assert card["actions"][0]["action"] == "site.preview"
    assert "href" not in card["actions"][0]
    assert "Review your saved page below" in card["body_md"]
    assert "Website" not in card["body_md"]


@pytest.fixture(autouse=True)
def _beta_gate_on(monkeypatch):
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")

@pytest.fixture()
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/site.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "fallback")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with company_os_test_client(main.create_app()) as client:
        made = client.post("/ventures", json={"raw_idea": getattr(request, "param", "a lobby lock waitlist for apartment buildings")})
        assert made.status_code == 201, made.text
        yield client, made.json()["id"], made.json()["workspace_id"]


def _chat(client, vid, text):
    r = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def _cards(msg):
    return [a for a in msg.get("actions") or [] if a.get("confirmation")]


@pytest.mark.parametrize("idea", ["Equipment leasing for laboratories", "Local swim lessons for children"])
def test_saved_page_card_refreshes_from_current_venture_after_restart(env, monkeypatch, idea):
    from fastapi import BackgroundTasks
    from app.api import chat_api
    from app.chat.store import append_message
    from app.db.models import Job
    from app.pagebuilder import api
    from app import state
    from app.tenancy import bind
    from core.clock import utcnow

    _, venture_id, workspace_id = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor="founder"):
        older = append_message(venture_id, role="assistant", kind="site_preview", body_md=f"Your private page for {idea} is ready.")
        latest = append_message(venture_id, role="assistant", kind="site_preview", body_md="Your landing page preview is ready.",
                                actions=[{"label": "Keys and connections", "href": "/settings/local", "kind": "recovery_link"},
                                         {"label": "Preview", "href": f"/api/ventures/{venture_id}/pagebuilder/jobs/old/preview"}])
    with state.db()() as session:
        for job_id in ("ready", "running"):
            session.add(Job(id=job_id, kind=api.KIND, venture_id=venture_id, workspace_id=workspace_id,
                            status="running" if job_id == "running" else "succeeded", created_at=utcnow(),
                            payload_json=json.dumps({"_runtime": {"state": job_id}})))
        session.commit()
    monkeypatch.setattr(api, "_candidate_current", lambda _: True)
    monkeypatch.setattr(api, "_view", lambda _: pytest.fail("Chat should not load build progress to refresh preview links"))
    with monkeypatch.context() as restart:
        restart.setenv("SUPYMEM_MODE", "hosted")
        state.reset_db_factory()
        state.db()
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor="founder"):
        response = chat_api.get_chat(venture_id, BackgroundTasks())
    cards = {card["id"]: card for card in response["items"]}
    prefix = f"/api/ventures/{venture_id}/pagebuilder/jobs/ready"
    assert [(action["label"], action["href"]) for action in cards[latest["id"]]["actions"] if action.get("href", "").startswith(prefix)] == [
        ("Open preview", prefix + "/preview"), ("Export", prefix + "/source")]
    assert any(action.get("href") == "/settings/local" for action in cards[latest["id"]]["actions"])
    assert not cards[older["id"]]["actions"]


def test_conversation_title_comes_only_from_the_first_founder_message(env):
    from app.chat.conversations import create, update
    from app.chat.store import append_message
    from app.db.models import ChatConversation
    from app.state import db
    from app.tenancy import bind

    _, vid, wid = env
    cases = [
        ("  Help me plan\n the first launch! More details", "Help me plan the first launch"),
        ("## **Build** _a_ `simple` landing page?!", "Build a simple landing page"),
        ("Let's plan a founder-led launch...", "Let's plan a founder-led launch"),
        ("你好 世界！ café launch…", "你好 世界 café launch"),
        ("a" * 59 + "-longer", "a" * 59),
        ("x" * 80, "x" * 60),
        ("   !!!   ", "New chat"),
    ]
    with bind(workspace_id=wid, venture_id=vid, actor="founder"):
        for index, (body, expected) in enumerate(cases):
            cid = create(vid, idempotency_key=f"title-case-{index}")["id"]
            append_message(vid, conversation_id=cid, role="system", body_md="Welcome")
            original = append_message(vid, conversation_id=cid, role="founder", body_md=body,
                                      preserve_exact=True, idempotency_key="first-title-message")
            assert append_message(vid, conversation_id=cid, role="founder", body_md=body,
                                  preserve_exact=True, idempotency_key="first-title-message")["id"] == original["id"]
            append_message(vid, conversation_id=cid, role="user", body_md="Do not replace the first title")
            with db()() as session:
                assert session.get(ChatConversation, cid).title == expected
            replay = create(vid, idempotency_key=f"title-case-{index}")
            assert (replay["id"], replay["title"]) == (cid, expected)
        cid = create(vid, idempotency_key="title-custom", title="My own title")["id"]
        append_message(vid, conversation_id=cid, role="user", body_md="Not the custom title")
        with db()() as session:
            assert session.get(ChatConversation, cid).title == "My own title"
        update(vid, cid, {"title": "Renamed chat"})
        replay = create(vid, idempotency_key="title-custom", title="My own title")
        assert (replay["id"], replay["title"]) == (cid, "Renamed chat")
        assert create(vid, idempotency_key="title-custom", title="Different requested title") == replay
        update(vid, cid, {"title": "New chat"})
        append_message(vid, conversation_id=cid, role="user", body_md="Not a first message anymore")
        with db()() as session:
            assert session.get(ChatConversation, cid).title == "New chat"


def test_conversation_history_filters_current_archive_and_pin_order(env):
    from datetime import datetime, timedelta
    from sqlalchemy import delete
    from app.chat.conversations import create, list_conversations, update
    from app.chat.store import append_message
    from app.db.models import ChatConversation, ChatMessage
    from app.state import db
    from app.tenancy import bind

    client, vid, wid = env
    path = f"/ventures/{vid}/chat/conversations"
    headers = {"X-Subject": "founder"}
    with db()() as session:
        session.execute(delete(ChatMessage).where(ChatMessage.venture_id == vid))
        session.commit()
    with bind(workspace_id=wid, venture_id=vid, actor="founder"):
        assert list_conversations(vid)["items"] == []
        empty, pinned, recent, archived = [create(vid, idempotency_key=f"history-{title}", title=title)["id"]
                                          for title in ("Empty", "Pinned", "Recent", "Archived")]
        for cid in (pinned, recent, archived):
            append_message(vid, conversation_id=cid, role="system", body_md="Saved history")
        update(vid, pinned, {"pinned": True, "folder": "Launch"})
        update(vid, archived, {"archived": True, "pinned": True})
        legacy = list_conversations(vid)["legacy_conversation_id"]
        message = append_message(vid, role="user", body_md="Earlier null-partition history")
    with bind(workspace_id=wid, venture_id=vid, actor="usr_other_history"):
        foreign = create(vid, idempotency_key="history-foreign")["id"]
    with db()() as session:
        now = datetime(2026, 10, 23, 12)
        for index, cid in enumerate((legacy, pinned, recent)):
            session.get(ChatConversation, cid).updated_at = now + timedelta(minutes=index)
        session.get(ChatMessage, message["id"]).conversation_id = None
        session.commit()
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [pinned, recent, legacy]
    assert items[0]["pinned"] is True and items[0]["archived"] is False and items[0]["folder"] == "Launch"
    assert {"id", "venture_id", "workspace_id", "visibility", "title", "created_at", "updated_at",
            "pinned", "archived", "folder"} == set(items[0])
    assert empty in {item["id"] for item in client.get(path, headers=headers, params={"current": empty}).json()["items"]}
    assert foreign not in str(client.get(path, headers=headers, params={"current": foreign}).json())
    assert [item["id"] for item in client.get(path, headers=headers, params={"archived": "1", "current": empty}).json()["items"]] == [archived]
    with bind(workspace_id=wid, venture_id=vid, actor="founder"):
        update(vid, empty, {"archived": True})
    archived_items = client.get(path, headers=headers, params={"archived": "1", "current": empty}).json()["items"]
    assert [item["id"] for item in archived_items] == [archived, empty]
    assert all(item["archived"] for item in archived_items)
    with db()() as session:
        owner = session.get(ChatConversation, pinned).creator_person_id
        session.get(ChatConversation, empty).archived_at = None
        session.get(ChatConversation, empty).updated_at = now - timedelta(days=1)
        for index in range(101):
            cid = f"cnv_history_limit_{index}"
            session.add(ChatConversation(id=cid, venture_id=vid, workspace_id=wid, creator_person_id=owner,
                visibility="private", title="Older history", creation_key=cid, created_at=now, updated_at=now))
            session.add(ChatMessage(id=f"msg_history_limit_{index}", venture_id=vid, workspace_id=wid,
                conversation_id=cid, role="user", kind="text", body_md="Saved history", created_at=now))
        session.commit()
    limited = client.get(path, headers=headers, params={"current": empty}).json()["items"]
    assert len(limited) == 101 and limited[0]["id"] == pinned and limited[-1]["id"] == empty


@pytest.mark.parametrize("env,title", [
    ("a stock counter for repair shops", "Workshop launch"),
    ("a planting planner for home gardeners", "Garden meetup"),
], indirect=["env"])
def test_conversation_patch_validates_fields(env, title):
    from app.db.models import ChatConversation
    from app.state import db

    client, vid, wid = env
    path = f"/ventures/{vid}/chat/conversations"
    headers = {"X-Subject": "founder"}
    response = client.post(path, headers=headers, json={"idempotency_key": "history-patch"})
    assert response.status_code == 200, response.text
    cid = response.json()["id"]
    target = path + "/" + cid
    patched = client.patch(target, headers=headers, json={"title": f"  {title}  ", "pinned": True,
                                                         "archived": True, "folder": "x" * 60})
    assert patched.status_code == 200, patched.text
    item = patched.json()
    assert (item["title"], item["pinned"], item["archived"], item["folder"]) == (title, True, True, "x" * 60)
    replay = client.post(path, headers=headers, json={"idempotency_key": "history-patch"})
    assert replay.status_code == 200 and replay.json() == item
    assert item["updated_at"] >= response.json()["updated_at"]
    with db()() as session:
        pinned_at = session.get(ChatConversation, cid).pinned_at
    cleared = client.patch(target, headers=headers, json={"folder": "", "pinned": True}).json()
    assert cleared["folder"] is None and cleared["archived"] is True and cleared["title"] == title
    with db()() as session:
        assert session.get(ChatConversation, cid).pinned_at == pinned_at
    reset = client.patch(target, headers=headers, json={"pinned": False, "archived": False}).json()
    assert reset["pinned"] is False and reset["archived"] is False
    for invalid in ({"folder": "x" * 61}, {"title": " "}, {"title": "x" * 121}, {"title": None},
                    {"pinned": "true"}, {"pinned": 1}, {"pinned": None},
                    {"archived": "false"}, {"archived": None}, {"extra": True}):
        assert client.patch(target, headers=headers, json=invalid).status_code == 422
    assert client.patch(path + "/cnv_missing", headers=headers, json={"pinned": True}).status_code == 404


@pytest.mark.hosted_only
def test_conversation_patch_preserves_creator_and_legacy_access(env):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from app.state import db
    from core.clock import utcnow

    client, vid, wid = env
    path = f"/ventures/{vid}/chat/conversations"
    headers = {"X-Subject": "founder"}
    response = client.post(path, headers=headers, json={"idempotency_key": "history-patch"})
    assert response.status_code == 200, response.text
    target = path + "/" + response.json()["id"]
    member = ensure_subject_person("usr_history_member")
    with db()() as session:
        session.add(WorkspaceMember(id="wmb_history", workspace_id=wid, person_id=member, role="member", created_at=utcnow()))
        session.commit()
    member_headers = {"X-Subject": member}
    assert client.patch(target, headers=member_headers, json={"title": "Not mine"}).status_code == 404
    assert client.patch(target, headers={"X-Subject": "usr_foreign_workspace"}, json={"pinned": True}).status_code == 404
    legacy = client.get(path, headers=headers).json()["legacy_conversation_id"]
    shared = client.patch(path + "/" + legacy, headers=member_headers, json={"folder": "Shared"})
    assert shared.status_code == 200 and shared.json()["folder"] == "Shared"


@pytest.mark.parametrize("idea", ["a stock counter for repair shops", "a trail planner for hiking families"])
def test_shared_and_private_worker_replies_bind_without_rewriting_legacy_null(env, monkeypatch, idea):
    from sqlalchemy import select
    from app.chat.conversations import bind_conversation, capture_job_conversation, create, resolve
    from app.chat.milestones import post_milestone
    from app.chat.store import append_message, idempotent_message, list_messages
    from app.core.jobs import FatalError
    from app.db.models import ChatConversation, ChatMessage, Job, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.runner import _reply_conversation_id, post_fallback_question
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow

    _, vid, wid = env
    actor = "usr_binding_regression"
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    with bind(workspace_id=wid, venture_id=vid, actor=actor):
        private = create(vid, idempotency_key="binding-private-0001")["id"]
        # A shared writer creates its legacy destination in the caller's transaction.
        with db()() as session:
            session.get(Venture, vid).raw_idea = idea
            assert post_milestone(vid, key="binding-shared", kind="plan_ready", body_md="Shared ready", session=session)
            session.commit()
        legacy = resolve(vid).id
        assert legacy != private
        with bind_conversation(vid, private):
            assert post_milestone(vid, key="binding-private", kind="plan_ready", body_md="Private ready")
            capture = capture_job_conversation(vid, wid)
        shared_payload = {"session_id": "ids_binding_shared", "workspace_id": wid, "venture_id": vid}
        private_payload = {"session_id": "ids_binding_private", "workspace_id": wid, "venture_id": vid,
                           "_chat_conversation": capture, "_actor_subject": actor}
        with db()() as session:
            now = utcnow()
            for payload in (shared_payload, private_payload):
                session.add(IdeaSession(id=payload["session_id"], workspace_id=wid, venture_id=vid,
                    case="know", state="clarifying", created_at=now, updated_at=now))
            session.add(Job(id="job_binding_private", kind="idea.clarify_turn", workspace_id=wid,
                venture_id=vid, payload_json=json.dumps(private_payload), status="leased", attempts=1,
                locked_by="binding-worker", created_at=now))
            session.commit()
        assert post_fallback_question(shared_payload)
        with bind_conversation(vid, private):
            private_payload.update(_job_id="job_binding_private", _worker_id="binding-worker", _job_attempt=1)
            assert post_fallback_question(private_payload)
            with db()() as session:
                bad = {**private_payload, "_chat_conversation": {**capture, "actor_id": "usr_foreign"}}
                with pytest.raises(FatalError, match="clarification_scope_mismatch"):
                    _reply_conversation_id(session, bad)
        with db()() as session:
            rows = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid)))
            replies = [row for row in rows if row.kind == "idea_clarification_ready"]
            assert {row.conversation_id for row in replies} == {legacy, private} and len(replies) == 2
            assert all(row.conversation_id is not None for row in rows)
            null_actions_id = replies[0].id
            replies[0].actions_json = "null"
            session.commit()
        shared = list_messages(vid)
        own = list_messages(vid, conversation_id=private)
        assert any("Shared ready" in row["body_md"] for row in shared)
        assert not any("Private ready" in row["body_md"] for row in shared)
        assert any("Private ready" in row["body_md"] for row in own)
        for row in shared + own:
            if row["kind"] != "idea_clarification_ready":
                continue
            if row["id"] == null_actions_id:
                assert row["actions"] == []
            else:
                assert len(row["actions"]) == 1
                action = row["actions"][0]
                assert action["action"] == "chat.reply" and action["text"] == "Skip the questions"
                assert action["payload"]["text"] == action["text"] and not action.get("confirmation")
                with db()() as session:
                    question = session.get(ClarifierTurn, action["payload"]["question_id"])
                    assert question and question.venture_id == vid and question.workspace_id == wid
        historical = append_message(vid, role="user", body_md="Historical shared note", idempotency_key="historical-binding")
        with db()() as session:
            session.get(ChatMessage, historical["id"]).conversation_id = None
            session.commit()
        assert idempotent_message(vid, "user", "historical-binding")["conversation_id"] == legacy
        assert append_message(vid, role="user", body_md="Historical shared note", idempotency_key="historical-binding")["id"] == historical["id"]
        with db()() as session:
            assert session.get(ChatMessage, historical["id"]).conversation_id is None
            assert session.get(ChatMessage, null_actions_id).actions_json == "null"
            session.get(ChatConversation, legacy).creator_person_id = actor
            session.commit()
        assert not post_milestone(vid, key="binding-corrupt", kind="plan_ready", body_md="Must not post")
        with db()() as session:
            with pytest.raises(ValueError, match="chat_conversation_not_found"):
                _reply_conversation_id(session, shared_payload)
            with pytest.raises(FatalError, match="clarification_scope_mismatch"):
                _reply_conversation_id(session, {**shared_payload, "workspace_id": "wsp_foreign"})


async def _site(wid, vid):
    from app.firstlook.service import generate_first_look
    from tests.firstlook_legacy import seed_legacy_page

    seed_legacy_page(wid, vid)
    return await generate_first_look(wid, "a lobby lock waitlist for apartment buildings", venture_id=vid,
                                     venture_name="LobbyLock", allow_model=False)


@pytest.mark.parametrize("text,expected", [
    ("show me the landing page", "site.preview"),
    ("what does my page say", "site.preview"),
    ("rewrite the landing page", "site.rewrite"),
    ("make the headline punchier", "site.rewrite"),
    ("change the CTA to Get early access", "site.rewrite"),
    ("open the waitlist", "waitlist.enable"),
    ("test the waitlist", "waitlist.test"),
    ("publish my landing page", "site.publish"),
    ("publish it", "site.publish"),
    ("don't publish yet", "assistant"),
    ("do not publish the page until I say so", "assistant"),
    ("how do I publish my page?", "assistant"),
    ("rewrite the landing page but don't publish it", "site.rewrite"),
    ("rewrite my landing page and show my website", "site.rewrite"),
    ("show my landing page and test my waitlist", "waitlist.test"),
])
def test_site_intents(text, expected):
    load_company_os_app()
    from app.chat.intent import classify_intent

    assert classify_intent(text) == expected


@pytest.mark.parametrize("headline", ["Make your equipment calibration traceable", "Plan your next family hike"])
def test_preview_is_read_only_summary(env, headline):
    from sqlalchemy import select
    from app.db.models import SiteVersion
    from app.state import db
    from tests.firstlook_legacy import seed_legacy_page

    client, vid, wid = env
    seed_legacy_page(wid, vid)
    with db()() as session:
        row = session.scalar(select(SiteVersion).where(SiteVersion.venture_id == vid))
        row.copy_json = json.dumps({"headline": headline, "subhead": "For people who need this"})
        session.commit()
    msg = _chat(client, vid, "show me the landing page")
    assert headline in msg["body_md"] and "**Subhead:** For people who need this" in msg["body_md"]
    assert "not public" in msg["body_md"]
    assert not any(a.get("href") for a in msg["actions"])  # chat-first: no Website page link


@pytest.mark.parametrize("idea,other_idea", [
    ("Inventory alerts for hospital procurement teams", "A weekend birdwatching diary for families"),
    ("A weekend birdwatching diary for families", "Inventory alerts for hospital procurement teams"),
])
@pytest.mark.parametrize("state", ["ready", "queued", "running", "critiquing", "failed"])
def test_chat_preview_uses_scoped_page_builds_and_keeps_controls_in_chat(env, monkeypatch, idea, other_idea, state):
    import base64
    import hashlib
    from datetime import timedelta
    from html import unescape
    from core.clock import utcnow
    from core.ids import new_id
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.pagebuilder import api
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID

    client, vid, wid = env
    headers = {"X-Subject": "founder"}
    other = client.post("/ventures", json={"raw_idea": other_idea}).json()
    now = utcnow()
    revision_id, session_id = new_id("idv"), new_id("ids")
    html = f"<html><head></head><body><h1>{idea}</h1></body></html>".encode()
    bundle = {"direction": "Your idea", "files": {"index.html": base64.b64encode(html).decode()},
              "manifest": {"artifact_capture": {"manifest": [{"path": "index.html", "size_bytes": len(html),
                                                               "sha256": hashlib.sha256(html).hexdigest()}]}}}
    doc = {"revision_id": "older-confirmed-revision", "variant_count": 1, "_actor_subject": LOCAL_PERSON_ID,
           "_runtime": {"state": state, "stage": "variant_0:generation", "progress": 0.4,
                        "operations": {}, "calls_used": 0, "quote": {"ceiling_micro": 10000, "payer": "platform_credits"}},
           "result": {"bundles": [bundle]}}
    if state == "failed":
        doc["_runtime"]["error"] = "pagebuilder_provider_rate_limited"
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "1")
    monkeypatch.setattr(api, "_candidate_current", lambda value: True)
    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.add(IdeaSession(id=session_id, workspace_id=wid, venture_id=vid, case="know", state="confirmed",
            confirmed_revision_id=revision_id, current_revision_id=revision_id, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id=revision_id, session_id=session_id, workspace_id=wid, venture_id=vid,
            rev=1, source="founder", summary=idea, structured="{}", created_at=now))
        session.add(Job(id="current-preview", kind=api.KIND, workspace_id=wid, venture_id=vid,
            payload_json=json.dumps(doc), status={"ready": "succeeded", "failed": "failed_dead"}.get(state, "scheduled" if state == "queued" else "leased"),
            created_at=now))
        older = {**doc, "_runtime": {**doc["_runtime"], "state": "ready"}}
        session.add(Job(id="older-preview", kind=api.KIND, workspace_id=wid, venture_id=vid,
            payload_json=json.dumps(older), status="succeeded", created_at=now - timedelta(days=1)))
        session.commit()
    texts = ("show me my landing page", "open my website", "preview my landing page draft", "where is my landing page?", "can I see my homepage?")
    for text in texts:
        msg = _chat(client, vid, text)
        assert msg["kind"] == "site_preview", (text, msg)
        assert "private" in msg["body_md"] and "not published" in msg["body_md"]
        assert "publish" in msg["body_md"] and "rebuild" in msg["body_md"]
        selected = next(action for action in msg["actions"] if action.get("action") == "site.preview")["payload"]["job_id"]
        assert selected == ("current-preview" if state == "ready" else "older-preview")
        from app.core.local_mode import is_local
        if is_local():
            prefix = f"/api/ventures/{vid}/pagebuilder/jobs/{selected}"
            expected = {f"{prefix}/preview", f"{prefix}/source"}
            if state == "failed":
                expected.add("/settings/local#coding-tools")
            assert {action["href"] for action in msg["actions"] if action.get("href")} == expected
        else:
            assert all(not action.get("href") for action in msg["actions"])
        if state == "failed":
            reason = "Your AI service is busy" if is_local() else "temporarily busy"
            assert "stopped" in msg["body_md"] and reason in msg["body_md"]
        elif state != "ready":
            assert ("queued" if state == "queued" else "running") in msg["body_md"] and "40%" in msg["body_md"]
        empty = _chat(client, other["id"], text)
        assert "private preview for you" in empty["body_md"]
        assert empty["actions"] == []
    confirm_saved_idea(other["id"])
    for text in texts:
        empty = _chat(client, other["id"], text)
        assert empty["actions"][0]["action"] == "pagebuilder.build"
        assert empty["actions"][0]["payload"] == {"prepare": True}
        assert all(not action.get("href") for action in empty["actions"])
    response = client.get(f"/ventures/{vid}/pagebuilder/profile?job_id={selected}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["current"]["job_id"] == selected
    document = client.get(f"/ventures/{vid}/pagebuilder/jobs/{selected}/preview-document", headers=headers)
    assert document.status_code == 200, document.text
    assert idea in document.json()["html"] and other_idea not in document.json()["html"]
    assert "form-action 'none'" in unescape(document.json()["html"])
    assert client.get(f"/ventures/{vid}/pagebuilder/profile?job_id=missing", headers=headers).status_code == 404
    if state != "ready":
        with db()() as session:
            row = session.get(Job, "older-preview")
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="failed", error="pagebuilder_provider_rate_limited")
            row.status, row.payload_json = "failed_dead", json.dumps(doc)
            session.commit()
        without_preview = _chat(client, vid, "show my landing page")
        assert without_preview["kind"] == "site_preview"
        assert without_preview["actions"][0]["payload"]["job_id"] == "current-preview"
        assert not any(action.get("action") == "pagebuilder.build" for action in without_preview["actions"])
        if is_local() and state == "failed":
            assert "Coding tools" in without_preview["body_md"]
            assert any(action.get("href") == "/settings/local#coding-tools" for action in without_preview["actions"])
        assert client.get(f"/ventures/{vid}/pagebuilder/jobs/current-preview/preview-document", headers=headers).status_code == 409
    with db()() as session:
        for job_id in ("current-preview", "older-preview"):
            row = session.get(Job, job_id)
            row.venture_id, row.workspace_id = other["id"], other["workspace_id"]
        session.commit()
    assert client.get(f"/ventures/{vid}/pagebuilder/profile?job_id={selected}", headers=headers).status_code == 404
    assert _chat(client, vid, "show my landing page")["actions"][0]["action"] == "pagebuilder.build"


@pytest.mark.asyncio
@pytest.mark.hosted
async def test_requested_rewrite_runs_and_passes_instruction(env, monkeypatch):
    client, vid, wid = env
    await _site(wid, vid)
    from app.firstlook import copy as copy_mod, versions

    seen = []
    real = copy_mod.generate_copy

    async def spy(idea, **kw):
        seen.append(kw.get("instruction"))
        copy, cost, _ = await real(idea, **{**kw, "allow_model": False})
        return {**copy, "headline": "Keep your entryway running smoothly"}, cost, False

    monkeypatch.setattr("app.firstlook.service.generate_copy", spy)
    n = versions.get_latest(wid).version
    msg = _chat(client, vid, "make the headline punchier")
    assert not any(card["confirmation"]["action"] == "site.rewrite" for card in _cards(msg))  # the request itself just runs
    assert seen and "make the headline punchier" in seen[0]
    assert versions.get_latest(wid).version == n + 1
    assert "Headline:" in msg["body_md"] and "not public" in msg["body_md"]


@pytest.mark.asyncio
@pytest.mark.hosted
@pytest.mark.parametrize("idea,headline,instruction", [
    ("Equipment inspection logs for workshops", "Keep workshop checks together", "Make the headline easier to scan"),
    ("A family recipe scrapbook", "Save the meals your family loves", "Rewrite the landing page with a warmer headline"),
])
async def test_requested_rewrite_fallback_copy_still_applies(env, monkeypatch, idea, headline, instruction):
    from app.firstlook import service, versions

    from tests.firstlook_legacy import seed_legacy_page

    client, _, _ = env
    made = client.post("/ventures", json={"raw_idea": idea}).json()
    vid, wid = made["id"], made["workspace_id"]
    seed_legacy_page(wid, vid)
    original = await service.generate_first_look(wid, idea, venture_id=vid, allow_model=False)
    seen = []

    async def fallback_copy(*args, **kwargs):
        seen.append(kwargs["instruction"])
        return {**original.copy, "headline": headline}, 0.0, True

    monkeypatch.setattr(service, "generate_copy", fallback_copy)
    reply = _chat(client, vid, instruction)
    saved = versions.get_latest(wid, venture_id=vid)
    assert seen, reply
    assert saved.version == original.version + 1 and saved.fallback
    assert seen and instruction in seen[0]
    assert "Headline:" in reply["body_md"] and "not public" in reply["body_md"]
    assert not any(card["confirmation"]["action"] == "site.rewrite" for card in _cards(reply))


@pytest.mark.asyncio
@pytest.mark.hosted
@pytest.mark.parametrize("idea,name", [
    ("Calibration records for laboratory teams", "BenchRecord"),
    ("Nature walks for new parents", "TrailTogether"),
])
async def test_requested_rewrite_with_identical_copy_keeps_version_and_wallet(env, monkeypatch, idea, name):
    from app.credits.accounts import ensure_account, get_account
    from app.firstlook import versions
    from app.firstlook.service import generate_first_look

    from tests.firstlook_legacy import seed_legacy_page

    client, _, _ = env
    made = client.post("/ventures", json={"raw_idea": idea}).json()
    vid, wid = made["id"], made["workspace_id"]
    seed_legacy_page(wid, vid)
    source = await generate_first_look(wid, idea, venture_id=vid, venture_name=name, allow_model=False)
    account = ensure_account(vid)
    if account is None:
        from app.core.local_mode import is_local
        from app.credits.local_meter import snapshot

        assert is_local()
        spent = snapshot(workspace_id=wid, venture_id=vid)
    else:
        balance = account.granted_micro - account.spent_micro
        held = account.reserved_micro

    async def identical_copy(*args, **kwargs):
        return {key: value for key, value in source.copy.items() if key != "_render_snapshot"}, 0.0, False

    monkeypatch.setattr("app.firstlook.service.generate_copy", identical_copy)
    msg = _chat(client, vid, "Please make your headline clearer")
    assert versions.get_latest(wid, venture_id=vid).id == source.id
    account = get_account(vid)
    if account is None:
        assert snapshot(workspace_id=wid, venture_id=vid) == spent
    else:
        assert account.granted_micro - account.spent_micro == balance
        assert account.reserved_micro == held
    assert "nothing changed" in msg["body_md"].lower()
    assert "review" in msg["body_md"].lower()


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_call_model_includes_instruction(env, monkeypatch):
    _, vid, wid = env
    from app.firstlook import copy as copy_mod

    sent = {}

    async def transmit(messages, **kw):
        sent["user"] = messages[1]["content"]
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}

    monkeypatch.setattr(copy_mod.sole, "transmit", transmit)
    await copy_mod._call_model("an idea", instruction="punchier please", workspace_id=wid, venture_id=vid)
    assert "punchier please" in sent["user"]


@pytest.mark.parametrize("idea", [
    "Inventory alerts for hospital procurement teams",
    "A weekend birdwatching diary for families",
])
def test_requested_waitlist_opens_without_a_card(env, monkeypatch, idea):
    client, vid, _ = env
    from app.chat.site_actions import build_landing_button
    from app.conversion import waitlist
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.commit()

    calls = []
    monkeypatch.setattr(waitlist, "configure", lambda *a, **k: calls.append((a, k)) or {"url": "https://g/x"})
    done = _chat(client, vid, "open the waitlist")
    assert not _cards(done)
    assert calls and calls[0][1]["enabled"] is True
    assert "Waitlist is on. Next: build your landing page" in done["body_md"]  # nothing built yet: never "publish"
    assert "https://g/x" not in done["body_md"] and "unverified" not in done["body_md"]
    confirm_saved_idea(vid)
    done = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "waitlist.enable", "payload": {}}).json()
    build = done["actions"][0]
    if is_local():
        assert "Coding tools" in build.pop("disabled_reason")
    assert build == build_landing_button()
    assert [a["label"] for a in done["actions"] if a.get("href")] == ["Open your waitlist form"]
    assert not any(a.get("confirmation", {}).get("action") == "site.publish" for a in done["actions"])


def test_waitlist_test_reports_honestly(env, monkeypatch):
    client, vid, _ = env
    from app.conversion import waitlist

    monkeypatch.setattr(waitlist, "probe", lambda *a: {"submission_verified": True})
    ok = _chat(client, vid, "test the waitlist")
    assert "no real email" in ok["body_md"] and "not counted as a sign-up" in ok["body_md"]

    def boom(*a):
        raise waitlist.WaitlistError("waitlist_public_submission_unconfirmed", 503)

    monkeypatch.setattr(waitlist, "probe", boom)
    bad = _chat(client, vid, "test the waitlist")
    assert "did not complete" in bad["body_md"] and "passed" not in bad["body_md"]
    assert any(a.get("action") == "blocker.explain" for a in bad["actions"])


@pytest.mark.parametrize("env", ["Purchase approvals for distributors", "Music lessons for families"], indirect=True)
def test_coding_failure_copy_is_plain_and_offers_retry(env, monkeypatch):
    from app.chat.mac_stop import post_mac_stopped
    from app.chat.store import list_messages
    from app.build.device_orders import KIND
    from app.db.models import VentureDocument
    from app.org.characters import parse_speaker
    from app.state import db
    from core.clock import utcnow

    _, vid, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with db()() as session:
        for order_id in ("failed_order", "retry_order"):
            session.add(VentureDocument(id=order_id, venture_id=vid, kind=KIND, title="Requested page", body_md="",
                provenance_json=json.dumps({"manifest": {"task_id": "landing_page_" + order_id,
                    "coding_loop_verification": {"provider": "codex"}}, "task_snapshot": {"kind": "landing_page_build"}}),
                created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    for order_id in ("failed_order", "retry_order"):
        assert post_mac_stopped(vid, order_id, None, "Build your requested page", outcome_code="needs_founder")
    message = next(item for item in list_messages(vid) if item.get("kind") == "mac_task_stopped")
    assert "your coding tool" in message["body_md"].lower()
    assert "Mac" not in message["body_md"]
    assert "closed task" not in message["body_md"]
    assert parse_speaker(message["body_md"])[1] == (
        "Your coding tool stopped before I could verify its work. Choose Retry to review it.")
    assert message["actions"][0]["label"] == "Retry"
    notices = [item for item in list_messages(vid) if item.get("kind") == "mac_task_stopped"]
    assert len(notices) == 2
    assert {item["actions"][0]["payload"]["task_id"] for item in notices} == {"landing_page_build"}
    assert {item["actions"][0]["payload"]["order_id"] for item in notices} == {"failed_order", "retry_order"}


def test_mac_stopped_milestone_explains_and_offers(env):
    from app.chat.mac_stop import post_mac_stopped
    from app.chat.store import list_messages

    _, vid, _ = env
    assert post_mac_stopped(vid, "dwo_1", "cu_permission_missing", "Open Settings")
    assert not post_mac_stopped(vid, "dwo_1", "cu_permission_missing")  # once per order
    assert not post_mac_stopped(vid, "dwo_2", "not_a_code")
    msg = next(m for m in list_messages(vid) if m.get("kind") == "mac_task_stopped")
    acts = msg["actions"]
    assert acts[0]["action"] == "mac.task.prepare"
    assert any(a.get("action") == "blocker.explain" and a["payload"] == {"code": "computer_use_permissions"} for a in acts)


def test_mac_finished_milestone_once_with_review_link(env):
    from app.chat.mac_stop import post_mac_finished
    from app.chat.store import list_messages

    _, vid, _ = env
    assert post_mac_finished(vid, "dwo_1", "Open Safari")
    assert not post_mac_finished(vid, "dwo_1")
    msg = next(m for m in list_messages(vid) if m.get("kind") == "mac_task_finished")
    assert msg["actions"][0]["href"] == f"/v/{vid}/device-work-orders/dwo_1"


def test_publish_blockers_then_requested_publish(env, monkeypatch):
    client, vid, _ = env
    from app.chat import site_actions as sa
    from app.hosting import service as hosting

    queued = []
    monkeypatch.setattr(hosting, "queue_accepted_build", lambda *a, **k: queued.append(1) or {"state": "pending"})
    blocked = _chat(client, vid, "publish my landing page")
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import blocked_message

        assert blocked_message("publish_pages") in blocked["body_md"] and not queued
        assert not _cards(blocked)
        return
    assert blocked["kind"] == "needs_you" and "Nothing went public" in blocked["body_md"]
    assert "no hosted app" in blocked["body_md"] and "waitlist is off" in blocked["body_md"]
    ex = [a for a in blocked["actions"] if a.get("action") == "blocker.explain"]
    assert len(ex) == 1 and ex[0]["payload"] == {"code": "no_hosted_app"}
    assert all(c["confirmation"]["action"] != "site.publish" for c in _cards(blocked))
    assert all(a.get("href") or a.get("action") or a.get("confirmation") for a in blocked["actions"])

    monkeypatch.setattr(sa, "publish_preflight", lambda v: ("app_1", "run_1", []))
    published = _chat(client, vid, "publish my landing page")
    assert not _cards(published) and "unverified" not in published["body_md"]
    assert queued and "not live yet" in published["body_md"]


@pytest.mark.asyncio
async def test_publish_executor_needs_actor_and_clear_preflight(env, monkeypatch):
    _, vid, wid = env
    from app.chat import site_actions as sa
    from app.chat.execute import execute_action
    from app.hosting import service as hosting
    from app.db.models import Person, WorkspaceMember
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        session.add(Person(id="person_1", display_name="Taylor", created_at=utcnow()))
        session.flush()
        session.add(WorkspaceMember(id="wsm_publish", workspace_id=wid, person_id="person_1",
                                    role="maintainer", can_approve=1, created_at=utcnow()))
        session.commit()

    queued = []
    monkeypatch.setattr(hosting, "queue_accepted_build", lambda *a, **k: queued.append(1) or {"state": "pending"})
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import blocked_message

        with pytest.raises(PermissionError):
            await sa.execute(vid, "site.publish", {}, None)
        out = await execute_action(vid, "site.publish", actor_subject="person_1")
        assert blocked_message("publish_pages") in out["body_md"] and not queued
        return
    with pytest.raises(PermissionError):
        await execute_action(vid, "site.publish", actor_subject=None)
    out = await execute_action(vid, "site.publish", actor_subject="person_1")  # real preflight: no app
    assert out["kind"] == "needs_you" and not queued
    monkeypatch.setattr(sa, "publish_preflight", lambda v: ("app_1", "run_1", []))
    out = await execute_action(vid, "site.publish", actor_subject="person_1")
    assert queued == [1] and "not live yet" in out["body_md"]


def test_negation_creates_no_card(env):
    client, vid, _ = env
    for text in ("don't publish yet", "do not open the waitlist yet"):
        msg = _chat(client, vid, text)
        assert not _cards(msg) and msg["kind"] != "confirmation", text


@pytest.mark.hosted
def test_first_look_milestone_chains_waitlist_rewrite_preview(env):
    _, vid, _ = env
    from sqlalchemy import select

    from app.chat.milestone_hooks import first_look_ready, waitlist_enabled
    from app.chat.site_actions import build_landing_button
    from app.db.models import ChatMessage
    from app.state import db

    first_look_ready(vid, "sv9", {"headline": "Doors that open for tenants", "subhead": "A lobby lock for buildings."})
    waitlist_enabled(vid)
    with db()() as s:
        rows = {m.kind: m for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid))}
    fl = rows["first_look_ready"]
    acts = json.loads(fl.actions_json)
    assert "Doors that open for tenants" in fl.body_md
    assert [a["label"] for a in acts] == ["Open waitlist", "Rewrite", "Open preview"]
    assert acts[0]["confirmation"]["action"] == "waitlist.enable" and acts[2]["style"] == "secondary"
    on = rows["waitlist_on"]
    assert "Waitlist is on. Next: build your landing page" in on.body_md
    assert json.loads(on.actions_json)[0] == build_landing_button()


def test_experiment_next_test_is_a_whole_sentence_with_direction_buttons():
    from app.chat.experiments import _direction_buttons, _sentence

    long = ("Ask five groomers whether they track appointments on paper. Count how many say they lose revenue to "
            "no-shows or over-order supplies because they cannot see stock in the shop before the week starts.")
    out = _sentence(long, 150)
    assert out == "Ask five groomers whether they track appointments on paper." and "\u2026" not in out
    assert _sentence("Short test.") == "Short test."
    assert [b["label"] for b in _direction_buttons()] == ["Keep going", "Narrow the wedge", "Change the offer", "Try the next idea"]
    assert all(b["action"] == "chat.reply" for b in _direction_buttons())


_NAMED_SYNTHETIC_TEST = ("Test the existing KennelScheduler waitlist with Supymem's synthetic submission check. "
    "Record this only as an internal functional test, never a real user or customer. "
    "Do not publish the landing page or send any invitation.")


@pytest.mark.parametrize("idea", [
    "Inventory alerts for hospital procurement teams",
    "A weekend birdwatching diary for families",
])
def test_exact_named_internal_probe_keeps_scope_and_runs_only_current_waitlist(env, monkeypatch, idea):
    from app.chat import assistant
    from app.chat.intent import split_speech_acts
    from app.conversion import waitlist
    from app.db.models import Job, Venture
    from app.state import db
    from sqlalchemy import select
    client, vid, wid = env
    with db()() as session:
        session.get(Venture, vid).name = 'Kennel Scheduler'
        session.get(Venture, vid).raw_idea = idea
        session.commit()
        before = list(session.scalars(select(Job.id)))
    calls = []
    def probe(v, w):
        calls.append((v, w))
        return {'submission_verified': True}
    async def forbidden(*_a, **_kw):
        raise AssertionError('internal probe must not call model or publish/send')
    monkeypatch.setattr(waitlist, 'probe', probe)
    monkeypatch.setattr(assistant, '_complete', forbidden)
    monkeypatch.setattr('app.chat.site_actions._publish', forbidden)
    assert split_speech_acts(_NAMED_SYNTHETIC_TEST) == [('waitlist.test', _NAMED_SYNTHETIC_TEST)]
    reply = _chat(client, vid, _NAMED_SYNTHETIC_TEST)
    assert calls == [(vid, wid)]
    assert 'test passed' in reply['body_md'] and 'not counted as a sign-up' in reply['body_md']
    assert reply['actions'] == []
    confirm_saved_idea(vid)
    reply = _chat(client, vid, _NAMED_SYNTHETIC_TEST)
    assert calls == [(vid, wid), (vid, wid)]
    assert 'test passed' in reply['body_md'] and 'not counted as a sign-up' in reply['body_md']
    assert reply['actions'] and all(a.get('action') == 'site.preview' and not a.get('href') for a in reply['actions'])
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == before


def test_named_internal_probe_refuses_different_current_venture(env, monkeypatch):
    from app.conversion import waitlist
    def forbidden(*_a, **_kw):
        raise AssertionError('different named waitlist must not be probed')
    monkeypatch.setattr(waitlist, 'probe', forbidden)
    client, vid, _ = env
    reply = _chat(client, vid, _NAMED_SYNTHETIC_TEST)
    assert 'does not match this venture' in reply['body_md'] and 'No test ran' in reply['body_md']
    assert all(a.get('action') in {None, 'site.preview'} for a in reply['actions'])  # read-only, in chat


@pytest.mark.parametrize('extra', [' Then publish it.', ' Send an invitation.', ' Test another venture.', ' Record it as a real user.'])
def test_named_internal_probe_does_not_relax_other_compound_forms(extra):
    from app.chat.site_actions import classify_site_intent, named_synthetic_waitlist_test
    assert named_synthetic_waitlist_test(_NAMED_SYNTHETIC_TEST + extra) is None
    assert classify_site_intent(_NAMED_SYNTHETIC_TEST + extra) is None


def test_waitlist_enable_confirm_probes_and_reports(env, monkeypatch):
    client, vid, _ = env
    from app.conversion import waitlist

    monkeypatch.setattr(waitlist, "configure", lambda *a, **k: {"url": "https://g/x"})
    monkeypatch.setattr(waitlist, "probe", lambda *a: {"submission_verified": True})
    done = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "waitlist.enable", "payload": {}}).json()
    assert "Waitlist on and tested" in done["body_md"]

    def boom(*a):
        raise waitlist.WaitlistError("waitlist_public_submission_unconfirmed")
    monkeypatch.setattr(waitlist, "probe", boom)
    done = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "waitlist.enable", "payload": {}}).json()
    assert "did not pass" in done["body_md"] and "did not confirm" in done["body_md"]


@pytest.mark.parametrize("host", ["published.inventory.example", "sent.family-outings.example"])
def test_claim_scrub_keeps_urls_without_allowing_unverified_publication_claims(host):
    from types import SimpleNamespace
    from app.chat.router import scrub_hallucinations

    context = SimpleNamespace(governance={"autonomy": "L1"}, website={"published": False})
    url = "https://" + host + "/launch"
    result = scrub_hallucinations("I published your page. View " + url, context)
    assert url in result
    assert result.startswith("I prepared your page.")


def test_untested_but_enabled_waitlist_offers_test_not_enable_again(env, monkeypatch):
    _, vid, _ = env
    from app.chat import site_actions as sa

    monkeypatch.setattr(sa, "_waitlist_state", lambda v: {"ok": True, "enabled": True})
    btn = sa._FL_BUTTONS["hosting_conversion_verification_required"](vid)
    assert btn["action"] == "waitlist.test"
    monkeypatch.setattr(sa, "_waitlist_state", lambda v: {"ok": True, "enabled": False})
    assert sa._FL_BUTTONS["hosting_conversion_verification_required"](vid)["label"] == "Turn on / test the waitlist"


@pytest.mark.parametrize("env", ["Inventory planning for repair shops", "Cycling weekends for families"], indirect=True)
@pytest.mark.asyncio
async def test_waitlist_destination_probe_preserves_your_next_step_without_claiming_verification(env, monkeypatch):
    from app.chat import site_actions
    from app.conversion import waitlist

    _, venture_id, _ = env
    note = "Your email destination is ready. Import consented signups in Website > Collect signups."
    monkeypatch.setattr(waitlist, "configure", lambda *args, **options: {"url": "https://your-site.test/waitlist"})
    monkeypatch.setattr(waitlist, "probe", lambda *args: {"submission_verified": False, "destination_tested": True, "note": note})
    monkeypatch.setattr(site_actions, "after_waitlist", lambda *args: ("Open Website to review your page.", []))
    reply = await site_actions._enable(venture_id, {}, "founder")
    assert note in reply["body_md"]
    assert "Nothing counted as verified" in reply["body_md"] and "Waitlist on and tested" not in reply["body_md"]
    assert "did not pass" not in reply["body_md"] and reply["kind"] == "needs_you"


@pytest.mark.parametrize("env", ["a parts tracker for repair shops", "a scrapbook for hiking families"], indirect=True)
def test_local_linkedin_mark_rolls_back_a_failed_write(env, monkeypatch):
    from app.gtm import linkedin_people as people
    from app.state import db

    _, venture_id, _ = env
    with db()() as session:
        row, data = people._doc(session, venture_id, create=True)
        data["people"] = [{"id": "saved-profile", "url": "https://www.linkedin.com/in/public-profile",
                           "sent_at": None, "replied_at": None}]
        people._save(row, data)
        session.commit()

    save = people._save

    def failed_save(row, data):
        save(row, data)
        raise OSError("write interrupted")

    monkeypatch.setattr(people, "_save", failed_save)
    with pytest.raises(OSError, match="write interrupted"):
        people.local_mark(venture_id, "founder", "saved-profile", sent=True, replied=True)
    with db()() as session:
        _, saved = people._doc(session, venture_id)
        assert saved["people"][0]["sent_at"] is None
        assert saved["people"][0]["replied_at"] is None


@pytest.mark.parametrize("env", ["a dispatch board for couriers", "a recipe journal for families"], indirect=True)
@pytest.mark.parametrize("host", ["inventory.example", "gardening.example"])
def test_fixed_waitlist_ui_keeps_local_measurement(env, monkeypatch, host):
    from app.api import waitlist_api
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(waitlist_api.waitlist, "beacon_key", lambda venture_id: "pk_app_fixture")
    monkeypatch.setattr(beacon, "public_events_url", lambda: f"https://{host}/e")
    content = f"<p>Join {host}</p>"
    body, enabled = waitlist_api._waitlist_body_with_beacon("venture_owned", host, content)
    assert enabled is True
    assert body == f"<main><h1>{host}</h1>{content}</main>" + beacon.script_tag(key="pk_app_fixture")


@pytest.mark.parametrize("env", ["a dispatch board for couriers", "a recipe journal for families"], indirect=True)
def test_waitlist_beacon_lookup_stays_in_the_service_and_preserves_fallback(env, monkeypatch):
    from app.api.waitlist_api import _waitlist_body_with_beacon
    from app.apps.keys import ensure_ingest_key
    from app.conversion import waitlist
    from app.db.supymem_models import FounderApp
    from app.product_analytics import beacon
    from app.state import db
    from core.clock import utcnow

    _, venture_id, workspace_id = env
    assert waitlist.beacon_key(venture_id) is None
    now = utcnow()
    with db()() as session:
        session.add(FounderApp(id="waitlist-app", workspace_id=workspace_id, venture_id=venture_id,
                              slug="waitlist-app", name="Your app", kind="web", created_at=now, updated_at=now))
        session.commit()
    key = waitlist.beacon_key(venture_id)
    assert key == ensure_ingest_key("waitlist-app")["secret"]
    assert waitlist.beacon_key("missing-venture") is None

    def interrupted_beacon(*args, **kwargs):
        raise OSError("beacon unavailable")

    monkeypatch.setattr(beacon, "script_tag", interrupted_beacon)
    body, enabled = _waitlist_body_with_beacon(venture_id, "Your waitlist", "<p>Join your waitlist</p>")
    assert body == "<main><h1>Your waitlist</h1><p>Join your waitlist</p></main>"
    assert enabled is False


@pytest.mark.parametrize("env", ["Inventory planning for repair shops", "Walking weekends for families"], indirect=True)
def test_current_model_exports_keep_the_requested_variant(env, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.db.models import Job
    from app.pagebuilder import api, local_publish
    from app.state import db
    from core.clock import utcnow

    _, venture_id, workspace_id = env
    with db()() as session:
        session.add(Job(id="model-export", kind=api.KIND, workspace_id=workspace_id, venture_id=venture_id,
            status="succeeded", payload_json="{}", created_at=utcnow()))
        session.commit()
    selected = []

    def load(venture, job_id, variant, caller, *, deployed):
        selected.append((venture, job_id, variant, caller, deployed))
        return [local_publish._file("index.html", b"<html><body>Your selected design</body></html>", "text/html; charset=utf-8")], "Your selected design"

    monkeypatch.setattr(local_publish, "_site_scope", lambda *args: (workspace_id, "owner"))
    monkeypatch.setattr(local_publish, "_load", load)
    assert local_publish.export_current(venture_id, variant=1, caller="owner").status_code == 200
    assert local_publish.export_current_html(venture_id, variant=1, caller="owner").status_code == 200
    assert selected == [(venture_id, "model-export", 1, "owner", False)] * 2
