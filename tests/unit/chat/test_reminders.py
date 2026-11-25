"""Reminders: parsing in the founder's timezone, one chat post + one Mac notice, list/cancel/snooze, no short-reminder email."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)  # Friday 4:30 PM in IST
IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.parametrize("rest,zone,due_local,phrase", [
    (" in 10 minutes to call Ravi", "Asia/Kolkata", "2026-10-23 16:40", "to call Ravi"),
    (" in an hour", "Asia/Kolkata", "2026-10-23 17:30", ""),
    (" tomorrow at 9 to check replies", "Asia/Kolkata", "2026-10-24 09:00", "to check replies"),
    (" on Friday about pricing", "Asia/Kolkata", "2026-10-30 09:00", "about pricing"),
    (" on Monday at 3pm to send the deck", "Asia/Kolkata", "2026-10-26 15:00", "to send the deck"),
    (" on 2 Nov at 10:30am to renew the domain", "Asia/Kolkata", "2026-11-02 10:30", "to renew the domain"),
    (" at 5pm to stretch", "Asia/Kolkata", "2026-10-23 17:00", "to stretch"),
    (" at 4pm to stretch", "Asia/Kolkata", "2026-10-24 16:00", "to stretch"),
    (" to call 5 people tomorrow morning", "Asia/Kolkata", "2026-10-24 09:00", "to call 5 people"),
    (" tomorrow at 9 to check replies", "America/New_York", "2026-10-24 09:00", "to check replies"),
])
def test_rules_parse_in_founder_timezone(rest, zone, due_local, phrase):
    load_company_os_app()
    from app.chat.reminders import parse_when

    due, said = parse_when(rest, NOW, ZoneInfo(zone))
    assert due.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M") == due_local
    assert due.tzinfo is not None and said == phrase


@pytest.mark.parametrize("text,action", [
    ("remind me in 10 minutes to call Ravi", "set"), ("Ping me on Friday about pricing", "set"),
    ("Charlie, can you remind me tomorrow at 9 to check replies?", "set"),
    ("what reminders do I have", "list"), ("show my reminders", "list"),
    ("cancel the Friday reminder", "cancel"), ("snooze 10 min", "snooze"),
])
def test_commands_are_recognised(text, action):
    load_company_os_app()
    from app.chat.reminders import command

    assert command(text)[0] == action


@pytest.mark.parametrize("text", ["Remember: our pricing is $20", "what is my pricing?", "how do reminders work in Slack?",
                                  "cancel the research", "remind the team to ship"])
def test_other_messages_are_not_reminders(text):
    load_company_os_app()
    from app.chat.reminders import command

    assert command(text) is None


@pytest.mark.parametrize("recall,future", [
    ("Remind me of the supplier audit worksheet you wrote earlier. Keep the original, not a replacement.",
     "Remind me to review the supplier audit worksheet you wrote earlier"),
    ("Could you remind me what was in the camping packing checklist you drafted last time? Do not rewrite it.",
     "Remind me about packing for our camping trip"),
])
def test_past_document_recall_is_not_a_future_reminder(recall, future):
    load_company_os_app()
    from app.chat.reminders import command

    assert command(recall) is None
    assert command(future)[0] == "set"
    assert command("Remind me tomorrow to review the document you wrote earlier")[0] == "set"


@pytest.fixture
def app(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/reminders.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state
    from core.clock import set_clock

    state.reset_db_factory()
    state.MEM.clear()
    clock = {"now": NOW}
    set_clock(lambda: clock["now"])
    with TestClient(main.create_app()) as client:
        from app.core.local_mode import is_local
        if is_local():
            from core.local_security import OWNER_COOKIE, _proxy_secret
            client.cookies.set(OWNER_COOKIE, _proxy_secret().decode("ascii"))
        vid = client.post("/ventures", json={"raw_idea": getattr(request, "param", "help synthesizer repair shops track parts"), "start": False}).json()["id"]
        yield client, vid, clock
    set_clock(None)


def _say(client, vid, text):
    r = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def _due_jobs(at):
    from app.core.jobs import lease
    from app.state import db
    with db()() as session:
        jobs = lease(session, "test-worker", kinds=["chat.reminder"], now=at)
        session.commit()
    return jobs


@pytest.mark.parametrize("what", ["check the gym search", "check the pottery class search", "review supplier quotes"])
def test_short_reminder_reply_is_saved_promptly_without_waking_redis(app, monkeypatch, what):
    import asyncio
    import time
    from unittest.mock import AsyncMock, Mock

    import redis
    from app.chat import llm_router, proactive, reminders, store
    from app.core import jobs
    from app.credits import metered
    from app.db.models import ChatMessage, Job
    from app.state import db
    from app.ventures import state_brief
    from core.clock import as_utc
    from sqlalchemy import select

    client, vid, clock = app
    forbidden = Mock(side_effect=AssertionError("reminder reply must not wait or run unrelated work"))
    model = AsyncMock(side_effect=AssertionError("a relative reminder needs no model call"))
    with monkeypatch.context() as patch:
        patch.setenv("REDIS_URL", "redis://unavailable.invalid:6379")
        patch.setattr(redis.Redis, "from_url", forbidden)
        patch.setattr(jobs, "publish_wake", forbidden)
        patch.setattr(time, "sleep", forbidden)
        patch.setattr(asyncio, "sleep", model)
        patch.setattr(metered, "llm", model)
        patch.setattr(llm_router, "route", model)
        patch.setattr(reminders, "_model_when", model)
        patch.setattr(reminders, "deliver", forbidden)
        patch.setattr(state_brief, "get_venture_state_brief", forbidden)
        patch.setattr(proactive, "on_chat_open", forbidden)
        patch.setattr(proactive, "post_brief", forbidden)
        patch.setattr(store, "_schedule_diagram", forbidden)
        started = time.perf_counter()
        reply = _say(client, vid, f"remind me in 2 minutes to {what}")
        assert time.perf_counter() - started < 3

    forbidden.assert_not_called()
    model.assert_not_called()
    assert reply["kind"] == "reminder" and f"to {what}." in reply["body_md"]
    with db()() as session:
        job = session.scalar(select(Job).where(Job.venture_id == vid, Job.kind == reminders.KIND))
        confirmation = session.get(ChatMessage, reply["id"])
        due = as_utc(job.run_after)
        assert due == NOW + timedelta(minutes=2)
        assert as_utc(confirmation.created_at) == clock["now"] < NOW + timedelta(seconds=3) < due
    assert _due_jobs(clock["now"]) == []
    assert len(_due_jobs(due)) == 1


@pytest.mark.parametrize("app", ["Supplier audit tracking for manufacturers", "Campground booking plans for families"], indirect=True)
def test_due_reminder_posts_once_and_notifies_mac_once_without_email(app, monkeypatch):
    client, vid, clock = app
    from app.chat import llm_router, reminders
    from app.core.jobs import complete_job
    from app.db.models import ChatMessage
    from app.db.supymem_models import EmailMessage, Notice
    from app.state import db
    from sqlalchemy import func, select

    async def forbidden(*_a, **_k):
        raise AssertionError("reminder commands never need the model router")
    monkeypatch.setattr(llm_router, "route", forbidden)
    reply = _say(client, vid, "remind me in 10 minutes to check replies")
    assert reply["body_md"].endswith("I'll remind you at 4:40 PM to check replies.")
    assert _due_jobs(NOW + timedelta(minutes=5)) == []
    clock["now"] = NOW + timedelta(minutes=11)
    [job] = _due_jobs(clock["now"])
    for _ in range(2):  # a retried job never repeats its effects
        reminders.deliver({**job.payload, "_job_id": job.id})
    with db()() as session:
        complete_job(session, job.id)
        session.commit()
        posted = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "reminder",
                                                           ChatMessage.body_md.contains("Reminder: check replies"))).all()
        assert len(posted) == 1
        assert {a["label"] for a in json.loads(posted[0].actions_json)} == {"Show customers and drafts", "Snooze 10 min"}
        assert session.scalar(select(func.count()).select_from(Notice).where(Notice.kind == f"reminder:{job.id}")) == 1
        assert session.scalar(select(func.count()).select_from(EmailMessage)) == 0
    from app import desktop_notices
    shown = [n for n in desktop_notices.pending() if n["kind"].startswith("reminder:")]
    assert len(shown) == 1 and shown[0]["title"].endswith("Reminder") and shown[0]["body"] == "check replies"

    snoozed = _say(client, vid, "snooze 10 min")
    assert "I'll remind you again at 4:51 PM: check replies" in snoozed["body_md"]
    assert "4:51 PM: check replies" in _say(client, vid, "what reminders do I have")["body_md"]


def test_list_and_cancel_by_weekday(app):
    client, vid, _ = app
    assert "no reminders" in _say(client, vid, "what reminders do I have")["body_md"]
    assert "9:00 AM on Fri 30 Oct about pricing" in _say(client, vid, "ping me on Friday about pricing")["body_md"]
    _say(client, vid, "remind me tomorrow at 9 to check replies")
    listed = _say(client, vid, "what reminders do I have")["body_md"]
    assert listed.index("check replies") < listed.index("pricing")
    assert "pricing" in _say(client, vid, "cancel the Friday reminder")["body_md"]
    listed = _say(client, vid, "what reminders do I have")["body_md"]
    assert "pricing" not in listed and "check replies" in listed
    assert "Which one?" in _say(client, vid, "cancel the Tuesday reminder")["body_md"]


def test_odd_phrasing_uses_one_validated_flash_call(app, monkeypatch):
    client, vid, _ = app
    from types import SimpleNamespace
    from app.credits import metered

    calls = []

    async def fake(messages, **kwargs):
        calls.append(kwargs)
        content = json.dumps({"due_local": "2026-10-30T18:00", "what": "call Ravi"}) if len(calls) == 1 else "not json"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(metered, "llm", fake)
    assert "6:00 PM on Fri 30 Oct: call Ravi" in _say(client, vid, "remind me a week from now in the evening-ish to call Ravi")["body_md"]
    assert calls[0]["tier"] == "flash"
    assert "When should I remind you?" in _say(client, vid, "remind me whenever the moon is full")["body_md"]


def test_email_only_for_long_reminders_when_founder_is_away(app, monkeypatch):
    _, vid, clock = app
    from app.chat import reminders
    from app.email import service

    sent = []
    monkeypatch.setattr(service, "send_email", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(service, "last_active", lambda *_a, **_k: None)
    short = {"venture_id": vid, "text": "call Ravi", "set_at": NOW.isoformat(), "due_at": (NOW + timedelta(hours=3)).isoformat()}
    reminders.deliver({**short, "_job_id": "job_short"})
    assert sent == []
    long = {**short, "due_at": (NOW + timedelta(days=3)).isoformat()}
    reminders.deliver({**long, "_job_id": "job_long"})
    assert len(sent) == 1 and sent[0][0][1] == "reminder" and sent[0][1]["ref"] == "job_long"
    monkeypatch.setattr(service, "last_active", lambda *_a, **_k: clock["now"] - timedelta(minutes=5))
    reminders.deliver({**long, "_job_id": "job_long_active"})
    assert len(sent) == 1


def test_charlie_context_has_founder_local_time(app):
    _, vid, _ = app
    from app.db.models import Person, Venture, Workspace
    from app.state import db
    from app.tasks.time_context import founder_timezone
    from core.time_context import time_context, timezone_context

    with db()() as session:
        owner = session.get(Workspace, session.get(Venture, vid).workspace_id).owner_id
        session.get(Person, owner).timezone = None
        session.commit()
    assert founder_timezone(owner) is None  # never guessed: Charlie says the zone is unconfirmed (UTC)
    with db()() as session:
        session.get(Person, owner).timezone = "Asia/Kolkata"
        session.commit()
    with timezone_context(founder_timezone(owner)):
        assert "2026-10-23T16:30:00+05:30 (Friday" in time_context() and "founder timezone: Asia/Kolkata" in time_context()
    with db()() as session:
        session.get(Person, owner).timezone = "America/New_York"
        session.commit()
    with timezone_context(founder_timezone(owner)):
        assert "2026-10-23T07:00:00-04:00" in time_context()
