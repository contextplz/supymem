"""Charlie leads: the "since you left" brief, event nudges (cap, quiet hours, dedupe), stale work and the next-step rule."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from tests.company_os_harness import load_company_os_app

NOON = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pro.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from tests.company_os_harness import company_os_test_client
    from app import state as state_mod
    from app.memory import reset_memory_for_tests
    from core.clock import set_clock

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    clock = {"now": NOON}
    with company_os_test_client(main.create_app()) as client:
        made = client.post("/ventures", json={"raw_idea": "a care-coordination app for adult children of aging parents"})
        assert made.status_code == 201, made.text
        vid = made.json()["id"]
        from app.db.models import ChatMessage, Job
        from app.state import db
        with db()() as s:  # start from a quiet venture
            s.execute(delete(Job).where(Job.venture_id == vid))
            s.execute(delete(ChatMessage).where(ChatMessage.venture_id == vid))
            s.commit()
        set_clock(lambda: clock["now"])
        try:
            yield client, vid, clock
        finally:
            set_clock(None)


def _job(vid, kind, status, *, created, finished=None, error=None):
    from app.db.models import Job, Venture
    from app.state import db
    from core.ids import new_id

    with db()() as s:
        wid = s.get(Venture, vid).workspace_id
        job = Job(id=new_id("job"), kind=kind, venture_id=vid, workspace_id=wid, payload_json="{}", status=status,
                  created_at=created, started_at=created, finished_at=finished, last_error=error)
        s.add(job)
        s.commit()
        return job.id


def _messages(vid, kind):
    from app.db.models import ChatMessage
    from app.state import db

    with db()() as s:
        return [(m.body_md, json.loads(m.actions_json or "[]")) for m in
                s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == kind))]


def _notices(vid):
    from app.db.supymem_models import Notice
    from app.state import db

    with db()() as s:
        return [n.title for n in s.scalars(select(Notice).where(Notice.kind.like("nudge:%")))]


def _user_said(vid, at):
    from app.db.models import ChatMessage, Venture
    from app.state import db
    from core.ids import new_id

    with db()() as s:
        s.add(ChatMessage(id=new_id("msg"), venture_id=vid, workspace_id=s.get(Venture, vid).workspace_id,
                          role="user", kind="text", body_md="hi", created_at=at))
        s.commit()


# ---- "since you left" brief ----

@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_chat_history_returns_before_a_busy_visit_marker_write(env, idea, mode, monkeypatch):
    import asyncio
    import sqlite3
    from fastapi import FastAPI
    from app.api.chat_api import router
    from app.chat.conversations import create
    from app.db.models import ChatMessage, Venture, Workspace
    from app.state import db
    from app.tenancy import bind

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    _, vid, _ = env
    with db()() as session:
        venture = session.get(Venture, vid)
        venture.raw_idea = idea
        wid = venture.workspace_id
        actor = session.get(Workspace, wid).owner_id
        filename = session.get_bind().url.database
        session.commit()
    app = FastAPI()
    app.include_router(router)

    async def request():
        bodies = []
        with bind(workspace_id=wid, venture_id=vid, actor=actor):
            conversation = create(vid, idempotency_key="busy-visit-marker", title=idea)
            with db()() as session:
                session.add(ChatMessage(id="busy-visit-" + vid, venture_id=vid, workspace_id=wid,
                    conversation_id=conversation["id"], role="assistant", kind="text", body_md=idea, created_at=NOON))
                session.commit()
            with sqlite3.connect(filename) as writer:
                writer.execute("BEGIN IMMEDIATE")
                loop = asyncio.get_running_loop()
                release = loop.call_later(2, writer.rollback)

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    if message["type"] == "http.response.body":
                        bodies.append((writer.in_transaction, message["body"]))
                        writer.rollback()

                try:
                    await app({"type": "http", "method": "GET", "path": f"/ventures/{vid}/chat",
                        "query_string": f"conversation_id={conversation['id']}".encode(), "headers": [],
                        "scheme": "http", "server": ("127.0.0.1", 3100), "client": ("127.0.0.1", 1234)}, receive, send)
                finally:
                    release.cancel()
        assert bodies and bodies[0][0], "Chat history waited for the optional visit-marker write"
        history = json.loads(bodies[0][1])
        assert history["conversation_id"] == conversation["id"]
        assert history["items"][0]["body_md"] == idea
        with db()() as session:
            metadata = json.loads(session.get(ChatMessage, "busy-visit-" + vid).citations_json or "[]")
        assert any("_proactive" in item for item in metadata) is (mode == "hosted")

    asyncio.run(request())


@pytest.mark.hosted
@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_brief_posts_once_after_inactivity_not_twice(env, monkeypatch, idea):
    client, vid, clock = env
    from app.chat import proactive
    from app.chat.store import append_message, list_messages
    from app.db.models import Venture, VentureDocument
    from app.state import db

    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.commit()
    monkeypatch.setattr(proactive, "_spawn", lambda fn, *args: fn(*args))
    _user_said(vid, NOON - timedelta(hours=5))
    reply = append_message(vid, role="assistant", body_md="Your saved next steps are ready.")
    assert reply["next_moves"]["actions"]
    with db()() as session:
        documents = list(session.scalars(select(VentureDocument.id)))
    assert client.get(f"/ventures/{vid}/chat").status_code == 200
    assert list_messages(vid)[-1] == reply
    clock["now"] = NOON + timedelta(minutes=30)  # still here, polling: no second brief
    client.get(f"/ventures/{vid}/chat")
    clock["now"] = NOON + timedelta(hours=2)
    client.get(f"/ventures/{vid}/chat")
    assert list_messages(vid)[-1] == reply
    clock["now"] = NOON + timedelta(hours=6)  # away 4 hours: one more
    client.get(f"/ventures/{vid}/chat")
    assert list_messages(vid)[-1] == reply
    assert len(list_messages(vid)) == 2
    assert not _messages(vid, "next_moves")
    with db()() as session:
        assert list(session.scalars(select(VentureDocument.id))) == documents


def test_brief_not_posted_for_a_brand_new_founder_or_a_short_gap(env):
    _, vid, clock = env
    from app.chat import proactive

    assert not proactive.claim_brief(vid)  # nothing seen yet: onboarding owns the first open
    clock["now"] = NOON + timedelta(minutes=20)
    assert not proactive.claim_brief(vid)


def test_brief_content_comes_from_state_with_stubbed_model(env, monkeypatch):
    _, vid, clock = env
    from app.chat import proactive
    from app.credits import metered
    from app.db.models import VentureDocument
    from app.state import db

    _job(vid, "research.deep", "failed", created=NOON - timedelta(hours=4), finished=NOON - timedelta(hours=3),
         error="research_provider_offline")
    with db()() as session:
        session.add(VentureDocument(id="journey", venture_id=vid, kind="gtm_journey", title="Search", body_md="",
            provenance_json=json.dumps({"state": "ready", "reachable_leads": [{}, {}]}),
            created_at=NOON, updated_at=NOON))
        session.commit()
    seen = {}

    class _R:
        choices = [type("C", (), {"message": type("M", (), {"content": "Welcome back — research stopped. Retry it?"})()})()]

    async def fake_llm(messages, **kw):
        seen["facts"] = messages[1]["content"]
        seen["tier"] = kw["tier"]
        return _R()

    monkeypatch.setattr(metered, "llm", fake_llm)
    assert proactive.post_brief(vid, since=NOON - timedelta(hours=5))
    body, actions = _messages(vid, proactive.BRIEF)[0]
    assert seen["tier"] == "flash" and "Research did not finish 3 hours ago" in seen["facts"]
    assert "—" not in body and "Welcome back, research stopped" in body
    assert any(a.get("action") == "job.retry" for a in actions)  # the stale risk carries retry
    assert any(a.get("payload", {}).get("text") == "Skip research" for a in actions)


def test_brief_falls_back_to_deterministic_text_without_a_model(env):
    _, vid, _ = env
    from app.chat import proactive

    assert proactive.post_brief(vid, since=NOON - timedelta(hours=5))  # LLM_OFFLINE: metered call fails
    body, actions = _messages(vid, proactive.BRIEF)[0]
    assert "Welcome back. Since you left:" in body and actions


@pytest.mark.parametrize("idea", ["help people find a gym", "help pottery studios fill classes"])
@pytest.mark.parametrize("reachable_count,state", [(0, None), (0, "ready"), (0, "needs_input"), (2, "ready")])
def test_customer_brief_counts_only_reachable_leads(env, monkeypatch, idea, reachable_count, state):
    from types import SimpleNamespace

    from app.chat import proactive
    from app.credits import metered
    from app.db.models import VentureDocument
    from app.state import db

    client, _, _ = env
    vid = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
    data = {"state": state, "job_id": "job_search", "reachable_leads": [{}] * reachable_count,
            "industry_experts": [{}], "need_signals": [{}], "channels": [{}],
            "discovery_notice": "No reachable matches were verified" if not reachable_count else ""}
    if state is not None:
        with db()() as session:
            session.add(VentureDocument(id="journey_result", venture_id=vid, kind="gtm_journey", title="Search",
                                       body_md="", provenance_json=json.dumps(data), created_at=NOON, updated_at=NOON))
            session.commit()
    calls = []

    async def model(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="Welcome back. Your leads are ready, and your saved idea and answers are waiting."))])

    monkeypatch.setattr(metered, "llm", model)
    assert proactive.post_brief(vid, since=NOON - timedelta(hours=5))
    body, actions = _messages(vid, proactive.BRIEF)[0]
    events = [event for event in proactive.events(vid) if event["key"].startswith("leads")]
    if state is None:
        assert events == [] and calls == [] and "leads are ready" not in body
        return
    assert len(events) == 1
    if reachable_count:
        assert len(calls) == 1 and "Your leads are ready" in body
        assert "I found 2 people" in events[0]["body"] and "I found 5" not in events[0]["body"]
    else:
        assert calls == []
        assert "No reachable leads found yet" in body
        assert "public communities" in body and "add someone you know" in body
        assert "leads are ready" not in body and "first messages" not in events[0]["body"]
        assert actions[0]["action"] == "openworker_search"


# ---- event nudges ----

@pytest.mark.parametrize("summary,problem,offer,customers,new_customer", [
    ("Equipment maintenance scheduling", "Missed maintenance visits", "A repair scheduling board",
     ["Repair shop operators", "Fleet managers"], "Equipment rental teams"),
    ("Stargazing outing planning", "Unclear viewing conditions", "A night-sky outing planner",
     ["Astronomy hobbyists", "Camping families"], "Amateur photographers"),
])
def test_idea_nudge_ignores_customer_order_and_duplicates_but_keeps_real_changes(
        env, summary, problem, offer, customers, new_customer):
    _, vid, _ = env
    from app.chat import proactive
    from app.db.models import Venture
    from app.state import db
    from app.ventures import idea_page

    with db()() as session:
        session.get(Venture, vid).raw_idea = summary
        session.commit()
    page = {"summary": summary, "problem": problem, "offer": offer, "customers": {"current": customers}}
    idea_page.save(vid, page)
    assert proactive.sweep(vid) == 0
    for current in (list(reversed(customers)), [*customers, customers[0].upper()], customers):
        idea_page.save(vid, {**page, "customers": {"current": current}, "what_changed": ["Formatting refreshed"]})
        assert proactive.sweep(vid) == 0
        assert _messages(vid, proactive.NUDGE) == []
        assert _notices(vid) == []
    legacy_parts = [summary, problem, offer, *reversed(customers)]
    legacy_mark = hashlib.sha256(json.dumps([" ".join(part.lower().split()) for part in legacy_parts]).encode()).hexdigest()[:16]
    with db()() as session:
        document, data = proactive._state(session, vid)
        proactive._write(session, vid, document, {**data, "idea": legacy_mark})
        session.commit()
    assert proactive.sweep(vid) == 0
    with db()() as session:
        assert proactive._state(session, vid)[1]["idea"] == proactive._idea_mark(vid)[0]
    idea_page.save(vid, {**page, "customers": {"current": [*customers, new_customer]}})
    assert proactive.sweep(vid) == 1
    assert len(_messages(vid, proactive.NUDGE)) == len(_notices(vid)) == 1
    assert proactive.sweep(vid) == 0


@pytest.mark.parametrize("name,summary", [
    ("Stockroom", "Warehouse inventory alerts\u2014for logistics teams."),
    ("Stride", "A weekend running club\u2014for neighborhood families."),
])
def test_idea_notice_keeps_plain_title_and_the_same_saved_chat_copy(env, name, summary):
    _, vid, _ = env
    from app import desktop_notices
    from app.chat import proactive
    from app.chat.milestones import scrub
    from app.db.models import Venture
    from app.org.characters import parse_speaker
    from app.state import db
    from app.ventures import idea_page

    with db()() as session:
        venture = session.get(Venture, vid)
        venture.name, venture.raw_idea = name, summary
        session.commit()
    idea_page.save(vid, {"summary": "The initial saved summary.", "problem": "p", "offer": "o"})
    proactive.sweep(vid)
    idea_page.save(vid, {"summary": summary, "problem": "p", "offer": "o"})
    assert proactive.sweep(vid) == 1
    notice, = desktop_notices.pending()
    message, = _messages(vid, proactive.NUDGE)
    assert notice["title"] == "Your Idea page changed"
    assert notice["venture_name"] == name
    assert notice["body"] == parse_speaker(message[0])[1]
    assert scrub(summary) in notice["body"]
    assert "\u2014" not in notice["body"]
    proactive.sweep(vid)
    assert len(_messages(vid, proactive.NUDGE)) == len(desktop_notices.pending()) == 1


@pytest.mark.parametrize("summary,updated,founder_text", [
    ("Route planning for waste collection supervisors", "Route planning for regional recycling contractors",
     "Change the audience to regional recycling contractors."),
    ("Shared recipe journals for home cooks", "Shared recipe journals for friends learning to bake",
     "My customers are friends learning to bake, not experienced home cooks."),
])
def test_idea_nudge_does_not_repeat_recent_founder_changes(env, summary, updated, founder_text):
    _, vid, clock = env
    from app.chat import proactive
    from app.db.models import ChatMessage, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.state import db
    from app.ventures import idea_page
    from core.ids import new_id

    idea_page.save(vid, {"summary": summary, "problem": "Coordination", "offer": "Shared plans"})
    assert proactive.sweep(vid) == 0
    clock["now"] += timedelta(minutes=1)
    with db()() as session:
        workspace_id = session.get(Venture, vid).workspace_id
        idea = IdeaSession(id=new_id("ids"), workspace_id=workspace_id, venture_id=vid, case="know",
                           state="confirmed", created_at=clock["now"], updated_at=clock["now"])
        session.add(idea)
        session.flush()
        revision = IdeaRevision(id=new_id("idv"), session_id=idea.id, workspace_id=workspace_id,
                                venture_id=vid, rev=1, source="founder", summary=updated, created_at=clock["now"])
        session.add(revision)
        session.add(ClarifierTurn(id=new_id("ict"), session_id=idea.id, workspace_id=workspace_id,
                                  venture_id=vid, turn=1, role="founder", kind="message", text=founder_text,
                                  revision_id=revision.id, created_at=clock["now"]))
        session.add(ChatMessage(id=new_id("msg"), workspace_id=workspace_id, venture_id=vid,
                                role="user", body_md=founder_text, created_at=clock["now"]))
        session.commit()
    _job(vid, "gtm.journey.prepare", "running", created=clock["now"])
    clock["now"] += timedelta(minutes=1)
    _user_said(vid, clock["now"])
    idea_page.save(vid, {"summary": updated, "problem": "Coordination", "offer": "Shared plans"})
    assert proactive.sweep(vid) == 0
    assert _messages(vid, proactive.NUDGE) == [] and _notices(vid) == []
    clock["now"] += timedelta(minutes=6)
    assert proactive.sweep(vid) == 0
    assert _messages(vid, proactive.NUDGE) == [] and _notices(vid) == []
    _user_said(vid, clock["now"])
    idea_page.save(vid, {"summary": updated, "problem": "Missed reminders", "offer": "Shared plans"})
    assert proactive.sweep(vid) == 1
    assert len(_messages(vid, proactive.NUDGE)) == len(_notices(vid)) == 1
    clock["now"] += timedelta(minutes=1)
    audience = updated.split(" for ", 1)[1]
    with db()() as session:
        session.add(ChatMessage(id=new_id("msg"), workspace_id=workspace_id, venture_id=vid,
                                role="user", body_md=audience + ".", created_at=clock["now"]))
        session.commit()
    idea_page.save(vid, {"summary": updated, "problem": "Missed reminders", "offer": "Shared plans",
                         "customers": {"current": [audience]}})
    assert proactive.sweep(vid) == 0
    clock["now"] += timedelta(minutes=6)
    assert proactive.sweep(vid) == 0
    assert len(_messages(vid, proactive.NUDGE)) == len(_notices(vid)) == 1


def _seed_events(vid, now):
    """One of each event type, all fresh."""
    from app.db.models import GtmContact, GtmTouch, Venture, VentureDocument
    from app.state import db
    from app.ventures import idea_page
    from core.ids import new_id

    _job(vid, "research.deep", "succeeded", created=now - timedelta(hours=2), finished=now - timedelta(minutes=10))
    _job(vid, "idea.validate", "failed", created=now - timedelta(minutes=40), finished=now - timedelta(minutes=20),
         error="handler_failed")
    _job(vid, "pagebuilder.build", "running", created=now - timedelta(minutes=50))
    with db()() as s:
        wid = s.get(Venture, vid).workspace_id
        s.add(VentureDocument(id=new_id("gtmj"), venture_id=vid, kind="gtm_journey", title="x", body_md="",
            provenance_json=json.dumps({"state": "ready", "job_id": "job_x", "reachable_leads": [{}, {}]}),
            created_at=now, updated_at=now))
        for i, d in enumerate([{"state": "draft", "channel": "email", "batch_id": "b1"},
                               {"state": "draft", "channel": "email", "error": "gmail_send_rejected"}]):
            s.add(VentureDocument(id=f"vdoc_draft{i}", venture_id=vid, kind="outreach_draft", title="d", body_md="",
                                  provenance_json=json.dumps(d), created_at=now, updated_at=now))
        s.add(GtmContact(id="gc_1", workspace_id=wid, email="a@example.com", stage_changed_at=now, created_at=now))
        s.flush()
        s.add(GtmTouch(id="gt_1", workspace_id=wid, venture_id=vid, contact_id="gc_1", direction="in",
                       simulated=0, created_at=now - timedelta(minutes=5)))
        s.commit()
    idea_page.save(vid, {"summary": "Care app.", "problem": "p", "offer": "o"})


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_each_event_type_posts_one_nudge_with_an_action_and_a_notice(env, monkeypatch, idea):
    _, vid, clock = env
    from app.chat import proactive
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.commit()
    monkeypatch.setattr(proactive, "DAILY_NUDGES", 99)
    _seed_events(vid, NOON)
    assert proactive.sweep(vid) >= 0  # first sight of the Idea page is remembered, not news
    from app.ventures import idea_page
    idea_page.save(vid, {"summary": idea, "problem": "p", "offer": "o"})
    proactive.sweep(vid)
    posted = _messages(vid, proactive.NUDGE)
    keys = {"Research on your idea is done", "I could not finish this step for you", "Building your landing page has been running",
            "I found 2 people", "1 outreach email is ready", "An outreach email was held back",
            "Someone replied", "Your Idea page changed"}
    for start in keys:
        hits = [(b, a) for b, a in posted if start in b]
        assert len(hits) == 1, (start, [b[:40] for b, _ in posted])
        assert hits[0][1], start  # every nudge has a one-tap action
        if start == "I could not finish this step for you":
            assert "your saved work is kept, and nothing went public" in hits[0][0]
            assert "Open your idea to continue" in hits[0][0] and "handler_failed" not in hits[0][0]
    assert len(_notices(vid)) == len(keys)
    proactive.sweep(vid)  # deduped per event
    assert len(_messages(vid, proactive.NUDGE)) == len(posted) and len(_notices(vid)) == len(keys)


def test_producer_message_is_not_repeated_but_notice_still_raised(env):
    _, vid, _ = env
    from app.chat import proactive
    from app.chat.milestones import post_milestone

    _job(vid, "research.deep", "succeeded", created=NOON - timedelta(hours=1), finished=NOON - timedelta(minutes=3))
    post_milestone(vid, key="research_done:x", kind="research_ready", body_md="Research is ready.")
    assert proactive.sweep(vid) == 1
    assert _messages(vid, proactive.NUDGE) == [] and _notices(vid) == ["Your research is ready"]


def test_daily_cap_and_quiet_hours(env):
    _, vid, clock = env
    from app.chat import proactive

    clock["now"] = NOON.replace(hour=23)
    _seed_events(vid, clock["now"])
    assert proactive.sweep(vid) == 0 and _messages(vid, proactive.NUDGE) == []  # quiet hours: held
    clock["now"] = NOON.replace(hour=23) + timedelta(hours=9)  # 08:00 next day
    proactive.sweep(vid)
    assert len(_messages(vid, proactive.NUDGE)) == proactive.DAILY_NUDGES  # held nudges arrive, capped
    proactive.sweep(vid)
    assert len(_messages(vid, proactive.NUDGE)) == proactive.DAILY_NUDGES


def test_quiet_hours_follow_the_founder_timezone(env):
    _, vid, _ = env
    from app.chat import proactive
    from app.db.models import Person, Venture, Workspace
    from app.state import db

    with db()() as s:
        owner = s.get(Person, s.get(Workspace, s.get(Venture, vid).workspace_id).owner_id)
        owner.timezone = "Asia/Kolkata"  # 12:00 UTC = 17:30 IST; 18:00 UTC = 23:30 IST
        s.commit()
    assert not proactive.quiet(vid, NOON)
    assert proactive.quiet(vid, NOON.replace(hour=18))


# ---- stale work ----

def test_stopped_research_over_an_hour_is_surfaced_with_retry_and_skip(env):
    _, vid, _ = env
    from app.chat import proactive

    _job(vid, "research.deep", "cancelled", created=NOON - timedelta(hours=3), finished=NOON - timedelta(hours=2))
    proactive.sweep(vid)
    body, actions = _messages(vid, proactive.NUDGE)[0]
    assert "Research stopped 2 hours ago" in body and "skip" in body
    assert [a.get("action") for a in actions] == ["job.retry", "chat.reply"]
    assert actions[1]["payload"]["text"] == "Skip research"


def test_recently_stopped_or_answered_research_is_not_nagged(env):
    _, vid, _ = env
    from app.chat import proactive

    _job(vid, "research.deep", "cancelled", created=NOON - timedelta(hours=3), finished=NOON - timedelta(hours=2))
    _user_said(vid, NOON - timedelta(minutes=30))  # the founder acted since
    assert proactive.sweep(vid) == 0


# ---- Charlie leads every reply ----

def test_prompt_contract_has_the_next_step_rule_and_chip():
    load_company_os_app()
    from app.chat.assistant import SYSTEM_PROMPT, next_step_chip, split_next_step

    assert "ONE concrete next step" in SYSTEM_PROMPT and '"let me know"' in SYSTEM_PROMPT and "NEXT:" in SYSTEM_PROMPT
    text, label = split_next_step("Good ICP.\nWant me to find 10 adult children of aging parents this week?\n"
                                  "NEXT: Find 10 adult children of aging parents")
    assert text.endswith("this week?") and "NEXT" not in text
    assert next_step_chip(label) == {"label": "Find 10 adult children of aging parents", "action": "chat.reply",
                                     "payload": {"text": "Find 10 adult children of aging parents"}, "style": "secondary"}
    assert split_next_step("Hi there.") == ("Hi there.", None)


@pytest.mark.parametrize("raw", ["suggest_next_step", "tool.suggest_next_step", "open_connections"])
def test_internal_ids_never_become_next_step_buttons(raw):
    from app.chat.assistant import next_step_chip, split_next_step

    body, label = split_next_step(f"Search is running for the clinics.\nNEXT: {raw}")
    assert body == "Search is running for the clinics."
    chip = next_step_chip(label)
    assert chip is None or "_" not in chip["label"] and "." not in chip["label"]


def test_plain_next_step_label_is_kept():
    from app.chat.assistant import split_next_step

    assert split_next_step("Done.\nNEXT: Draft outreach emails for the parents list")[1] == "Draft outreach emails for the parents list"
