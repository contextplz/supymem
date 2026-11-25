"""Real migrated RLS under a non-bypass role, with deliberately unfiltered queries.

The role and database are disposable loopback fixtures. No production credentials,
roles or rows are used, and provider calls are unnecessary.
"""
from __future__ import annotations

import uuid
import hashlib
import json
import os
from datetime import datetime

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    pg_database as pg_database,
    service_modules,
)
from core.env_contract import coerce_bool

pytestmark = pytest.mark.skipif(
    not coerce_bool(os.environ.get("COMPANY_OS_MEMORY_FACTS_ENABLED"), default=False),
    reason="Draft memory RLS prerequisites are unqualified and default-off",
)

TABLES = (
    "institutional_memories", "memory_working_entries", "chat_messages",
    "chat_conversations", "chat_context_checkpoints", "venture_facts",
)


@pytest.fixture()
def rls(pg_database):
    url, admin = pg_database
    role = "memory_rls_" + uuid.uuid4().hex[:16]
    with service_modules("company_os"):
        from app.db.fact_models import VentureFact
        from app.db.models import (
            ChatContextCheckpoint, ChatConversation, ChatMessage, InstitutionalMemory,
            MemoryWorkingEntry, Person, Venture, Workspace,
        )
        from app.db.session import make_engine, make_session_factory
        from app.tenancy import bind

        now = datetime(2026, 10, 8)
        models = {row.__tablename__: row for row in (
            InstitutionalMemory, MemoryWorkingEntry, ChatMessage,
            ChatConversation, ChatContextCheckpoint, VentureFact,
        )}
        with sessionmaker(bind=admin)() as session:
            session.add_all([Person(id=person, display_name=person, created_at=now)
                             for person in ("rls_person_a", "rls_person_b")])
            session.flush()
            session.add_all([Workspace(id=wid, owner_id="rls_person_a", name=wid, kind="venture", entry_mode="idea",
                                       created_at=now, updated_at=now)
                             for wid in ("rls_workspace_a", "rls_workspace_b")])
            session.flush()
            for label, wid in (("a", "rls_workspace_a"), ("b", "rls_workspace_b"), ("c", "rls_workspace_a")):
                vid, cid = "rls_venture_" + label, "rls_conversation_" + label
                session.add(Venture(id=vid, workspace_id=wid, name=vid, raw_idea="RLS fixture",
                                    stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
                session.flush()
                session.add(ChatConversation(id=cid, workspace_id=wid, venture_id=vid,
                    creator_person_id=None, visibility="legacy_shared", title="RLS fixture",
                    creation_key=cid, created_at=now, updated_at=now))
                session.flush()
                session.add_all([
                    InstitutionalMemory(id="rls_memory_" + label, workspace_id=wid, venture_id=vid,
                                        dedupe_key=label, record_json="{}", version=1),
                    MemoryWorkingEntry(scope_key="venture:" + vid, key="rls_key", workspace_id=wid,
                        venture_id=vid, payload_json="{}", version=1, updated_at=now),
                    ChatMessage(id="rls_message_" + label, workspace_id=wid, venture_id=vid,
                        conversation_id=cid, role="user", body_md="RLS fixture", kind="text", created_at=now),
                    ChatContextCheckpoint(id="rls_checkpoint_" + label, conversation_id=cid, version=1,
                        watermark_id="rls_message_" + label, source_ids_json="[]", source_digest="0" * 64,
                        payload_json="{}", created_at=now),
                ])
                for person in ("rls_person_a", "rls_person_b"):
                    session.add(VentureFact(id=f"rls_fact_{label}_{person}", workspace_id=wid, venture_id=vid,
                        person_id=person, subject="idea.audience", predicate="statement", value="RLS fixture",
                        value_json='"RLS fixture"', source_json="{}", source_digest="0" * 64,
                        dedupe_key=person, confidence=1, valid_from=now, recorded_at=now))
            session.commit()

        with admin.begin() as connection:
            connection.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT"))
            connection.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {role}"))
            connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"))

        def select_role(connection, _record):
            previous = connection.autocommit
            connection.autocommit = True
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute(f"SET ROLE {role}")
                finally:
                    cursor.close()
            finally:
                connection.autocommit = previous

        runtime = make_engine(url)
        event.listen(runtime, "connect", select_role)
        factory = make_session_factory(runtime)
        try:
            yield {"admin": admin, "runtime": runtime, "factory": factory, "bind": bind,
                   "models": models, "role": role, "get_url": lambda: url, "select_role": select_role}
        finally:
            runtime.dispose()
            with admin.begin() as connection:
                # Only this disposable database's synthetic fixtures. The facts
                # trigger deliberately refuses DELETE even for its admin role.
                connection.execute(text("TRUNCATE venture_facts"))
                connection.execute(text("TRUNCATE venture_documents"))
                connection.execute(text("DELETE FROM jobs WHERE id LIKE 'rls_journal_job_%'"))
                for table in ("chat_context_checkpoints", "chat_messages", "chat_conversations",
                              "institutional_memories", "memory_working_entries"):
                    connection.execute(text(f"DELETE FROM {table}"))
                connection.execute(text("DELETE FROM ventures WHERE id LIKE 'rls_venture_%'"))
                connection.execute(text("DELETE FROM workspaces WHERE id LIKE 'rls_workspace_%'"))
                connection.execute(text("DELETE FROM people WHERE id LIKE 'rls_person_%'"))
                connection.execute(text(f"DROP OWNED BY {role}"))
                connection.execute(text(f"DROP ROLE {role}"))


def owned(d, label="a", person="rls_person_a"):
    return d["bind"](workspace_id="rls_workspace_b" if label == "b" else "rls_workspace_a",
                     venture_id="rls_venture_" + label, actor=person)


@pytest.mark.parametrize("table", TABLES)
def test_unfiltered_queries_require_workspace_and_venture_setting(rls, table):
    with rls["factory"]() as session:
        assert session.execute(text(f"SELECT * FROM {table}")).all() == []
        with owned(rls):
            assert len(session.execute(text(f"SELECT * FROM {table}")).all()) == 1
        assert session.execute(text(f"SELECT * FROM {table}")).all() == []
        with owned(rls, "b"):
            assert len(session.execute(text(f"SELECT * FROM {table}")).all()) == 1
        with owned(rls, "c"):
            assert len(session.execute(text(f"SELECT * FROM {table}")).all()) == 1


def test_runtime_role_and_forced_policies_are_real(rls):
    with rls["factory"]() as session:
        role = session.execute(text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
        assert tuple(role) == (rls["role"], False, False)
        policies = session.execute(text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables)"),
                                   {"tables": list(TABLES)}).all()
        assert len(policies) == len(TABLES)
        assert all(enabled and forced for _, enabled, forced in policies)


def test_force_rls_also_applies_to_non_bypass_table_owner(rls):
    with rls["admin"].begin() as connection:
        previous = connection.scalar(text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'institutional_memories'"))
        connection.execute(text(f"ALTER TABLE institutional_memories OWNER TO {rls['role']}"))
    try:
        with rls["factory"]() as session:
            assert session.execute(text("SELECT * FROM institutional_memories")).all() == []
            with owned(rls):
                assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"
    finally:
        quoted_owner = rls["admin"].dialect.identifier_preparer.quote(previous)
        with rls["admin"].begin() as connection:
            connection.execute(text(f"ALTER TABLE institutional_memories OWNER TO {quoted_owner}"))


def test_broken_filters_cannot_read_foreign_workspace_venture_or_person(rls):
    with owned(rls), rls["factory"]() as session:
        for table in TABLES:
            rows = session.execute(text(f"SELECT * FROM {table}")).mappings().all()
            assert len(rows) == 1
            if "venture_id" in rows[0]:
                assert rows[0]["venture_id"] == "rls_venture_a"
        assert session.execute(text("SELECT * FROM institutional_memories WHERE workspace_id = 'rls_workspace_b'")).all() == []
        assert session.execute(text("SELECT * FROM institutional_memories WHERE venture_id = 'rls_venture_c'")).all() == []
        assert session.execute(text("SELECT * FROM venture_facts WHERE person_id = 'rls_person_b'")).all() == []


def test_private_chat_and_checkpoint_policies_follow_the_source_owner(rls):
    from app.db.models import ChatContextCheckpoint, ChatConversation, ChatMessage
    now = datetime(2026, 10, 8)
    with sessionmaker(bind=rls["admin"])() as session:
        for person in ("rls_person_a", "rls_person_b"):
            cid, mid = "rls_private_" + person, "rls_private_message_" + person
            session.add(ChatConversation(id=cid, workspace_id="rls_workspace_a", venture_id="rls_venture_a",
                creator_person_id=person, visibility="private", title="Private RLS fixture",
                creation_key=cid, created_at=now, updated_at=now))
            session.flush()
            session.add_all([
                ChatMessage(id=mid, workspace_id="rls_workspace_a", venture_id="rls_venture_a",
                    conversation_id=cid, author_person_id=person, role="user", body_md="Private RLS fixture", created_at=now),
                ChatContextCheckpoint(id="rls_private_checkpoint_" + person, conversation_id=cid, version=1,
                    watermark_id=mid, source_ids_json="[]", source_digest="0" * 64, payload_json="{}", created_at=now),
            ])
        session.commit()
    for person in ("rls_person_a", "rls_person_b"):
        with owned(rls, person=person), rls["factory"]() as session:
            assert set(session.scalars(text("SELECT id FROM chat_conversations"))) == {"rls_conversation_a", "rls_private_" + person}
            assert set(session.scalars(text("SELECT id FROM chat_messages"))) == {"rls_message_a", "rls_private_message_" + person}
            assert set(session.scalars(text("SELECT id FROM chat_context_checkpoints"))) == {"rls_checkpoint_a", "rls_private_checkpoint_" + person}
    with owned(rls, person="system"), rls["factory"]() as session:
        assert session.scalar(text("SELECT id FROM chat_conversations")) == "rls_conversation_a"
        assert session.scalar(text("SELECT id FROM chat_messages")) == "rls_message_a"
        assert session.scalar(text("SELECT id FROM chat_context_checkpoints")) == "rls_checkpoint_a"
        assert session.execute(text("SELECT * FROM venture_facts")).all() == []


def test_scope_can_bind_after_session_and_transaction_start(rls):
    with rls["factory"]() as session:
        assert session.scalar(text("SELECT 1")) == 1
        with owned(rls):
            assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"
            nested = session.begin_nested()
            with owned(rls, "b"):
                assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_b"
                nested.rollback()
                assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_b"
            assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"


def test_missing_scope_cannot_insert_memory(rls):
    with rls["factory"]() as session:
        with pytest.raises(DBAPIError, match="row-level security"):
            session.execute(text("""INSERT INTO institutional_memories
                (id, workspace_id, venture_id, dedupe_key, record_json, version)
                VALUES ('rls_unscoped', 'rls_workspace_a', 'rls_venture_a', 'unscoped', '{}', 1)"""))
        session.rollback()


def test_workspace_working_rows_remain_scoped_when_venture_is_null(rls):
    with rls["admin"].begin() as connection:
        connection.execute(text("""INSERT INTO memory_working_entries
            (scope_key, key, workspace_id, venture_id, payload_json, version, updated_at)
            VALUES ('workspace:rls_workspace_a', 'rls_workspace_key', 'rls_workspace_a', NULL,
                    '{}', 1, CURRENT_TIMESTAMP)"""))
    with owned(rls), rls["factory"]() as session:
        assert session.scalar(text("SELECT key FROM memory_working_entries WHERE venture_id IS NULL")) == "rls_workspace_key"
    with rls["bind"](workspace_id="rls_workspace_a", venture_id="", actor="system"), rls["factory"]() as session:
        assert session.scalar(text("SELECT key FROM memory_working_entries")) == "rls_workspace_key"
        assert session.execute(text("SELECT * FROM institutional_memories")).all() == []
    with rls["bind"](workspace_id="rls_workspace_b", venture_id="", actor="system"), rls["factory"]() as session:
        assert session.execute(text("SELECT * FROM memory_working_entries")).all() == []


def test_local_setting_clears_on_commit_rollback_and_pool_reuse(rls):
    with owned(rls), rls["factory"]() as session:
        pid = session.scalar(text("SELECT pg_backend_pid()"))
        assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"
        session.commit()
        assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"
        session.rollback()
    # Bypass the SQLAlchemy hook to prove set_config was transaction-local.
    raw = rls["runtime"].raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("SELECT current_setting('app.workspace_id', true), current_setting('app.venture_id', true), current_setting('app.person_id', true)")
        assert all(not value for value in cursor.fetchone())
        cursor.close()
        raw.rollback()
    finally:
        raw.close()
    with rls["factory"]() as session:
        assert session.scalar(text("SELECT pg_backend_pid()")) == pid
        assert session.execute(text("SELECT * FROM institutional_memories")).all() == []
        with owned(rls, "b"):
            assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_b"


@pytest.mark.parametrize("read", ["get", "get_one"])
@pytest.mark.parametrize("changed", ["workspace", "venture", "person", "missing"])
def test_session_identity_map_cannot_replay_cached_protected_rows(rls, changed, read):
    model = rls["models"]["institutional_memories"]
    with rls["factory"]() as session:
        with owned(rls):
            original = session.get(model, "rls_memory_a")
            assert original is not None
        scope = {"workspace": owned(rls, "b"), "venture": owned(rls, "c"),
                 "person": owned(rls, person="rls_person_b")}
        if changed == "missing":
            with pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
                getattr(session, read)(model, "rls_memory_a")
        else:
            with scope[changed], pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
                getattr(session, read)(model, "rls_memory_a")


def test_cross_scope_mutation_and_forged_fact_owner_are_denied(rls):
    with owned(rls), rls["factory"]() as session:
        with pytest.raises(DBAPIError, match="row-level security"):
            session.execute(text("UPDATE institutional_memories SET workspace_id = 'rls_workspace_b' WHERE id = 'rls_memory_a'"))
        session.rollback()
        with pytest.raises(DBAPIError, match="row-level security"):
            session.execute(text("""INSERT INTO venture_facts
                (id, workspace_id, venture_id, person_id, subject, predicate, value, value_json,
                 source_json, source_digest, dedupe_key, confidence, valid_from, recorded_at)
                VALUES ('rls_forged_owner', 'rls_workspace_a', 'rls_venture_a', 'rls_person_b',
                        'idea.forged', 'statement', 'Fixture', '"Fixture"', '{}', :digest, 'forged', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""), {"digest": "0" * 64})
        session.rollback()
        assert session.execute(text("DELETE FROM institutional_memories WHERE id = 'rls_memory_b'")).rowcount == 0
        assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"


@pytest.mark.parametrize("write", ["flush", "commit"])
def test_dirty_protected_row_cannot_flush_after_scope_switch(rls, write):
    model = rls["models"]["institutional_memories"]
    with rls["factory"]() as session:
        with owned(rls):
            original = session.get(model, "rls_memory_a")
            original.version += 1
        with owned(rls, "b"), pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
            getattr(session, write)()
        session.rollback()
        with owned(rls):
            assert session.get(model, "rls_memory_a").version == 1


def test_pending_protected_insert_is_pinned_before_any_flush(rls):
    model = rls["models"]["institutional_memories"]
    with rls["factory"]() as session:
        with owned(rls):
            session.add(model(id="rls_pending", workspace_id="rls_workspace_a", venture_id="rls_venture_a",
                              dedupe_key="pending", record_json="{}", version=1))
        assert not session.identity_map and session.new
        with owned(rls, person="rls_person_b"), pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
            session.flush()
        session.rollback()


def test_unprotected_identity_rows_allow_workspace_maintenance_binding(rls):
    from app.db.models import Venture
    with rls["factory"]() as session:
        first = session.get(Venture, "rls_venture_a")
        with owned(rls):
            assert first.id == "rls_venture_a"
            assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"
        with owned(rls, "b"):
            assert session.get(Venture, "rls_venture_b").id == "rls_venture_b"
            assert session.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_b"


def test_connection_marker_cannot_skip_scope_binding_and_autocommit_is_rejected(rls):
    with rls["runtime"].connect().execution_options(_supymem_scope_statement=True) as connection:
        with owned(rls):
            assert connection.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"
        assert connection.execute(text("SELECT * FROM institutional_memories")).all() == []
    with rls["runtime"].connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        with owned(rls), pytest.raises(PermissionError, match="database_workspace_scope_requires_transaction"):
            connection.execute(text("SELECT * FROM institutional_memories"))


def test_structural_compiled_cache_collision_cannot_recurse_or_skip_scope(rls):
    from app.db.session import _POSTGRES_SCOPE_SQL
    # Compile an identical caller-created TextClause first, in its own cache.
    # The recursive setter shares its compiled SQL but remains our invocation.
    with rls["runtime"].connect().execution_options(compiled_cache={}) as connection, owned(rls):
        connection.execute(text(str(_POSTGRES_SCOPE_SQL)), {
            "workspace_id": "rls_workspace_b", "venture_id": "rls_venture_b", "person_id": "rls_person_b",
        })
        assert connection.scalar(text("SELECT id FROM institutional_memories")) == "rls_memory_a"


def test_fact_scope_cannot_bind_a_venture_to_the_wrong_workspace(rls):
    with rls["bind"](workspace_id="rls_workspace_a", venture_id="rls_venture_b", actor="rls_person_a"), rls["factory"]() as session:
        with pytest.raises(DBAPIError, match="row-level security"):
            session.execute(text("""INSERT INTO venture_facts
                (id, workspace_id, venture_id, person_id, subject, predicate, value, value_json,
                 source_json, source_digest, dedupe_key, confidence, valid_from, recorded_at)
                VALUES ('rls_forged_scope', 'rls_workspace_a', 'rls_venture_b', 'rls_person_a',
                        'idea.forged', 'statement', 'Fixture', '"Fixture"', '{}', :digest, 'forged', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""), {"digest": "0" * 64})
        session.rollback()


@pytest.mark.asyncio
async def test_async_sessions_bind_and_clear_the_same_scope(rls):
    from app.db.session import make_async_engine, make_async_session_factory
    runtime = make_async_engine(rls["get_url"]())
    event.listen(runtime.sync_engine, "connect", rls["select_role"])
    factory = make_async_session_factory(runtime)
    try:
        async with factory() as session:
            assert (await session.execute(text("SELECT * FROM venture_facts"))).all() == []
            with owned(rls):
                assert (await session.scalar(text("SELECT person_id FROM venture_facts"))) == "rls_person_a"
                await session.commit()
                assert (await session.scalar(text("SELECT person_id FROM venture_facts"))) == "rls_person_a"
            await session.rollback()
            assert (await session.execute(text("SELECT * FROM venture_facts"))).all() == []
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_async_cached_get_and_get_one_remain_guarded_after_commit(rls):
    from app.db.session import make_async_engine, make_async_session_factory
    runtime = make_async_engine(rls["get_url"]())
    event.listen(runtime.sync_engine, "connect", rls["select_role"])
    factory = make_async_session_factory(runtime)
    model = rls["models"]["venture_facts"]
    try:
        async with factory() as session:
            with owned(rls):
                original = await session.get(model, "rls_fact_a_rls_person_a")
                await session.commit()
                assert original.person_id == "rls_person_a"  # expire_on_commit=False
            with owned(rls, person="rls_person_b"):
                for read in (session.get, session.get_one):
                    with pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
                        await read(model, original.id)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("operation", ["raw", "unprotected_get", "unprotected_query", "connection", "flush", "commit"])
def test_protected_resident_blocks_every_session_operation_after_scope_change(rls, operation):
    from app.db.models import InstitutionalMemory, Venture
    with rls["factory"]() as session:
        with owned(rls):
            original = session.get(InstitutionalMemory, "rls_memory_a")
        actions = {"raw": lambda: session.execute(text("SELECT 1")),
                   "unprotected_get": lambda: session.get(Venture, "rls_venture_b"),
                   "unprotected_query": lambda: session.scalars(select(Venture)).all(),
                   "connection": session.connection, "flush": session.flush, "commit": session.commit}
        with owned(rls, "b"), pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
            actions[operation]()
        assert original.id == "rls_memory_a"


def test_protected_cache_merge_without_load_cannot_adopt_another_actor(rls):
    from app.db.models import InstitutionalMemory
    with owned(rls), rls["factory"]() as source:
        original = source.get(InstitutionalMemory, "rls_memory_a")
        source.expunge(original)
    with owned(rls, person="rls_person_b"), rls["factory"]() as target:
        with pytest.raises(PermissionError, match="database_protected_merge_requires_load"):
            target.merge(original, load=False)


@pytest.mark.parametrize("consume", ["all", "first", "mappings", "cached", "scalar", "text"])
def test_delayed_protected_results_cannot_hydrate_or_return_under_another_scope(rls, consume):
    from app.db.models import ChatMessage
    with rls["factory"]() as session:
        with owned(rls):
            if consume == "cached":
                original = session.get(ChatMessage, "rls_message_a")
                assert original.id == "rls_message_a"
            statement = (text("SELECT body_md FROM chat_messages") if consume == "text"
                         else select(ChatMessage.body_md) if consume == "scalar" else select(ChatMessage))
            result = session.execute(statement)
        with owned(rls, "b"), pytest.raises(PermissionError, match="database_result_scope_changed"):
            if consume == "first":
                result.scalars().first()
            elif consume == "mappings":
                result.mappings().all()
            else:
                result.scalars().all()


@pytest.mark.asyncio
async def test_async_delayed_protected_result_is_bound_to_its_sql_origin(rls):
    from app.db.models import ChatMessage
    from app.db.session import make_async_engine, make_async_session_factory
    runtime = make_async_engine(rls["get_url"]())
    event.listen(runtime.sync_engine, "connect", rls["select_role"])
    factory = make_async_session_factory(runtime)
    try:
        async with factory() as session:
            with owned(rls):
                result = await session.scalars(select(ChatMessage))
            with owned(rls, "b"), pytest.raises(PermissionError, match="database_result_scope_changed"):
                result.all()
    finally:
        await runtime.dispose()


def test_save_update_cascade_cannot_rebind_a_detached_protected_child(rls):
    from sqlalchemy import ForeignKey, Text
    from sqlalchemy.orm import Mapped, mapped_column, relationship
    from app.db.models import Base, InstitutionalMemory

    class Parent(Base):
        __tablename__ = "rls_scope_parents"
        id: Mapped[str] = mapped_column(Text, primary_key=True)
        memory_id: Mapped[str] = mapped_column(Text, ForeignKey("institutional_memories.id"))
        memory: Mapped[InstitutionalMemory] = relationship(InstitutionalMemory)

    Parent.__table__.create(rls["admin"], checkfirst=True)
    with owned(rls), rls["factory"]() as source:
        original = source.get(InstitutionalMemory, "rls_memory_a")
        source.expunge(original)
    with owned(rls, person="rls_person_b"), rls["factory"]() as target:
        with pytest.raises(PermissionError, match="database_scope_changed_use_new_session"):
            target.add(Parent(id="parent", memory=original))
        target.rollback()


def journal_source(d, *, label="a", person="rls_person_a", suffix="source"):
    """Crafted private input/job rows; no provider call or real customer data."""
    from app.db.models import ChatConversation, ChatMessage, Job
    now = datetime(2026, 10, 8)
    wid = "rls_workspace_b" if label == "b" else "rls_workspace_a"
    vid = "rls_venture_" + label
    cid, mid, jid = "rls_journal_cnv_" + suffix, "rls_journal_message_" + suffix, "rls_journal_job_" + suffix
    payload = {"workspace_id": wid, "venture_id": vid, "source_message_id": mid, "_actor_subject": person,
               "_chat_conversation": {"schema_version": 1, "conversation_id": cid,
                                      "workspace_id": wid, "venture_id": vid, "actor_id": person}}
    snapshot = {"source_message_id": mid, "workspace_id": wid, "venture_id": vid, "person_id": person,
                "conversation_id": cid, "created_at": "2026-10-08T00:00:00+00:00",
                "visibility": "private", "creator_person_id": person}
    snapshot["digest"] = hashlib.sha256(json.dumps({**snapshot, "body": "My audience is independent founders.",
        "role": "user", "kind": "text"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    doc = {"_runtime": {"job_id": jid, "actor": person, "source": snapshot, "operations": {}, "calls_used": 0}}
    did = "memory_extract:" + hashlib.sha256(jid.encode()).hexdigest()
    with sessionmaker(bind=d["admin"])() as session:
        session.add(ChatConversation(id=cid, workspace_id=wid, venture_id=vid, creator_person_id=person,
                                    visibility="private", title="Private journal source", creation_key=cid,
                                    created_at=now, updated_at=now))
        session.flush()
        session.add(ChatMessage(id=mid, workspace_id=wid, venture_id=vid, conversation_id=cid,
            author_person_id=person, role="user", kind="text", body_md="My audience is independent founders.", created_at=now))
        session.add(Job(id=jid, kind="memory.extract", workspace_id=wid, venture_id=vid,
            payload_json=json.dumps(payload), status="leased", locked_by="rls_worker", locked_at=now,
            attempts=1, max_attempts=1, created_at=now))
        session.commit()
    return did, doc, payload


def admit_journal(d, did, doc):
    from app.db.models import VentureDocument
    with owned(d), d["factory"]() as session:
        session.add(VentureDocument(id=did, venture_id="rls_venture_a", kind="memory_extract_runtime_v1",
            title="Private memory extraction receipt", provenance_json=json.dumps(doc),
            created_at=datetime(2026, 10, 8), updated_at=datetime(2026, 10, 8)))
        session.commit()


def test_private_journal_is_actor_scoped_while_other_documents_keep_their_baseline(rls):
    from app.db.models import VentureDocument
    did, doc, _ = journal_source(rls)
    admit_journal(rls, did, doc)
    with rls["admin"].begin() as connection:
        connection.execute(text("""INSERT INTO venture_documents
            (id, venture_id, kind, title, provenance_json, created_at, updated_at)
            VALUES ('rls_regular_doc', 'rls_venture_a', 'landing', 'Ordinary document', 'legacy invalid JSON',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""))
    with owned(rls), rls["factory"]() as session:
        assert set(session.scalars(select(VentureDocument.id))) == {did, "rls_regular_doc"}
    for scope in (owned(rls, person="rls_person_b"), owned(rls, "b")):
        with scope, rls["factory"]() as session:
            assert list(session.scalars(select(VentureDocument.id))) == ["rls_regular_doc"]
    with rls["factory"]() as session:
        assert list(session.scalars(select(VentureDocument.id))) == ["rls_regular_doc"]


@pytest.mark.parametrize("tamper", ["kind", "venture", "source_person", "source_message", "actor", "job", "route", "payload_source"])
def test_private_journal_source_binding_cannot_be_downgraded_or_forged(rls, tamper):
    did, doc, payload = journal_source(rls)
    doc["_runtime"]["route"] = {"provider": "fixture", "model": "fixture"}
    doc["_runtime"]["payload"] = {**payload, "_job_id": doc["_runtime"]["job_id"], "_worker_id": "rls_worker", "_job_attempt": 1}
    admit_journal(rls, did, doc)
    updated = json.loads(json.dumps(doc))
    changes = {"kind": "kind = 'landing'", "venture": "venture_id = 'rls_venture_c'"}
    if tamper == "source_person":
        updated["_runtime"]["source"]["person_id"] = "rls_person_b"
    elif tamper == "source_message":
        updated["_runtime"]["source"]["source_message_id"] = "rls_message_b"
    elif tamper in {"actor", "job", "route"}:
        updated["_runtime"][{"job": "job_id"}.get(tamper, tamper)] = "forged"
    elif tamper == "payload_source":
        updated["_runtime"]["payload"]["source_message_id"] = "rls_message_b"
    update = changes.get(tamper, "provenance_json = :doc")
    with owned(rls), rls["factory"]() as session:
        with pytest.raises(DBAPIError, match="memory_extract_receipt_immutable|memory_extract_private_source_invalid"):
            session.execute(text(f"UPDATE venture_documents SET {update} WHERE id = :id"),
                            {"id": did, "doc": json.dumps(updated)})
        session.rollback()


def test_forged_private_journal_cannot_reference_another_founders_source(rls):
    did, doc, _ = journal_source(rls, person="rls_person_b", suffix="foreign")
    doc["_runtime"]["actor"] = "rls_person_a"
    doc["_runtime"]["source"]["person_id"] = "rls_person_a"
    doc["_runtime"]["source"]["creator_person_id"] = "rls_person_a"
    with pytest.raises(DBAPIError, match="memory_extract_private_source_invalid"):
        admit_journal(rls, did, doc)


def test_already_admitted_private_journal_can_save_funded_response_after_source_revocation(rls):
    from app.db.models import VentureDocument
    did, doc, _ = journal_source(rls)
    admit_journal(rls, did, doc)
    with rls["admin"].begin() as connection:
        connection.execute(text("UPDATE chat_messages SET body_md = 'Changed after funding' WHERE id = :id"),
                           {"id": doc["_runtime"]["source"]["source_message_id"]})
        connection.execute(text("UPDATE chat_conversations SET creator_person_id = 'rls_person_b' WHERE id = :id"),
                           {"id": doc["_runtime"]["source"]["conversation_id"]})
        connection.execute(text("UPDATE jobs SET status = 'failed_dead', locked_by = NULL WHERE id = :id"),
                           {"id": doc["_runtime"]["job_id"]})
    updated = json.loads(json.dumps(doc))
    updated["_runtime"]["operations"] = {"extract": {"state": "responded", "content": "Fixture funded response",
        "usage": {"state": "settled", "source": "platform", "fixture_only": True}}}
    with owned(rls), rls["factory"]() as session:
        row = session.get(VentureDocument, did)
        assert row is not None
        row.provenance_json = json.dumps(updated)
        session.commit()
        assert json.loads(session.get(VentureDocument, did).provenance_json) == updated
        assert session.execute(text("SELECT * FROM chat_messages WHERE id = :id"),
                               {"id": doc["_runtime"]["source"]["source_message_id"]}).all() == []
    # This proves receipt persistence only. Projection's actual financial
    # recovery tests must prove settlement and no facts after source revocation.
    with owned(rls, person="rls_person_b"), rls["factory"]() as session:
        assert session.get(VentureDocument, did) is None
