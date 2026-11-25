"""Manual recovery respects no-search constraints and keeps founder work read-only."""
import asyncio

from tests.unit.chat.test_round3_matrix import venture as venture_fixture

venture = venture_fixture


def test_manual_no_search_chat_is_grounded_and_never_queues_or_calls_tools(venture, monkeypatch):
    from app.research import explore
    from app.chat import assistant
    from app.db.models import Job, PlannedTask
    from app.state import db
    from app.ventures import state_brief
    from sqlalchemy import select
    async def forbidden(*_a, **_kw):
        raise AssertionError("manual guide must not call a search or model")
    monkeypatch.setattr(explore, "explore_web", forbidden)
    monkeypatch.setattr(assistant, "_complete", forbidden)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        "idea": {"name": "StudioSlot", "audience": "independent personal trainers"}})
    client, vid, _ = venture
    with db()() as session:
        before = (set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh"))), set(session.scalars(select(PlannedTask.id))))
    text = ("The trainer lookup just failed. Do it another way: give me a manual Google-to-Gmail discovery guide "
            "and a short interview request. Do not call web-search tools, invent trainer names or emails, "
            "or claim outreach happened. Choose one city and give me the exact query, website checks, and Gmail compose steps.")
    reply = client.post(f"/ventures/{vid}/chat", json={"message": text}).json()
    assert "StudioSlot" in reply["body_md"] and "Compose" in reply["body_md"] and "Review draft" in reply["body_md"]
    assert "Bengaluru" in reply["body_md"] and "before making any product claims" in reply["body_md"]
    assert reply["citations"] == [] and "Subject:" in reply["body_md"]
    assert "contacted anyone" in reply["body_md"].lower()
    assert "confirmation" not in str(reply["actions"])
    with db()() as session:
        assert before == (set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh"))), set(session.scalars(select(PlannedTask.id))))


def test_failed_new_research_does_not_claim_live_sources_were_checked(venture, monkeypatch):
    from app.chat import execute, router
    from app.chat.context import assemble
    from app.chat.search_recovery import search_failure_reply
    client, vid, _ = venture
    monkeypatch.setattr(execute, "research_needs_start", lambda _v: False)
    async def unavailable(*_a, **_kw):
        return search_failure_reply("independent personal trainers in Bengaluru", "search_rate_limited")
    monkeypatch.setattr(router, "execute_action", unavailable)
    reply = asyncio.run(router._dispatch_intent(vid, assemble(vid), "research",
        "Research current public sources for independent personal trainers in Bengaluru today", None))
    assert "rate-limited" in reply["body_md"]
    assert "checked live" not in reply["body_md"] and "found source leads" not in reply["body_md"]
