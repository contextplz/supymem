"""Real loopback PostgreSQL acceptance, including pgvector-absent containers."""
import asyncio
import json

import pytest
from sqlalchemy import inspect, select, text

from tests.integration.test_postgres_wallet_bridge_rollout import bridge_db as bridge_db


@pytest.fixture
def search_db(bridge_db, monkeypatch):
    from app.config import settings
    from app.db.models import ChatConversation, ChatMessage, CreditPolicy, Person, Venture, Workspace
    from app.db.fact_models import VentureFact
    from app.credits.policy import invalidate
    from app.memory import facts, profile
    from app.tenancy import bind
    from core.clock import utcnow
    d = bridge_db
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", True)
    now = utcnow()
    founders = [dict(workspace_id="ws_freight", venture_id="v_freight", person_id="per_freight",
        statement="Our audience is freight dispatch teams."),
        dict(workspace_id="ws_garden", venture_id="v_garden", person_id="per_garden",
        statement="Our audience is balcony gardeners.")]
    with d["factory"]() as session:
        policy = session.get(CreditPolicy, "feature_flags")
        if policy is None:
            session.add(CreditPolicy(key="feature_flags", value_json='{"memory_search_v2":true}', updated_at=now))
        else:
            policy.value_json = '{"memory_search_v2":true}'
        for founder in founders:
            session.add(Person(id=founder["person_id"], display_name="Synthetic private founder", created_at=now))
        session.flush()
        for founder in founders:
            session.add(Workspace(id=founder["workspace_id"], owner_id=founder["person_id"], kind="company",
                name="Synthetic scoped workspace", entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        for founder in founders:
            session.add(Venture(id=founder["venture_id"], workspace_id=founder["workspace_id"], name="Synthetic venture",
                raw_idea=founder["statement"], stage="draft", created_at=now, updated_at=now))
        session.flush()
        for founder in founders:
            session.add(ChatConversation(id="c_" + founder["venture_id"], workspace_id=founder["workspace_id"],
                venture_id=founder["venture_id"], creator_person_id=founder["person_id"], visibility="private",
                title="Private source", creation_key="c_" + founder["venture_id"], created_at=now, updated_at=now))
        session.flush()
        for founder in founders:
            session.add(ChatMessage(id="m_" + founder["venture_id"], workspace_id=founder["workspace_id"],
                venture_id=founder["venture_id"], author_person_id=founder["person_id"], role="user", kind="text",
                body_md=founder["statement"], conversation_id="c_" + founder["venture_id"], created_at=now))
        session.commit()
    invalidate()
    for founder in founders:
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d["factory"]() as session:
            row = facts.remember_fact(session, venture_id=founder["venture_id"], subject="idea.audience", predicate="is",
                value=founder["statement"], source={"type": "chat", "ref": "chat:m_" + founder["venture_id"], "quote": founder["statement"]})
            founder["fact_id"] = row.id
            session.commit()
    return d, founders, profile, bind


def test_postgres_fulltext_profile_refresh_and_withdrawal_are_private(search_db, monkeypatch):
    from app.memory.context import context_for
    import app.memory
    from types import SimpleNamespace
    d, founders, profile, bind = search_db
    monkeypatch.setattr(app.memory, "get_memory", lambda: SimpleNamespace(name="postgres"))
    with d["factory"]() as session:
        assert session.scalar(text("SELECT count(*) FROM memory_search_jobs")) == 2
        assert session.scalar(text("SELECT count(*) FROM memory_embedding_cache")) == 0
        available = profile.vector_available(session)
        if available:
            assert session.scalar(text("SELECT count(*) FROM venture_facts WHERE embedding IS NOT NULL")) == 0
        assert "search_document" in {column["name"] for column in inspect(session.get_bind()).get_columns("venture_facts")}
    for founder in founders:
        scope = {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}
        job = {**scope, "request_hash": "", "query_text": "", "generation": 1, "attempts": 0}
        with bind(workspace_id=scope["workspace_id"], venture_id=scope["venture_id"], actor=scope["person_id"]), d["factory"]() as session:
            keyword = "dispatch" if founder["person_id"] == "per_freight" else "gardeners"
            rows = profile.search_rows(session, venture_id=scope["venture_id"], query=keyword, limit=5)
            assert [row["id"] for row in rows] == [founder["fact_id"]]
            profile._prepare(job)
            with monkeypatch.context() as patch:
                patch.setattr(profile, "search_rows", lambda *args, **kwargs: pytest.fail("profile answers without search"))
                context = context_for(session, workspace_id=scope["workspace_id"], venture_id=scope["venture_id"],
                    purpose="venture.chat.assistant", query="who is my audience?")
                assert [row["statement"] for row in context["facts"]] == [founder["statement"]]
            session.execute(text("UPDATE chat_messages SET body_md='Withdrawn' WHERE id=:id"), {"id": "m_" + scope["venture_id"]})
            session.commit()
            assert session.scalar(text("SELECT generation>refreshed_generation FROM memory_core_profiles "
                "WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id"), scope)
            context = context_for(session, workspace_id=scope["workspace_id"], venture_id=scope["venture_id"],
                purpose="venture.chat.assistant", query="who is my audience?")
            assert context["facts"] == []
    with d["factory"]() as session:
        assert session.scalar(text("SELECT count(*) FROM memory_embedding_cache")) == 0


def test_postgres_embedding_cache_is_background_and_hash_scoped(search_db, monkeypatch):
    d, founders, profile, bind = search_db
    with d["factory"]() as session:
        if not profile.vector_available(session):
            pytest.skip("pgvector unavailable; full-text fallback is covered separately")
    calls = []
    async def embed(content, *, scope, **kwargs):
        calls.append((scope["venture_id"], content))
        return json.dumps([.01] * 1536)
    monkeypatch.setattr(profile, "embed_content", embed)
    assert asyncio.run(profile.run_one())
    assert asyncio.run(profile.run_one())
    assert {venture for venture, _ in calls} == {founder["venture_id"] for founder in founders}
    with d["factory"]() as session:
        assert session.scalar(text("SELECT count(*) FROM venture_facts WHERE embedding IS NOT NULL")) == 2
        assert session.scalar(text("SELECT count(*) FROM memory_embedding_cache")) == 2
    assert not asyncio.run(profile.run_one())
