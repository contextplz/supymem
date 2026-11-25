"""Source facts on the migrated PostgreSQL schema, with real write concurrency."""
from datetime import timedelta
import os

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from tests.integration.test_postgres_auth_security import concurrent, pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from core.env_contract import coerce_bool

pytestmark = pytest.mark.skipif(
    not coerce_bool(os.environ.get("COMPANY_OS_MEMORY_FACTS_ENABLED"), default=False),
    reason="Fact schema and PostgreSQL isolation are unqualified and default-off",
)


def source(d, message):
    return {"type": "chat", "ref": "chat:" + message["id"], "quote": message["body_md"]}


def save(d, value, *, subject="idea.audience"):
    from app.memory.facts import remember_fact
    from app.tenancy import bind
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), d["factory"]() as session:
        row = remember_fact(session, venture_id=d["vid"], subject=subject, predicate="is",
                            value=value["quote"], source=value)
        identity = row.id
        session.commit()
        return identity


def test_migration_has_current_index_and_retained_source_columns(journey_db):
    schema = inspect(journey_db["engine"])
    assert {"person_id", "source_json", "source_digest", "valid_from", "valid_to", "recorded_at",
            "superseded_at", "forgotten_at"} <= {row["name"] for row in schema.get_columns("venture_facts")}
    indexes = {row["name"]: row for row in schema.get_indexes("venture_facts")}
    assert indexes["uq_venture_fact_current"]["unique"] is True
    where = str(indexes["uq_venture_fact_current"]["dialect_options"]["postgresql_where"])
    assert "valid_to IS NULL" in where and "forgotten_at IS NULL" in where


def test_same_source_concurrent_capture_is_idempotent_then_correction_keeps_history(journey_db):
    from app.chat.store import append_message
    from app.memory.facts import query_facts
    from app.tenancy import bind
    d = journey_db
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        first = source(d, append_message(d["vid"], role="user", body_md="Our audience is teachers."))
    identities = concurrent(lambda: save(d, first))
    assert len(set(identities)) == 1
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        second = source(d, append_message(d["vid"], role="user", body_md="Our audience is nurses."))
    latest = save(d, second)
    assert save(d, first) == identities[0]
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), d["factory"]() as session:
        assert [row.id for row in query_facts(session, venture_id=d["vid"], predicate="is")] == [latest]
        history = query_facts(session, venture_id=d["vid"], predicate="is", include_history=True)
        assert {row.id for row in history} == {latest, identities[0]}
        previous = next(row for row in history if row.id == identities[0])
        assert previous.valid_to and previous.superseded_at


def test_delayed_source_and_foreign_actor_cannot_restore_or_read_old_fact(journey_db):
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.memory.facts import query_facts
    from app.tenancy import bind
    d = journey_db
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        old = source(d, append_message(d["vid"], role="user", body_md="Our audience is teachers."))
        newer = source(d, append_message(d["vid"], role="user", body_md="Our audience is nurses."))
        with d["factory"]() as session:
            session.get(ChatMessage, old["ref"].split(":", 1)[1]).created_at -= timedelta(days=1)
            session.commit()
        current = save(d, newer)
        retired = save(d, old)
        with d["factory"]() as session:
            assert [row.id for row in query_facts(session, venture_id=d["vid"], predicate="is")] == [current]
            assert len(query_facts(session, venture_id=d["vid"], predicate="is", include_history=True)) == 2
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor="usr_pg_foreign"), d["factory"]() as session:
        assert query_facts(session, venture_id=d["vid"], predicate="is", include_history=True) == []
        with pytest.raises(ValueError, match="memory_fact_founder_source_invalid"):
            save({**d, "actor": "usr_pg_foreign"}, old)
    assert current != retired


def test_source_is_database_immutable_but_scoped_forget_keeps_history(journey_db):
    from app.chat.store import append_message
    from app.db.fact_models import VentureFact
    from app.memory.facts import forget_fact, query_facts
    from app.tenancy import bind
    d = journey_db
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        original = source(d, append_message(d["vid"], role="user", body_md="Our audience is teachers."))
        identity = save(d, original)
        with d["factory"]() as session:
            with pytest.raises(DBAPIError, match="venture_fact_immutable"):
                session.execute(text("UPDATE venture_facts SET value='unproven model prose' WHERE id=:id"), {"id": identity})
            session.rollback()
            forget_fact(session, venture_id=d["vid"], fact_id=identity)
            session.commit()
            assert query_facts(session, venture_id=d["vid"], predicate="is", include_history=True) == []
            retained = session.get(VentureFact, identity)
            assert retained and retained.forgotten_at and retained.value == original["quote"]
        assert save(d, original) == identity
