"""Chat says so when a managed build passes QA and when the founder accepts it."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from tests.unit.test_local_mode import _owner_client, local_owner_app  # noqa: F401 (fixture)


@pytest.fixture
def env(local_owner_app):
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        made = client.post("/ventures", json={"raw_idea": "a lobby lock waitlist for apartment buildings"})
        assert made.status_code == 201, made.text
        yield client, made.json()["id"], made.json()["workspace_id"]


@pytest.mark.parametrize("idea", ["Inspection records for factory managers", "Shared recipes for home cooks"])
def test_failed_attempt_notices_are_deduped_per_day_and_proactive_chat_respects_total_cap(env, monkeypatch, idea):
    from datetime import datetime, timedelta, timezone
    from app.chat import milestones
    from app.chat.store import append_message
    from app.db.models import ChatMessage, Venture
    from app.state import db
    _, vid, _ = env
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(milestones, "utcnow", lambda: now)
    monkeypatch.setattr("app.chat.store.utcnow", lambda: now)
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.commit()
        milestones.post_milestone(vid, key="failure-1", kind="build_failed", body_md="The checks stopped.", session=session)
        assert not milestones.post_milestone(vid, key="failure-2", kind="build_failed", body_md="The checks stopped.", session=session)
        assert milestones.post_milestone(vid, key="failure-3", kind="build_failed", body_md="The source could not be saved.", session=session)
        session.commit()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        count = session.scalar(select(func.count()).select_from(ChatMessage).where(
            ChatMessage.venture_id == vid, ChatMessage.role == "assistant", ChatMessage.created_at >= day,
            ChatMessage.created_at < day + timedelta(days=1)))
    for _index in range(milestones.DAILY_MESSAGE_LIMIT - count - 1):
        append_message(vid, role="assistant", kind="text", body_md="A requested reply.")
    assert milestones.post_milestone(vid, key="last-notice", kind="autopilot_note", body_md="Review the saved result.")
    assert not milestones.post_milestone(vid, key="over-cap", kind="build_failed", body_md="Another attempt stopped.")
    assert milestones.post_milestone(vid, key="search-results", kind="text", body_md="People you can reach.", requested=True)
    assert milestones.post_milestone(vid, key="search-results-2", kind="text", body_md="More people.", requested=True)
    with db()() as session:
        assert len(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.role == "assistant", ChatMessage.created_at >= now.replace(hour=0))).all()) == 16
    now += timedelta(days=1)
    assert milestones.post_milestone(vid, key="tomorrow", kind="build_failed", body_md="The checks stopped.")
    for _index in range(milestones.DAILY_MESSAGE_LIMIT - 1):
        append_message(vid, role="assistant", kind="text", body_md="A requested reply.")
    assert milestones.bump_milestone(vid, key="last-notice", body_md="The saved review is still waiting.")
    with db()() as session:
        assert len(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.role == "assistant", ChatMessage.created_at >= now.replace(hour=0))).all()) == 14


@pytest.mark.parametrize("idea", ["Inspection records for factory managers", "Shared recipes for home cooks"])
def test_pending_notices_obey_idempotency_and_caps_without_committing(env, monkeypatch, idea):
    from app.chat import milestones
    from app.db.models import ChatMessage
    from app.state import db

    _, venture_id, _ = env
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    with db()() as session:
        before = set(session.scalars(select(ChatMessage.id)))
        for notice in range(milestones.DAILY_MESSAGE_LIMIT):
            assert milestones.post_milestone(venture_id, key=f"notice-{notice}", kind="autopilot_note", body_md=idea,
                session=session)
        assert not milestones.post_milestone(venture_id, key="notice-0", kind="autopilot_note", body_md=idea,
            session=session, requested=True)
        assert not milestones.post_milestone(venture_id, key="over-cap", kind="autopilot_note", body_md=idea,
            session=session)
        session.rollback()
    with db()() as session:
        assert set(session.scalars(select(ChatMessage.id))) == before


def test_autopilot_unconfirmed_notice_is_once_per_reason_per_day(env, monkeypatch):
    from app.autopilot import service
    from app.db.models import ChatMessage
    from app.state import db
    _, vid, _ = env
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    for _attempt in range(20):
        service._tell(vid, "The outcome requires review. Open the saved result before retrying.")
    with db()() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.kind == "autopilot_note")).all()
        assert len(messages) == 1
        assert json.loads(messages[0].actions_json)[0]["action"] == "status"  # chat-first: Right now is shown in chat


def test_build_ready_then_accepted_post_once_with_next_buttons(env):  # noqa: F811
    _, vid, _ = env
    from app.chat.milestone_hooks import build_accepted, build_ready
    from app.db.models import ChatMessage
    from app.state import db

    run = SimpleNamespace(id="run_1", venture_id=vid, app_id="app_1", spec='{"artifact_profile":"static_landing_v1"}')
    qa = SimpleNamespace(passed=31, failed=0, unsupported=0)
    with db()() as s:
        build_ready(s, run, qa)
        build_accepted(s, run)
        s.commit()
    with db()() as s:
        build_ready(s, run, qa)  # a retried finalization does not repeat the post
        s.commit()
    with db()() as s:
        rows = {m.kind: m for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid))}
        assert sum(m.kind == "build_ready" for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid))) == 1
    ready = rows["build_ready"]
    assert "QA passed 31/31" in ready.body_md
    acts = json.loads(ready.actions_json)
    assert [a.get("action") for a in acts] == ["site.preview", "build.accept"]
    assert acts[1]["action"] == "build.accept" and not any(a.get("href") for a in acts)
    accepted = rows["build_accepted"]
    # waitlist is off: offer to turn it on (then test, then publish), never Publish first
    assert "turn on the waitlist" in accepted.body_md
    first = json.loads(accepted.actions_json)[0]
    assert first["confirmation"]["action"] == "waitlist.enable" and first.get("label") == "Turn on waitlist"


def _accepted_actions(vid, monkeypatch, state):
    from app.chat import site_actions
    from app.chat.milestone_hooks import build_accepted
    from app.db.models import ChatMessage
    from app.state import db

    monkeypatch.setattr(site_actions, "_waitlist_state", lambda v: state)
    run = SimpleNamespace(id="run_w", venture_id=vid, app_id="app_1", spec="{}")
    with db()() as s:
        build_accepted(s, run)
        s.commit()
    with db()() as s:
        row = s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "build_accepted")).one()
    return row.body_md, json.loads(row.actions_json)


def test_accepted_offers_test_when_waitlist_untested(env, monkeypatch):  # noqa: F811
    _, vid, _ = env
    body, acts = _accepted_actions(vid, monkeypatch, {"ok": True, "enabled": True, "submission_verified": False})
    assert "test the waitlist" in body and acts[0]["action"] == "waitlist.test"


def test_accepted_offers_publish_when_waitlist_ready(env, monkeypatch):  # noqa: F811
    _, vid, _ = env
    body, acts = _accepted_actions(vid, monkeypatch, {"ok": True, "enabled": True, "submission_verified": True})
    assert "Next: publish" in body and acts[0]["confirmation"]["action"] == "site.publish"


def test_revision_build_ready_and_failed_build_both_post_once(env):  # noqa: F811
    _, vid, _ = env
    from app.chat.milestone_hooks import build_ended, build_ready
    from app.db.models import ChatMessage
    from app.state import db

    ok = SimpleNamespace(id="run_rev", venture_id=vid, app_id="app_1", spec="{}")
    bad = SimpleNamespace(id="run_bad", venture_id=vid, app_id="app_1", spec="{}", trigger="manual")
    with db()() as s:
        build_ready(s, ok, SimpleNamespace(passed=3, failed=0, unsupported=0), revision=True)
        build_ended(s, bad)
        s.commit()
    with db()() as s:
        build_ended(s, bad)  # a retried finalization does not repeat the post
        s.commit()
    with db()() as s:
        rows = list(s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid)))
    ready = [m for m in rows if m.kind == "build_ready"]
    failed = [m for m in rows if m.kind == "build_failed"]
    assert len(ready) == 1 and "revised landing page build is ready" in ready[0].body_md
    assert len(failed) == 1 and "before it passed QA" in failed[0].body_md


@pytest.mark.parametrize("idea,live_url", [
    ("Inventory reconciliation for distribution teams", "https://inventory.example/"),
    ("Weekend walking routes for local families", "https://walking.example/"),
])
def test_failed_landing_qa_keeps_live_page_and_way_forward_in_chat(env, monkeypatch, idea, live_url):
    _, vid, _ = env
    from app.chat.milestone_hooks import build_ended
    from app.db.models import ChatMessage, Venture
    from app.state import db
    monkeypatch.setattr("app.planning.publication_evidence.landing_build_result", lambda *_: {
        "available": True, "live_url": live_url, "qa_note": "The latest QA attempt failed; the live page is still available."})
    run = SimpleNamespace(id="qa_failed_live", venture_id=vid, app_id="app_1", spec="{}")
    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        build_ended(session, run)
        session.commit()
    with db()() as session:
        message = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "build_ready"))
        assert "nothing to review" not in message.body_md
        actions = json.loads(message.actions_json)
        assert [action["label"] for action in actions] == ["Visit landing page", "Build my landing page", "Show customers and drafts"]
        assert actions[0]["href"] == live_url
        assert actions[1]["action"] == "pagebuilder.build" and actions[1]["payload"] == {"prepare": True}
