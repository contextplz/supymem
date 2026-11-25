"""Bounded retrieval, two unrelated private founders, and offline embedding custody."""
import asyncio
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.unit.memory.test_facts import facts as facts
from tests.integration.test_postgres_auth_security import service_modules

ROOT = Path(__file__).resolve().parents[3]


def test_v2_pack_keeps_only_current_owned_receipt_outcomes(founders, monkeypatch):
    from datetime import timedelta
    from sqlalchemy.orm import Session
    from app.db.models import InstitutionalMemory
    from app.db.supymem_models import BuildRun, FounderApp
    from app.memory import evidence
    from app.memory.adapters import postgres
    from app.memory.model import MemoryKind, MemoryRecord, TaskRef
    from app.tenancy import bind
    import app.hosting.service

    d, domains, profile = founders
    sources, artifacts = {}, {}
    original_get = Session.get

    def get(session, model, identity, **kwargs):
        return sources.get((model, identity)) if model in {BuildRun, FounderApp} else original_get(session, model, identity, **kwargs)

    def accepted(session, app, run_id, *, historical):
        assert historical and sources[(BuildRun, run_id)].app_id == app.id
        return None, artifacts[run_id], SimpleNamespace(id="qa_" + run_id, report_digest="qa-digest"), {"manifest_digest": "manifest"}, None

    monkeypatch.setattr(Session, "get", get)
    monkeypatch.setattr(app.hosting.service, "_accepted_source", accepted)
    monkeypatch.setattr(postgres, "db", lambda: d.factory)
    monkeypatch.setattr(profile, "selection_for", lambda *args, **kwargs: {"facts": [], "legacy": []})
    for founder in domains:
        venture_id = founder["venture_id"]
        app_id, run_id = "app_" + venture_id, "run_" + venture_id
        scope = {key: founder[key] for key in ("workspace_id", "venture_id")}
        sources[(FounderApp, app_id)] = SimpleNamespace(id=app_id, **scope)
        sources[(BuildRun, run_id)] = SimpleNamespace(id=run_id, app_id=app_id, spec="{}", **scope)
        artifacts[run_id] = SimpleNamespace(id="artifact_" + venture_id, sha256="owned-digest")
        with bind(**scope, actor=founder["person_id"]), d.factory() as session:
            proof = evidence._resolve(session, "accepted_source:" + run_id, **scope)
            record = MemoryRecord(mem_id="outcome_" + venture_id, venture_id=venture_id, kind=MemoryKind.OUTCOME,
                subject="build.source", statement=proof["statement"], author="system", confidence=1,
                evidence=[proof["ref"]], valid_from=d.when, dedupe_key="receipt_" + venture_id,
                metadata={"workspace_id": scope["workspace_id"], "source_evidence": [proof]})
            for suffix, changes in (("", {}), ("_expired", {"valid_to": d.when + timedelta(days=1)}),
                    ("_unbacked", {"evidence": ["accepted_source:missing"]}), ("_future", {"valid_from": d.when + timedelta(days=365)})):
                saved = record.model_copy(update={"mem_id": record.mem_id + suffix, "dedupe_key": record.dedupe_key + suffix, **changes})
                session.add(InstitutionalMemory(id=saved.mem_id, dedupe_key=saved.dedupe_key,
                    record_json=json.dumps(saved.model_dump(mode="json"), indent=2), **scope))
            session.commit()
    adapter = postgres.PostgresMemoryAdapter()
    for founder in domains:
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]):
            pack = adapter._context_pack_v2(founder["venture_id"], TaskRef(title=founder["statement"]), 4000)
            assert [hit.mem_id for hit in pack.memories] == ["outcome_" + founder["venture_id"]]
            assert pack.memories[0].kind == MemoryKind.OUTCOME
            small = adapter._context_pack_v2(founder["venture_id"], TaskRef(title=founder["statement"]), 1)
            assert small.memories == [] and small.omitted_count == 1
            artifacts["run_" + founder["venture_id"]].sha256 = "withdrawn-digest"
            assert adapter._context_pack_v2(founder["venture_id"], TaskRef(title=founder["statement"]), 4000).memories == []


@pytest.fixture(autouse=True)
def current_checkout():
    with service_modules("company_os"):
        yield


@pytest.fixture
def founders(facts):
    from app.db.models import Person, Venture, Workspace
    from app.tenancy import bind
    from app.memory import profile
    d = facts
    with d.factory() as session:
        session.add(Person(id="usr_bob", display_name="Garden owner", created_at=d.when))
        session.flush()
        session.add(Workspace(id="wsp_other", owner_id="usr_bob", kind="venture", name="Garden journal",
            entry_mode="idea", created_at=d.when, updated_at=d.when))
        session.flush()
        session.add(Venture(id="ven_other", workspace_id="wsp_other", name="Garden journal", raw_idea="Seed tracking",
            stage="draft", created_at=d.when, updated_at=d.when))
        session.flush()
        session.add(d.Conversation(id="cnv_other", workspace_id="wsp_other", venture_id="ven_other",
            creator_person_id="usr_bob", visibility="private", title="Garden", creation_key="garden",
            created_at=d.when, updated_at=d.when))
        session.commit()
    founders = [dict(workspace_id="wsp_owned", venture_id="ven_owned", person_id="usr_alice",
        conversation_id="cnv_owned", statement="Our audience is freight dispatch teams."),
        dict(workspace_id="wsp_other", venture_id="ven_other", person_id="usr_bob",
        conversation_id="cnv_other", statement="Our audience is balcony gardeners.")]
    spec = importlib.util.spec_from_file_location("memory_search_migration", ROOT /
        "services/company_os/app/db/migrations/versions/s058memsearch.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with d.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    d.Base.metadata.tables["institutional_memories"].create(d.engine, checkfirst=True)
    for index, founder in enumerate(founders):
        d.message("domain_" + str(index), founder["statement"], workspace_id=founder["workspace_id"],
            venture_id=founder["venture_id"], author_person_id=founder["person_id"],
            conversation_id=founder["conversation_id"])
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            row = d.api.remember_fact(session, venture_id=founder["venture_id"], subject="idea.audience",
                predicate="is", value=founder["statement"],
                source={"type": "chat", "ref": "chat:domain_" + str(index), "quote": founder["statement"]})
            founder["fact_id"] = row.id
            session.commit()
            session.execute(text("""INSERT INTO memory_core_profiles
                (workspace_id,venture_id,person_id,generation,refreshed_generation,payload_json)
                VALUES (:workspace_id,:venture_id,:person_id,1,1,:payload)"""),
                {**founder, "payload": json.dumps({"fact_ids": [row.id], "records": []})})
            session.commit()
    return d, founders, profile


def test_profile_is_scoped_and_skips_search_for_two_unrelated_domains(founders, monkeypatch):
    from app.memory.context import context_for
    from app.tenancy import bind
    import app.memory
    d, founders, profile = founders
    monkeypatch.setattr(profile, "enabled", lambda session=None: True)
    monkeypatch.setattr(app.memory, "get_memory", lambda: SimpleNamespace(name="postgres"))
    monkeypatch.setattr(profile, "search_rows", lambda *args, **kwargs: pytest.fail("profile answer must not search"))
    for founder in founders:
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            projected = context_for(session, workspace_id=founder["workspace_id"], venture_id=founder["venture_id"],
                purpose="venture.chat.assistant", query="who is my audience?")
            assert [row["statement"] for row in projected["facts"]] == [founder["statement"]]
            assert all(other["statement"] not in json.dumps(projected) for other in founders if other != founder)
            with pytest.raises(PermissionError):
                profile.read_profile(session, venture_id=next(other["venture_id"] for other in founders if other != founder))


def test_cached_profile_never_revives_withdrawn_sources(founders):
    from app.tenancy import bind
    d, founders, profile = founders
    for index, founder in enumerate(founders):
        with d.factory() as session:
            session.get(d.Message, "domain_" + str(index)).body_md = "Withdrawn quotation"
            session.commit()
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            selection = profile.read_profile(session, venture_id=founder["venture_id"])
            assert selection["facts"] == []
            assert selection["blocked_count"] == 1


def test_profile_batches_source_validation_for_both_domains(founders):
    from app.tenancy import bind
    from sqlalchemy import event
    d, founders, profile = founders
    for index, founder in enumerate(founders):
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            source = {"type": "chat", "ref": "chat:domain_" + str(index), "quote": founder["statement"]}
            pricing = d.api.remember_fact(session, venture_id=founder["venture_id"], subject="idea.pricing", predicate="is",
                value=founder["statement"], source=source)
            session.commit()
            rows = [session.get(d.Fact, founder["fact_id"]), pricing]
            statements = []
            def capture(connection, cursor, statement, parameters, context, many):
                statements.append(statement.lower())
            event.listen(d.engine, "before_cursor_execute", capture)
            try:
                selection = profile._selection(session,
                    {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}, rows, [])
            finally:
                event.remove(d.engine, "before_cursor_execute", capture)
            assert len(selection["facts"]) == 2
            assert sum("from chat_messages" in statement for statement in statements) == 1
            assert sum("from chat_conversations" in statement for statement in statements) == 1
            for invalid in ("not JSON", '{"ref":null}'):
                pricing.source_json = invalid
                session.commit()
                selection = profile._selection(session,
                    {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}, rows, [])
                assert [row.id for row in selection["facts"]] == [founder["fact_id"]]
                assert selection["blocked_count"] == 1


def test_local_flag_on_and_off_context_is_byte_identical_for_two_founders(founders, monkeypatch):
    from app.memory.context import context_for
    from app.tenancy import bind
    import app.memory
    import app.credits.policy
    d, founders, profile = founders
    monkeypatch.setattr(app.memory, "get_memory", lambda: SimpleNamespace(name="postgres", _records=lambda venture: []))
    for founder in founders:
        outputs = []
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            for flag in (False, True):
                monkeypatch.setattr(app.credits.policy, "get_policy", lambda key, flag=flag: {"memory_search_v2": flag})
                result = context_for(session, workspace_id=founder["workspace_id"], venture_id=founder["venture_id"],
                    purpose="venture.chat.assistant", query="who is my audience?")
                assert [row["statement"] for row in result["facts"]] == [founder["statement"]]
                outputs.append(json.dumps(result, separators=(",", ":")).encode())
        assert outputs[0] == outputs[1]


def test_newer_fact_can_follow_a_forgotten_legacy_report(founders):
    from app.memory.model import MemoryRecord, MemoryKind
    from app.tenancy import bind
    from datetime import timedelta
    d, founders, profile = founders
    for index, founder in enumerate(founders):
        source = d.message("after_forget_" + str(index), founder["statement"], day=2,
            workspace_id=founder["workspace_id"], venture_id=founder["venture_id"],
            author_person_id=founder["person_id"], conversation_id=founder["conversation_id"])
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            row = d.api.remember_fact(session, venture_id=founder["venture_id"], subject="idea.audience", predicate="is",
                value=founder["statement"], source=source)
            session.commit()
            retired = MemoryRecord(mem_id="forgotten_" + str(index), venture_id=founder["venture_id"], kind=MemoryKind.INSIGHT,
                subject="general", statement=founder["statement"], confidence=1, author=founder["person_id"],
                valid_from=d.when, dedupe_key="forgotten_" + str(index), tombstoned=True,
                metadata={"workspace_id": founder["workspace_id"], "forgotten_at": (d.when + timedelta(days=1)).isoformat()})
            selection = profile._selection(session, {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}, [row], [retired])
            assert [fact.id for fact in selection["facts"]] == [row.id]
            assert selection["blocked_count"] == 0
            for invalid_at in ((d.when + timedelta(days=1)).replace(tzinfo=None).isoformat(), "2099-01-01T00:00:00+00:00"):
                retired.metadata["forgotten_at"] = invalid_at
                selection = profile._selection(session,
                    {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}, [row], [retired])
                assert selection["facts"] == [] and selection["blocked_count"] == 1


def test_profile_current_facts_suppress_older_search_hits_in_both_domains(founders, monkeypatch):
    from app.memory.model import MemoryKind, MemoryRecord
    from app.memory.evidence import validate_write_evidence
    from app.tenancy import bind
    from datetime import timedelta
    d, founders, profile = founders
    monkeypatch.setattr(profile, "_latest", lambda *args, **kwargs: [])
    for index, founder in enumerate(founders):
        statement = "Our audience is warehouse buyers." if index == 0 else "Our audience is indoor plant collectors."
        source = d.message("older_report_" + str(index), "Remember: " + statement, day=-1,
            workspace_id=founder["workspace_id"], venture_id=founder["venture_id"],
            author_person_id=founder["person_id"], conversation_id=founder["conversation_id"])
        scope = {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}
        with bind(workspace_id=scope["workspace_id"], venture_id=scope["venture_id"], actor=scope["person_id"]), d.factory() as session:
            record = MemoryRecord(mem_id="older_" + str(index), venture_id=scope["venture_id"], kind=MemoryKind.INSIGHT,
                subject="general", statement=statement, evidence=[source["ref"]], confidence=1,
                author=scope["person_id"], valid_from=d.when - timedelta(days=1), dedupe_key="older_" + str(index),
                metadata={"workspace_id": scope["workspace_id"]})
            record.metadata["source_evidence"] = validate_write_evidence(session, record)
            monkeypatch.setattr(profile, "search_rows", lambda *args, **kwargs: [{"record_type": "memory",
                "id": record.mem_id, "dedupe_key": record.dedupe_key, "record_json": record.model_dump_json()}])
            monkeypatch.setattr(profile, "_legacy_records", lambda *args, **kwargs: [record])
            selection = profile.selection_for(session, venture_id=scope["venture_id"], query="audience and pricing")
            assert [row.value for row in selection["facts"]] == [founder["statement"]]
            assert selection["legacy"] == []


def test_search_records_reject_forged_identity_and_foreign_scope_in_both_domains(founders):
    from app.memory.model import MemoryKind, MemoryRecord
    d, founders, profile = founders
    for founder in founders:
        scope = {key: founder[key] for key in ("workspace_id", "venture_id", "person_id")}
        record = MemoryRecord(mem_id="record", venture_id=scope["venture_id"], kind=MemoryKind.INSIGHT,
            subject="general", statement=founder["statement"], confidence=1, author=scope["person_id"],
            valid_from=d.when, dedupe_key="dedupe", metadata={"workspace_id": scope["workspace_id"]})
        payload = record.model_dump_json()
        assert profile.scoped_record(scope, "record", "dedupe", payload) == record
        assert profile.scoped_record(scope, "forged", "dedupe", payload) is None
        assert profile.scoped_record(scope, "record", "forged", payload) is None
        foreign = next(other for other in founders if other != founder)
        assert profile.scoped_record(foreign, "record", "dedupe", payload) is None


@pytest.mark.parametrize("table", ["venture_facts", "institutional_memories"])
@pytest.mark.parametrize("vector", [False, True])
def test_hybrid_sql_is_one_bounded_scoped_rrf(table, vector):
    from app.memory.fuse import hybrid_sql
    sql = hybrid_sql(table=table, vector=vector)
    assert "bm25" not in sql and "sum(score)" in sql
    assert sql.count("workspace_id=:workspace_id") == (3 if vector else 2)
    assert sql.count("venture_id=:venture_id") == (3 if vector else 2)
    assert sql.count("LIMIT :candidates") == (3 if vector else 2)
    assert "LIMIT :limit" in sql and "websearch_to_tsquery" in sql
    assert ("CAST(:embedding AS vector)" in sql) is vector
    if table == "institutional_memories":
        assert sql.count(":subject=ANY(report_subjects)") == 1
    else:
        assert sql.count("subject=:subject") == 1
    with pytest.raises(ValueError):
        hybrid_sql(table="other; DROP TABLE venture_facts")


def test_unified_sql_bounds_and_fuses_both_memory_stores():
    from app.memory.fuse import hybrid_sql
    sql = hybrid_sql(table="all", vector=True)
    assert "'fact' AS record_type" in sql and "'memory' AS record_type" in sql
    assert sql.count("workspace_id=:workspace_id") == 6
    assert sql.count("venture_id=:venture_id") == 6
    assert sql.count("LIMIT :candidates") == 6
    assert "ORDER BY score DESC,record_type,id LIMIT :limit" in sql


@pytest.mark.parametrize("table", ["venture_facts", "institutional_memories"])
def test_historical_sql_filters_active_intervals_before_bounded_ranking(table):
    from app.memory.fuse import hybrid_sql
    sql = hybrid_sql(table=table, as_of=True)
    assert "superseded_by' IS NULL" not in sql
    assert "valid_to>:at" in sql if table == "venture_facts" else "::timestamptz > :at" in sql
    assert sql.count("venture_id=:venture_id") == 2 and sql.count("LIMIT :candidates") == 2


@pytest.mark.parametrize("value", [None, {}, {"memory_search_v2": False}, {"memory_search_v2": "true"}])
def test_search_flag_defaults_off_and_local_never_changes(facts, monkeypatch, value):
    from app.memory.profile import enabled
    import app.credits.policy
    monkeypatch.setattr(app.credits.policy, "get_policy", lambda key: value)
    assert not enabled()
    monkeypatch.setattr(app.credits.policy, "get_policy", lambda key: {"memory_search_v2": True})
    with facts.factory() as session:
        assert not enabled(session)


def test_profile_does_not_claim_compound_or_unanswered_questions(founders):
    d, founders, profile = founders
    from app.tenancy import bind
    for founder in founders:
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            selection = profile.read_profile(session, venture_id=founder["venture_id"])
            assert profile._answers(selection, "who is my audience?", "idea.audience")
            assert not profile._answers(selection, "audience and pricing", "idea.audience")
            assert not profile._answers(selection, "audience churn last quarter", "idea.audience")
            assert not profile._answers(selection, "what is our pricing?", "idea.pricing")
            legacy_only = {**selection, "facts": [], "legacy": [SimpleNamespace(statement=founder["statement"])]}
            assert profile._answers(legacy_only, "who is my audience?", "idea.audience")


def test_embedding_gateway_is_metered_and_does_not_block_loop(facts, monkeypatch):
    from app.memory import profile
    from app.credits import metered
    from llm_gateway import sole
    scopes = [dict(workspace_id="wsp_owned", venture_id="ven_owned", person_id="usr_alice"),
        dict(workspace_id="wsp_other", venture_id="ven_other", person_id="usr_bob")]
    calls = []
    async def embed(texts, **kwargs):
        await asyncio.sleep(0)
        calls.append((texts, kwargs))
        return SimpleNamespace(data=[SimpleNamespace(embedding=[.01] * 1536)], usage=SimpleNamespace(prompt_tokens=7))
    async def call(callback, **kwargs):
        assert kwargs["purpose"] == "memory.embed" and kwargs["estimate_micro"] > 0
        response = await callback()
        assert kwargs["actual_micro"](response) >= 0
        return response
    monkeypatch.setattr(sole, "embed", embed)
    monkeypatch.setattr(metered, "call", call)
    config = SimpleNamespace(deployment_embed="configured-embedding", provider="foundry")
    async def run():
        for scope, content in zip(scopes, ("freight dispatch analytics", "balcony seed journal")):
            result = await profile.embed_content(content, scope=scope, config=config)
            assert len(json.loads(result)) == 1536
    asyncio.run(run())
    assert [kwargs["workspace_id"] for _, kwargs in calls] == [scope["workspace_id"] for scope in scopes]
    assert all(kwargs["config"] is config for _, kwargs in calls)


def test_postgres_v2_recall_dispatches_database_work_off_loop(facts, monkeypatch):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import RecallQuery
    adapter = PostgresMemoryAdapter()
    monkeypatch.setattr(adapter, "_search_v2_enabled", lambda: True)
    loop_thread = threading.get_ident()
    def recall(query):
        assert threading.get_ident() != loop_thread
        return [query.venture_id]
    monkeypatch.setattr(adapter, "_recall_v2", recall)
    for venture in ("ven_owned", "ven_other"):
        assert asyncio.run(adapter.recall(RecallQuery(venture_id=venture))) == [venture]


def test_memory_port_retrieves_source_qualified_facts_in_both_domains(founders, monkeypatch):
    from app.memory.adapters import postgres
    from app.memory.model import RecallQuery
    from app.tenancy import bind, current
    d, founders, profile = founders
    monkeypatch.setattr(postgres, "db", lambda: d.factory)
    monkeypatch.setattr(profile, "_legacy_records", lambda *args, **kwargs: [])
    monkeypatch.setattr(profile, "_latest", lambda *args, **kwargs: [])
    def rows(session, **kwargs):
        founder = next(founder for founder in founders if founder["venture_id"] == current().venture_id)
        return [{"id": founder["fact_id"], "record_type": "fact", "score": .03}]
    monkeypatch.setattr(profile, "search_rows", rows)
    adapter = postgres.PostgresMemoryAdapter()
    monkeypatch.setattr(adapter, "_search_v2_enabled", lambda: True)
    for founder in founders:
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]):
            hits = asyncio.run(adapter.recall(RecallQuery(venture_id=founder["venture_id"], text="audience")))
            assert [hit.statement for hit in hits] == [founder["statement"]]
            assert hits[0].mem_id == founder["fact_id"] and hits[0].evidence


def test_historical_recall_preserves_retired_reports_but_revalidates_sources_in_both_domains(founders, monkeypatch):
    from app.memory.adapters import postgres
    from app.memory.model import MemoryKind, MemoryRecord, RecallQuery
    from app.memory.evidence import validate_write_evidence
    from app.tenancy import bind
    from datetime import timedelta
    d, founders, profile = founders
    monkeypatch.setattr(postgres, "db", lambda: d.factory)
    adapter = postgres.PostgresMemoryAdapter()
    monkeypatch.setattr(adapter, "_search_v2_enabled", lambda: True)
    at = d.when + timedelta(hours=12)
    for index, founder in enumerate(founders):
        source = d.message("archive_" + str(index), "Remember: " + founder["statement"],
            workspace_id=founder["workspace_id"], venture_id=founder["venture_id"],
            author_person_id=founder["person_id"], conversation_id=founder["conversation_id"])
        with bind(workspace_id=founder["workspace_id"], venture_id=founder["venture_id"], actor=founder["person_id"]), d.factory() as session:
            record = MemoryRecord(mem_id="mem_archive_" + str(index), venture_id=founder["venture_id"], kind=MemoryKind.INSIGHT,
                subject="general", statement=founder["statement"], evidence=[source["ref"]], confidence=1,
                author=founder["person_id"], valid_from=d.when, valid_to=d.when + timedelta(days=1),
                superseded_by="mem_newer", dedupe_key="archive_" + str(index), metadata={"workspace_id": founder["workspace_id"]})
            record.metadata["source_evidence"] = validate_write_evidence(session, record)
            def rows(session, **kwargs):
                assert kwargs["at"] == at
                return [{"record_type": "fact", "id": founder["fact_id"], "score": .03},
                    {"record_type": "memory", "id": record.mem_id, "dedupe_key": record.dedupe_key,
                        "record_json": record.model_dump_json(), "score": .02}]
            monkeypatch.setattr(profile, "search_rows", rows)
            query = RecallQuery(venture_id=founder["venture_id"], as_of=at)
            assert [hit.mem_id for hit in asyncio.run(adapter.recall(query))] == [founder["fact_id"], record.mem_id]
            session.get(d.Message, source["ref"].split(":", 1)[1]).body_md = "Withdrawn report"
            session.commit()
            assert [hit.mem_id for hit in asyncio.run(adapter.recall(query))] == [founder["fact_id"]]


def test_background_embeddings_are_content_hash_cached_per_venture(facts, monkeypatch):
    from app.memory import profile
    from app.tenancy import current
    jobs = [dict(workspace_id="wsp_owned", venture_id="ven_owned", person_id="usr_alice", request_hash=""),
        dict(workspace_id="wsp_other", venture_id="ven_other", person_id="usr_bob", request_hash="")]
    claimed, cached, calls, finished = iter(jobs), {}, [], []
    config = SimpleNamespace(deployment_embed="configured-embedding")
    monkeypatch.setattr(profile, "_claim", lambda: next(claimed, None))
    def prepare(job):
        assert current().venture_id == job["venture_id"]
        scope = {key: job[key] for key in ("workspace_id", "venture_id", "person_id")}
        content = "freight dispatch" if job["person_id"] == "usr_alice" else "balcony seeds"
        return scope, [("venture_facts", "one", content), ("venture_facts", "two", content)], config, False, False
    monkeypatch.setattr(profile, "_prepare", prepare)
    monkeypatch.setattr(profile, "_cache", lambda scope, deployment, content:
        cached.get((scope["venture_id"], deployment, profile._hash(content))))
    def save(scope, deployment, content, embedding, *args):
        cached[(scope["venture_id"], deployment, profile._hash(content))] = embedding
    monkeypatch.setattr(profile, "_save_embedding", save)
    monkeypatch.setattr(profile, "_finish", lambda job, **kwargs: finished.append((job, kwargs)))
    async def embed(content, *, scope, **kwargs):
        calls.append((scope["venture_id"], content))
        return "[0.1]"
    monkeypatch.setattr(profile, "embed_content", embed)
    async def run():
        assert await profile.run_one()
        assert await profile.run_one()
        assert not await profile.run_one()
    asyncio.run(run())
    assert len(calls) == len(cached) == len(finished) == 2
    assert {venture for venture, _ in calls} == {job["venture_id"] for job in jobs}


def test_disabled_worker_leaves_both_founders_jobs_untouched(founders, monkeypatch):
    from app import state
    d, founders, profile = founders
    monkeypatch.setattr(state, "db", lambda: d.factory)
    with d.factory() as session:
        for founder in founders:
            session.execute(text("""INSERT INTO memory_search_jobs(workspace_id,venture_id,person_id)
                VALUES (:workspace_id,:venture_id,:person_id)"""), founder)
        session.commit()
    assert not asyncio.run(profile.run_one())
    with d.factory() as session:
        assert session.scalar(text("SELECT count(*) FROM memory_search_jobs")) == 2


def test_unconfigured_embeddings_still_allow_fulltext_for_both_domains(founders, monkeypatch, caplog):
    d, founders, profile = founders
    monkeypatch.setattr(profile, "vector_available", lambda session: True)
    def missing(workspace_id):
        raise ValueError("memory_embedding_config_unavailable")
    monkeypatch.setattr(profile, "deployment_config", missing)
    for founder in founders:
        assert profile.cached_embedding(None, founder, "audience") == (None, None)
    assert "memory_search_embedding_unconfigured_fulltext_only" in caplog.text


def test_benchmark_uses_isolated_indexed_smoke_data():
    spec = importlib.util.spec_from_file_location("memory_bench", ROOT / "scripts/bench/memory_bench.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    result = bench.sqlite_bench(10002, 2)
    assert result["rows"] == 10002 and result["profile"]["p95_ms"] >= 0
    assert result["search"]["p95_ms"] >= 0
    assert any("VIRTUAL TABLE INDEX" in row for row in result["plan"])
