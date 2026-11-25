"""Founder cancellation stops new calls without discarding paid-for sources."""
import json

import pytest
from sqlalchemy import select

from tests.research_fixtures import install_research
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import run_queued
from tests.security.test_research_boundary import confirmed, dossier, job


@pytest.mark.parametrize("stop", [True, False], ids=["stopped", "normal"])
def test_research_stop_retains_first_search_and_suppresses_ready(client, monkeypatch, stop):
    from app.core.jobs import request_cancel
    from app.db.models import ChatMessage
    from app.research import deep, execution as ex
    from app.state import db

    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    provider = deep._web_provider()
    search = provider.search

    async def search_then_stop(query):
        result = await search(query)
        if stop:
            with db()() as session:
                assert request_cancel(session, jid)
                session.commit()
        return result

    provider.search = search_then_stop
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_queued("research.deep", through_job_id=jid)

    result = dossier(client, vid)
    saved = json.loads(ex.read(ex.identifier(jid)).summary)
    with db()() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid)).all()
    ready = [message for message in messages if message.kind == "research_ready"]
    assert result["findings"]["source"]
    assert result["summary"]["stopped_early"] is stop
    assert job(jid).status == ("cancelled" if stop else "succeeded")
    if stop:
        assert [kind for kind, _ in calls] == ["search"]
        assert result["calls_used"] == 1
        assert result["state"] == "partial"
        assert result["summary"]["review"] is None
        assert saved["error_code"] == "research_cancelled"
        assert saved["review_kind"] == "retrieved_sources_only"
        assert len(saved["_runtime"]["operations"]) == 1
        receipt = next(iter(saved["_runtime"]["operations"].values()))
        assert receipt["state"] == "completed" and receipt["usage"]["settled"] is True
        assert not ready and not any("Research is ready" in message.body_md for message in messages)
        stopped = [message for message in messages if "Research stopped as you asked" in message.body_md]
        assert len(stopped) == 1 and stopped[0].kind == "text"
        assert "research_cancelled" not in stopped[0].body_md
        from app.chat.build_path import research_card
        retry = research_card(vid)["actions"][0]
        assert retry["action"] == "research.start"
        assert retry in json.loads(stopped[0].actions_json)
    else:
        assert [kind for kind, _ in calls].count("search") > 1
        assert [kind for kind, _ in calls].count("github") == 1
        assert [kind for kind, _ in calls].count("review") == 1
        assert result["summary"]["review"] is not None
        assert "error_code" not in saved
        assert len(ready) == 1 and "Research is ready" in ready[0].body_md


def test_cancellation_after_finishing_still_suppresses_ready(client, monkeypatch):
    from app.core.jobs import request_cancel
    from app.db.models import ChatMessage
    from app.research import deep
    from app.state import db

    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    finish = deep._finish

    def finish_then_stop(*args):
        finish(*args)
        with db()() as session:
            assert request_cancel(session, jid)
            session.commit()

    monkeypatch.setattr(deep, "_finish", finish_then_stop)
    run_queued("research.deep", through_job_id=jid)
    assert job(jid).status == "cancelled"
    with db()() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid)).all()
    assert not any(message.kind == "research_ready" or "Research is ready" in message.body_md for message in messages)
    assert any("Research stopped as you asked" in message.body_md for message in messages)
