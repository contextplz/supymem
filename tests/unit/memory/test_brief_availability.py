"""Whole venture memory must not become false absence or clipped constraints."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from tests.company_os_harness import load_company_os_app
from tests.unit.memory.test_facts import facts as facts


@pytest.fixture
def memory_app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/memory.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()


def test_recall_unavailable_is_not_none_saved(memory_app, monkeypatch, caplog):
    from app.ventures import founder_memory as memory
    from app.ventures.state_brief import render_brief_for_llm
    class Failing:
        async def context_pack(self, **kwargs):
            raise OSError("test shared-store failure")
    monkeypatch.setattr(memory, "get_memory", lambda: Failing())
    section = memory.memory_section_for_brief("ven_owned")
    assert section["availability"] == "unavailable"
    text = render_brief_for_llm({"memory": section})
    assert "memory" not in text.lower()
    assert "Venture memory: unavailable" not in text
    assert "memory_recall_failed" in caplog.text
    assert "test shared-store failure" not in caplog.text
    assert "Founder preferences: none saved" not in text


def test_whole_constraint_retained_or_explicitly_omitted(memory_app, monkeypatch):
    from app.memory.model import ContextPack, MemoryHit, MemoryKind
    from app.ventures import founder_memory as memory
    from app.ventures.state_brief import render_brief_for_llm, BRIEF_CHAR_BUDGET
    statement = "Never publish without current founder approval; " + "完整的约束 " * 800
    hit = MemoryHit(mem_id="mem_long", venture_id="ven_owned", kind=MemoryKind.CONSTRAINT,
                    subject="publishing", statement=statement, evidence=["chat:msg_owned"],
                    author="usr_founder", confidence=1, decayed_confidence=1,
                    valid_from=datetime.now(timezone.utc))
    class Port:
        async def context_pack(self, **kwargs):
            # Include an oversized whole preference even when an old/local port
            # ignores its budget: the consumer must still enforce its own bound.
            return ContextPack(venture_id="ven_owned", memories=[hit])
    monkeypatch.setattr(memory, "get_memory", lambda: Port())
    section = memory.memory_section_for_brief("ven_owned")
    assert section["constraints"] == []
    assert section["omitted_count"] == 1
    assert section["omitted_memory_ids"] == [hit.mem_id]
    text = render_brief_for_llm({"memory": section})
    assert "omitted 1 complete items" in text and "Founder preferences: none saved" not in text
    assert statement[:100] not in text and len(text) <= BRIEF_CHAR_BUDGET


def test_foreign_memory_pack_is_unavailable(memory_app, monkeypatch):
    from app.memory.model import ContextPack
    from app.ventures import founder_memory as memory
    class Port:
        async def context_pack(self, **kwargs):
            return ContextPack(venture_id="ven_other")
    monkeypatch.setattr(memory, "get_memory", lambda: Port())
    assert memory.memory_section_for_brief("ven_owned")["availability"] == "unavailable"


def test_insight_brief_retains_only_whole_current_source_bound_facts(memory_app, monkeypatch):
    from app.memory.model import ContextPack, MemoryHit, MemoryKind
    from app.ventures import founder_memory as memory
    current = MemoryHit(mem_id="mem_current", venture_id="ven_owned", kind=MemoryKind.INSIGHT,
                        subject="idea.audience", statement="Our customers are independent local bookshops.",
                        evidence=["chat:msg_owned"], author="usr_founder", confidence=1,
                        decayed_confidence=1, valid_from=datetime.now(timezone.utc))
    retired = current.model_copy(update={"mem_id": "mem_old", "superseded_by": current.mem_id})
    unlinked = current.model_copy(update={"mem_id": "mem_unlinked", "evidence": []})
    oversized = current.model_copy(update={"mem_id": "mem_large", "statement": "完整的客户事实 " * 800})
    class Port:
        async def context_pack(self, **kwargs):
            return ContextPack(venture_id="ven_owned", memories=[retired, unlinked, oversized, current])
    monkeypatch.setattr(memory, "get_memory", lambda: Port())
    section = memory.memory_section_for_brief("ven_owned")
    assert section["availability"] == "available"
    assert [row["id"] for row in section["other"]] == [current.mem_id]
    assert section["other"][0]["statement"] == current.statement
    assert section["other"][0]["evidence"] == current.evidence
    assert section["other"][0]["author"] == current.author
    assert section["omitted_count"] == 3
    assert section["omitted_memory_ids"] == ["mem_large", "mem_old", "mem_unlinked"]


def test_legacy_insight_prompt_reports_keep_source_and_never_promote_outcomes(memory_app):
    from app.ventures.state_brief import render_brief_for_llm
    section = {"availability": "available", "other": [
        {"kind": "insight", "statement": "Our customers are independent bookshops.",
         "evidence": ["chat:msg_owned"], "author": "usr_founder"},
        {"kind": "outcome", "statement": "The business has ten verified sales.",
         "evidence": ["publication:dep_fixture"], "author": "system"},
        {"kind": "insight", "statement": "Unlinked assertion", "evidence": [], "author": "usr_founder"},
    ]}
    rendered = render_brief_for_llm({"memory": section})
    assert "Our customers are independent bookshops." in rendered
    assert "sources: chat:msg_owned" in rendered and "current idea and controls win" in rendered
    assert "The business has ten verified sales" not in rendered and "Unlinked assertion" not in rendered
    assert "Founder reports omitted 1 complete items" in rendered


def test_legacy_insight_prompt_omits_whole_oversized_report(memory_app):
    from app.ventures.state_brief import render_brief_for_llm
    statement = "Our audience is " + "teachers " * 200
    rendered = render_brief_for_llm({"memory": {"availability": "available", "other": [
        {"kind": "insight", "statement": statement, "evidence": ["chat:msg_owned"], "author": "usr_founder"},
    ]}})
    assert statement[:100] not in rendered and "Founder reports omitted 1 complete items" in rendered


def test_settings_lists_all_founder_kinds_without_lexical_search(memory_app, monkeypatch):
    from app.memory.model import MemoryHit, MemoryKind
    from app.ventures import founder_memory as memory
    kinds = {MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION, MemoryKind.DECISION,
             MemoryKind.CONSTRAINT, MemoryKind.INSIGHT}
    hits = [MemoryHit(mem_id=f"mem_{kind.value}", venture_id="ven_owned", kind=kind,
                      subject="idea.audience", statement="Our customers are independent local bookshops.",
                      evidence=["chat:msg_owned"], author="usr_founder", confidence=1,
                      decayed_confidence=1, valid_from=datetime.now(timezone.utc)) for kind in kinds]
    calls = []
    class Port:
        async def recall(self, query):
            calls.append(query)
            return [hit for hit in hits if hit.kind in query.kinds]
    monkeypatch.setattr(memory, "get_memory", lambda: Port())
    rows = memory.list_founder_memories("ven_owned")
    assert {row["kind"] for row in rows} == {kind.value for kind in kinds}
    assert calls[-1].text == "" and set(calls[-1].kinds) == kinds
    assert {row["author"] for row in rows} == {"usr_founder"}
    assert all(row["evidence"] == ["chat:msg_owned"] for row in rows)
    assert all(row["sources"] == [{"ref": "chat:msg_owned"}] for row in rows)
    assert [row["kind"] for row in memory.list_founder_memories("ven_owned", kinds=["insight"])] == ["insight"]


@pytest.fixture
def settings_facts(facts, monkeypatch):
    """Actual SQLite fact/source storage plus an already-qualified legacy port."""
    from app import state
    from app.memory.model import MemoryHit, MemoryKind
    from app.ventures import founder_memory as memory
    d = facts
    d.message("msg_legacy", "Remember: I prefer short answers.")
    legacy = MemoryHit(mem_id="mem_legacy", venture_id="ven_owned", kind=MemoryKind.FOUNDER_PREFERENCE,
                       subject="general", statement="I prefer short answers.", evidence=["chat:msg_legacy"],
                       author="usr_alice", confidence=1, decayed_confidence=1, valid_from=d.when)
    class Port:
        async def recall(self, query):
            return [legacy] if legacy.kind in query.kinds else []
        async def timeline(self, **kwargs):
            return [legacy]
    monkeypatch.setattr(memory, "get_memory", lambda: Port())
    monkeypatch.setattr(state, "db", lambda: d.factory)
    return d, memory, legacy


def test_settings_adds_only_current_owned_facts_with_source_and_respects_kind_filter(settings_facts):
    from app.db.models import ChatConversation
    d, memory, legacy = settings_facts
    before = d.message("msg_before", "Our audience is teachers.", day=1)
    latest = d.message("msg_latest", "Our audience is nurses.", day=2)
    with d.bind():
        prior_id = d.write(before)
        current_id = d.write(latest)
    with d.factory() as session:
        session.add(ChatConversation(id="cnv_bob", workspace_id="wsp_owned", venture_id="ven_owned",
                                    creator_person_id="usr_bob", visibility="private", title="Private",
                                    creation_key="bob", created_at=d.when, updated_at=d.when))
        session.commit()
    other_source = d.message("msg_bob", "Our audience is dentists.", day=3,
                             author_person_id="usr_bob", conversation_id="cnv_bob")
    with d.bind(actor="usr_bob"):
        other_id = d.write(other_source)
    with d.bind():
        rows = memory.list_founder_memories("ven_owned")
        assert [row["id"] for row in rows] == [current_id, legacy.mem_id]
        fact = rows[0]
        assert fact["kind"] == "insight" and fact["statement"] == latest["quote"]
        assert fact["author"] == "usr_alice" and fact["source"] == latest
        assert fact["evidence"] == [latest["ref"]] and fact["sources"] == [latest]
        assert fact["valid_to"] is None and fact["effect_authority"] is False
        assert prior_id not in {row["id"] for row in rows} and other_id not in {row["id"] for row in rows}
        assert memory.list_founder_memories("ven_owned", limit=1)[0]["id"] == current_id
        assert [row["id"] for row in memory.list_founder_memories("ven_owned", kinds=["founder_preference"])] == [legacy.mem_id]
    with d.bind(actor="usr_bob"):
        assert [row["id"] for row in memory.list_founder_memories("ven_owned") if row["id"].startswith("fact_")] == [other_id]


def test_settings_fact_store_failure_logs_no_content_and_preserves_legacy(settings_facts, caplog):
    from sqlalchemy import text
    d, memory, legacy = settings_facts
    with d.factory() as session:
        session.execute(text("DROP TABLE venture_facts"))
        session.commit()
    with d.bind():
        status = {}
        assert [row["id"] for row in memory.list_founder_memories("ven_owned", status=status)] == [legacy.mem_id]
        assert status == {"availability": "partial", "conflict_count": 0}
    records = [record for record in caplog.records if record.message == "memory_fact_list_failed"]
    assert len(records) == 1 and records[0].error_class == "OperationalError"
    assert "Our audience" not in caplog.text and "SELECT" not in caplog.text


def test_legacy_retrieval_failure_reports_unavailable_or_partial_without_old_rows(legacy_fact_store, monkeypatch, caplog):
    from app.ventures import founder_memory
    d, adapter, _ = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    monkeypatch.setattr(adapter, "_records", lambda *args: (_ for _ in ()).throw(OSError("private source content")))
    with d.bind():
        status = {}
        assert founder_memory.list_founder_memories("ven_owned", status=status) == []
        assert status == {"availability": "unavailable", "conflict_count": 0}
        identity = d.write(d.message("available_auto", "Our audience is bookshops."))
        status = {}
        rows = founder_memory.list_founder_memories("ven_owned", status=status)
        assert [row["id"] for row in rows] == [identity]
        assert status == {"availability": "partial", "conflict_count": 0}
    records = [record for record in caplog.records if record.message == "memory_legacy_list_failed"]
    assert len(records) == 2 and all(record.error_class == "OSError" for record in records)
    assert "private source content" not in caplog.text


def test_settings_forget_dispatches_owned_facts_without_deleting_source_or_history(settings_facts, monkeypatch):
    d, memory, _ = settings_facts
    source = d.message("msg_current", "Our audience is teachers.", day=1)
    invalidated = []
    monkeypatch.setattr(memory, "invalidate_venture_state_brief", invalidated.append)
    with d.bind():
        identity = d.write(source)
    with pytest.raises(KeyError):
        memory.forget_founder_memory("ven_owned", identity)
    with d.bind(actor="usr_bob"), pytest.raises(KeyError):
        memory.forget_founder_memory("ven_owned", identity)
    with d.bind():
        memory.forget_founder_memory("ven_owned", identity)
        assert d.read() == [] and d.write(source) == identity
        assert not any(row["id"] == identity for row in memory.list_founder_memories("ven_owned"))
    with d.factory() as session:
        row = session.get(d.Fact, identity)
        assert row.forgotten_at and row.value == source["quote"] and row.source_json
        assert session.get(d.Message, "msg_current").body_md == source["quote"]
    assert invalidated == ["ven_owned"]


@pytest.fixture
def legacy_fact_store(facts, monkeypatch):
    """Use the accepted legacy adapter against the same actual source database."""
    import app.memory
    import app.state
    from app.db.models import Base
    from app.memory.adapters import postgres
    from app.memory.model import MemoryKind, MemoryWrite
    d = facts
    Base.metadata.create_all(d.factory.kw["bind"], tables=[Base.metadata.tables["institutional_memories"]])
    monkeypatch.setattr(app.state, "db", lambda: d.factory)
    monkeypatch.setattr(postgres, "db", lambda: d.factory)
    adapter = postgres.PostgresMemoryAdapter()
    monkeypatch.setattr(app.memory, "get_memory", lambda: adapter)

    def write(identity, statement, *, day, previous=None):
        body = f"Correct memory {previous}: {statement}" if previous else "Remember: " + statement
        source = d.message(identity, body, day=day)
        value = MemoryWrite(venture_id="ven_owned", kind=MemoryKind.INSIGHT, subject="general",
                            statement=statement, evidence=[source["ref"]], author="usr_alice",
                            confidence=1, dedupe_key=identity, valid_from=d.when + timedelta(days=6))
        result = asyncio.run(adapter.supersede(previous, by=value, reason="explicit correction") if previous
                             else adapter.remember(value))
        return result

    return d, adapter, write


def test_settings_and_adapter_cannot_read_forget_or_replace_another_founders_memory(legacy_fact_store, monkeypatch):
    from app.db.models import InstitutionalMemory
    from app.memory.model import MemoryRecord, RecallQuery, MemoryKind, MemoryWrite
    from app.ventures import founder_memory
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.bind():
        identity = write("msg_owner", "Our audience is teachers.", day=1)
    source = d.message("msg_other", "Remember: Our audience is dentists.", day=2, author_person_id="usr_bob")
    with d.bind(actor="usr_bob"):
        assert asyncio.run(adapter.recall(RecallQuery(venture_id="ven_owned", text=""))) == []
        with pytest.raises(KeyError):
            founder_memory.forget_founder_memory("ven_owned", identity)
        replacement = MemoryWrite(venture_id="ven_owned", kind=MemoryKind.INSIGHT, subject="general",
            statement=source["quote"].removeprefix("Remember: "), evidence=[source["ref"]], author="usr_bob",
            confidence=1, dedupe_key="other-replacement")
        with pytest.raises(PermissionError):
            asyncio.run(adapter.supersede(identity, by=replacement, reason="unowned replacement"))
    with d.factory() as session:
        saved = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, identity).record_json)
        assert not saved.tombstoned and saved.valid_to is None


@pytest.mark.parametrize("operation", ["correct", "forget"])
@pytest.mark.parametrize("field,value", [("mem_id", "mem_mismatched"), ("venture_id", "ven_mismatched")])
@pytest.mark.parametrize("facts_enabled", [False, True])
def test_mutations_reject_changed_record_identity_without_writing(legacy_fact_store, monkeypatch, operation, field, value, facts_enabled):
    from sqlalchemy import func, select
    from app.config import settings
    from app.db.models import InstitutionalMemory
    from app.memory.model import MemoryKind, MemoryRecord, MemoryWrite
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", facts_enabled)
    with d.bind():
        identity = write("msg_identity", "Our audience is teachers.", day=1)
        command = d.message("msg_identity_command", f"Correct memory {identity}: Our audience is dentists."
                            if operation == "correct" else f"Forget memory {identity}", day=2)
        with d.factory() as session:
            row = session.get(InstitutionalMemory, identity)
            record = MemoryRecord.model_validate_json(row.record_json)
            setattr(record, field, value)
            before = record.model_dump_json()
            row.record_json = before
            session.commit()
        with pytest.raises(ValueError, match="memory_" + ("correction" if operation == "correct" else "forget") + "_source_changed"):
            if operation == "correct":
                replacement = MemoryWrite(venture_id="ven_owned", kind=MemoryKind.INSIGHT, subject="general",
                    statement="Our audience is dentists.", evidence=[command["ref"]], author="usr_alice",
                    confidence=1, dedupe_key="identity-new")
                asyncio.run(adapter.supersede(identity, by=replacement, reason="explicit command"))
            else:
                asyncio.run(adapter.forget(venture_id="ven_owned", mem_id=identity, reason="explicit command",
                                           source_message_id=command["ref"].removeprefix("chat:")))
        with d.factory() as session:
            assert session.get(InstitutionalMemory, identity).record_json == before
            assert session.scalar(select(func.count(InstitutionalMemory.id))) == 1


def test_default_off_facts_leave_legacy_context_available_without_fact_table(legacy_fact_store, monkeypatch):
    from sqlalchemy import text
    from app.config import settings
    from app.chat.memory_commands import fact_reply
    from app.memory.context import context_for
    from app.ventures import founder_memory
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.bind():
        identity = write("msg_legacy_only", "Our audience is teachers.", day=1)
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", False)
    with d.factory() as session:
        session.execute(text("DROP TABLE venture_facts"))
        session.commit()
    with d.bind(), d.factory() as session:
        status = {}
        assert [row["id"] for row in founder_memory.list_founder_memories("ven_owned", status=status)] == [identity]
        context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned", purpose="plan.compile", query="audience")
        assert context["availability"] == status["availability"] == "available"
        assert context["facts"] == [] and context["statements"][0]["statement"] == "Our audience is teachers."
        assert "teachers" in fact_reply("ven_owned", "idea.audience")["body_md"]


def test_local_brief_cache_never_reuses_another_founders_memory(facts, monkeypatch):
    from app.tenancy import current
    from app.ventures import state_brief
    d = facts
    calls = []
    def assemble(venture_id):
        calls.append(current().actor)
        return {"memory": {"author": current().actor}}
    monkeypatch.setattr(state_brief, "db", lambda: d.factory)
    monkeypatch.setattr(state_brief, "assemble_venture_state_brief", assemble)
    state_brief.invalidate_venture_state_brief()
    for actor in ("usr_alice", "usr_bob", "usr_alice"):
        with d.bind(actor=actor):
            assert state_brief.get_venture_state_brief("ven_owned")["memory"]["author"] == actor
    assert calls == ["usr_alice", "usr_bob"]
    state_brief.invalidate_venture_state_brief("ven_owned")
    for actor in ("usr_alice", "usr_bob"):
        with d.bind(actor=actor):
            assert state_brief.get_venture_state_brief("ven_owned")["memory"]["author"] == actor
    assert calls == ["usr_alice", "usr_bob", "usr_alice", "usr_bob"]


def test_explicit_remember_dedupe_binds_actor_and_source_not_just_statement(legacy_fact_store):
    from app.gbrain.verbs import remember
    d, adapter, _ = legacy_fact_store
    ids = []
    for actor, source_id, conversation_id in (("usr_alice", "msg_alice", "cnv_owned"),
                                             ("usr_bob", "msg_bob", "cnv_bob"),
                                             ("usr_alice", "msg_again", "cnv_owned")):
        if actor == "usr_bob":
            with d.factory() as session:
                session.add(d.Conversation(id=conversation_id, workspace_id="wsp_owned", venture_id="ven_owned",
                    creator_person_id=actor, visibility="private", title="Private", creation_key=conversation_id,
                    created_at=d.when, updated_at=d.when))
                session.commit()
        source = d.message(source_id, "Remember: Our audience is teachers.",
                           conversation_id=conversation_id, author_person_id=actor)
        with d.bind(actor=actor):
            result = asyncio.run(remember(venture_id="ven_owned", fact="Our audience is teachers.",
                                         provenance=source["ref"]))
            replay = asyncio.run(remember(venture_id="ven_owned", fact="Our audience is teachers.",
                                         provenance=source["ref"]))
            assert result["id"] == replay["id"]
            ids.append(result["id"])
    assert len(set(ids)) == 3


def test_natural_recall_combines_explicit_and_auto_facts_by_source_event_time(legacy_fact_store):
    from app.chat.memory_commands import fact_reply
    d, _, write = legacy_fact_store
    with d.bind():
        original = write("legacy_first", "Our audience is teachers.", day=1)
        assert "teachers" in fact_reply("ven_owned", "idea.audience")["body_md"]
        automatic = d.message("automatic", "Our audience is nurses.", day=2)
        d.write(automatic)
        # The older explicit memory's later write time cannot outrank the source.
        response = fact_reply("ven_owned", "idea.audience")
        assert "nurses" in response["body_md"] and "teachers" not in response["body_md"]
        write("legacy_correction", "Our audience is dentists.", day=3, previous=original)
        response = fact_reply("ven_owned", "idea.audience")
        assert "dentists" in response["body_md"] and "nurses" not in response["body_md"]
        assert response["citations"][0]["source_ref"] == "chat:legacy_correction"
        history = fact_reply("ven_owned", "idea.audience", history=True)
        assert history["body_md"].index("teachers") < history["body_md"].index("nurses") < history["body_md"].index("dentists")
        assert {citation["source_ref"] for citation in history["citations"]} == {
            "chat:legacy_first", "chat:automatic", "chat:legacy_correction"}
        assert all(citation["href"] == "/v/ven_owned?chat=1" and citation["label"] == "Open conversation"
                   for citation in history["citations"])


def test_prompt_latest_correction_wins_across_explicit_and_automatic_stores(legacy_fact_store, monkeypatch):
    from app.chat.memory_commands import fact_reply
    from app.memory.context import context_for
    from app.memory import task_context
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(task_context, "get_memory", lambda: adapter)
    with d.bind():
        original = write("prompt_first", "Our audience is teachers.", day=1)
        d.write(d.message("prompt_auto", "Our audience is nurses.", day=2))
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                                  purpose="plan.compile", query="audience")
        assert [row["statement"] for row in context["facts"]] == ["Our audience is nurses."]
        assert all("teachers" not in row["statement"] for row in context["statements"])
        write("prompt_later", "Our audience is dentists.", day=3, previous=original)
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                                  purpose="plan.compile", query="audience")
        assert context["facts"] == []
        assert [row["statement"] for row in context["statements"]] == ["Our audience is dentists."]
    with d.bind(actor="usr_bob"):
        assert "don't have a source-backed statement" in fact_reply("ven_owned", "idea.audience")["body_md"]
    with d.factory() as session:
        session.get(d.Conversation, "cnv_owned").creator_person_id = "usr_bob"
        session.commit()
    with d.bind():
        assert "forgotten, withdrawn or cannot be verified" in fact_reply("ven_owned", "idea.audience")["body_md"]


def test_natural_recall_abstains_when_equal_source_times_conflict(legacy_fact_store):
    from app.chat.memory_commands import fact_reply
    d, _, write = legacy_fact_store
    with d.bind():
        write("legacy_same_time", "Our audience is teachers.", day=1)
        d.write(d.message("auto_same_time", "Our audience is nurses.", day=1))
        response = fact_reply("ven_owned", "idea.audience")
        assert "same source time and conflict" in response["body_md"]
        assert "teachers" not in response["body_md"] and "nurses" not in response["body_md"]


def test_newer_oversized_source_shadows_old_prompt_and_current_settings_before_budget(legacy_fact_store, monkeypatch):
    from app.memory.context import context_for
    from app.ventures import founder_memory
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    long = ("Our audience is architects. " + "Complete source detail. " * 400).strip()
    with d.bind():
        old = d.write(d.message("budget_old", "Our audience is nurses.", day=2))
        latest = write("budget_latest", long, day=3)
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                                  purpose="plan.compile", query="audience", budget_bytes=2000)
        assert context["facts"] == context["statements"] == []
        assert context["availability"] == "available" and context["omitted_count"] == 1
        assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()) <= 2000
        assert [row["id"] for row in founder_memory.list_founder_memories("ven_owned")] == [latest]
        assert len(d.read(include_history=True)) == 1 and d.read(include_history=True)[0]["id"] == old


def test_prompt_and_current_settings_hide_equal_event_conflicts_with_honest_status(legacy_fact_store, monkeypatch):
    from app.memory.context import context_for
    from app.ventures import founder_memory
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.bind():
        write("tie_explicit", "Our audience is teachers.", day=1)
        d.write(d.message("tie_auto", "Our audience is nurses.", day=1))
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned", purpose="plan.compile", query="audience")
        assert context["facts"] == context["statements"] == []
        assert context["conflict_count"] == 1 and "Ask which statement to use" in context["grounding"]
        status = {}
        assert founder_memory.list_founder_memories("ven_owned", status=status) == []
        assert status == {"availability": "conflict", "conflict_count": 1}
        assert len(d.read(include_history=True)) == 1


def test_complete_legacy_scan_keeps_latest_source_beyond_old_recall_page(legacy_fact_store, monkeypatch):
    from app.ventures import founder_memory
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.bind():
        old = d.write(d.message("page_auto", "Our audience is nurses.", day=1))
        latest = write("page_explicit", "Our audience is architects.", day=2)
        for index in range(105):
            write("unrelated_" + str(index), "A separate saved note " + str(index) + ".", day=3 + index / 1000)
        rows = founder_memory.list_founder_memories("ven_owned", limit=100)
        assert all(row["id"] != old for row in rows)
        from app.chat.memory_commands import fact_reply
        assert "architects" in fact_reply("ven_owned", "idea.audience")["body_md"]
        from app.memory.current import legacy_source_hits, select_current_reports
        with d.factory() as session:
            selected = select_current_reports(session, venture_id="ven_owned", legacy_hits=legacy_source_hits(adapter, "ven_owned"))
        assert selected["facts"] == [] and latest in {row.mem_id for row in selected["legacy"]}


def test_natural_recall_omits_whole_oversize_quotes_with_bounded_source_reply(facts, monkeypatch):
    import app.state
    from app.chat.memory_commands import fact_reply, LIST_BYTES
    monkeypatch.setattr(app.state, "db", lambda: facts.factory)
    source = facts.message("long_recall", "Our audience is " + "international bookshops " * 600)
    with facts.bind():
        facts.write(source)
        response = fact_reply("ven_owned", "idea.audience")
        assert response["omitted_count"] == 1 and "Omitted 1 whole source statements" in response["body_md"]
        assert "international bookshops" not in response["body_md"]
        assert len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()) <= LIST_BYTES
        assert facts.read()[0]["value"] == source["quote"]


@pytest.mark.parametrize("newer_store", ["automatic", "legacy"])
@pytest.mark.parametrize("withdrawal", ["source_changed", "forgotten"])
def test_newest_invalid_or_forgotten_cross_store_source_blocks_all_current_readers(
        legacy_fact_store, monkeypatch, newer_store, withdrawal):
    from app.chat.memory_commands import fact_reply
    from app.memory.context import context_for
    from app.ventures import founder_memory
    from core import clock
    d, adapter, write = legacy_fact_store
    now = [d.when + timedelta(days=10)]
    monkeypatch.setattr(clock, "_clock", lambda: now[0])
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.bind():
        if newer_store == "automatic":
            older = write("barrier_older", "Our audience is teachers.", day=1)
            newer = d.write(d.message("barrier_newer", "Our audience is nurses.", day=2))
        else:
            older = d.write(d.message("barrier_older", "Our audience is teachers.", day=1))
            newer = write("barrier_newer", "Our audience is nurses.", day=2)
        if withdrawal == "source_changed":
            with d.factory() as session:
                session.get(d.Message, "barrier_newer").body_md = "This source has been withdrawn."
                session.commit()
        elif newer_store == "automatic":
            with d.factory() as session:
                d.api.forget_fact(session, venture_id="ven_owned", fact_id=newer)
                session.commit()
        else:
            asyncio.run(adapter.forget(venture_id="ven_owned", mem_id=newer, reason="forget newer topic"))
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                                  purpose="plan.compile", query="audience")
        assert context["facts"] == context["statements"] == []
        assert context["availability"] == "unavailable" and context["blocked_count"] == 1
        status = {}
        assert founder_memory.list_founder_memories("ven_owned", status=status) == []
        assert status == {"availability": "unavailable", "conflict_count": 0, "blocked_count": 1}
        reply = fact_reply("ven_owned", "idea.audience")
        assert "forgotten, withdrawn or cannot be verified" in reply["body_md"]
        assert "teachers" not in reply["body_md"] and "nurses" not in reply["body_md"]
        history = fact_reply("ven_owned", "idea.audience", history=True)
        assert "teachers" in history["body_md"] and "Earlier statement" in history["body_md"]
        assert "nurses" not in history["body_md"]
        # A later source can establish the topic after a proven cutoff.
        now[0] = d.when + timedelta(days=12)
        fresh = write("barrier_after", "Our audience is dentists.", day=11)
        assert "dentists" in fact_reply("ven_owned", "idea.audience")["body_md"]
        assert [row["id"] for row in founder_memory.list_founder_memories("ven_owned")] == [fresh]
        with d.factory() as session:
            assert session.get(d.Message, "barrier_older") and session.get(d.Message, "barrier_newer")
        assert older != newer


@pytest.mark.parametrize("stamp", [None, "broken timestamp", "2026-10-03T00:00:00", "2060-01-01T00:00:00+00:00", 17])
def test_old_tombstone_without_proven_time_conservatively_blocks_older_reports(legacy_fact_store, monkeypatch, stamp):
    from app.db.models import InstitutionalMemory
    from app.memory.model import MemoryRecord
    from app.chat.memory_commands import fact_reply
    from app.ventures import founder_memory
    d, adapter, write = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.bind():
        d.write(d.message("old_stamp_auto", "Our audience is teachers.", day=1))
        identity = write("old_stamp_legacy", "Our audience is nurses.", day=2)
        with d.factory() as session:
            row = session.get(InstitutionalMemory, identity)
            saved = MemoryRecord.model_validate_json(row.record_json)
            saved.tombstoned = True
            saved.metadata["forgotten_at"] = stamp
            row.record_json = saved.model_dump_json()
            session.commit()
        assert founder_memory.list_founder_memories("ven_owned") == []
        assert "teachers" not in fact_reply("ven_owned", "idea.audience")["body_md"]
        # Ordinary repeat forget cannot fabricate a missing historical cutoff.
        asyncio.run(adapter.forget(venture_id="ven_owned", mem_id=identity, reason="repeat"))
        with d.factory() as session:
            saved = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, identity).record_json)
            assert saved.metadata["forgotten_at"] == stamp


def test_forget_timestamp_is_server_owned_stable_and_new_source_must_follow_it(legacy_fact_store, monkeypatch):
    from app.db.models import InstitutionalMemory
    from app.memory.model import MemoryRecord
    from app.chat.memory_commands import fact_reply
    from core import clock
    d, adapter, write = legacy_fact_store
    now = [d.when + timedelta(days=10)]
    monkeypatch.setattr(clock, "_clock", lambda: now[0])
    with d.bind():
        identity = write("stamp_original", "Our audience is teachers.", day=1)
        with d.factory() as session:
            row = session.get(InstitutionalMemory, identity)
            saved = MemoryRecord.model_validate_json(row.record_json)
            saved.metadata["forgotten_at"] = "2000-01-01T00:00:00+00:00"
            row.record_json = saved.model_dump_json()
            session.commit()
        asyncio.run(adapter.forget(venture_id="ven_owned", mem_id=identity, reason="first"))
        with d.factory() as session:
            row = session.get(InstitutionalMemory, identity)
            saved = MemoryRecord.model_validate_json(row.record_json)
            stamp, version = saved.metadata["forgotten_at"], row.version
            assert stamp == now[0].isoformat() and saved.tombstoned
        # An event at the forget instant is not a proven later statement.
        d.write(d.message("stamp_equal", "Our audience is nurses.", day=10))
        assert "nurses" not in fact_reply("ven_owned", "idea.audience")["body_md"]
        now[0] += timedelta(days=2)
        asyncio.run(adapter.forget(venture_id="ven_owned", mem_id=identity, reason="repeat"))
        with d.factory() as session:
            row = session.get(InstitutionalMemory, identity)
            saved = MemoryRecord.model_validate_json(row.record_json)
            assert saved.metadata["forgotten_at"] == stamp and row.version == version + 1
            assert saved.tombstone_reason == "repeat"
        d.write(d.message("stamp_after", "Our audience is dentists.", day=11))
        assert "dentists" in fact_reply("ven_owned", "idea.audience")["body_md"]


def test_forget_timestamp_and_tombstone_rollback_together(legacy_fact_store):
    from sqlalchemy import event
    from app.db.models import InstitutionalMemory
    from app.memory.model import MemoryRecord
    d, adapter, write = legacy_fact_store
    with d.bind():
        identity = write("stamp_rollback", "Our audience is teachers.", day=1)
        with d.factory() as session:
            original = session.get(InstitutionalMemory, identity).record_json
        def fail_commit(session):
            raise RuntimeError("transaction failed")
        event.listen(d.factory.class_, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="transaction failed"):
                asyncio.run(adapter.forget(venture_id="ven_owned", mem_id=identity, reason="fails"))
        finally:
            event.remove(d.factory.class_, "before_commit", fail_commit)
        with d.factory() as session:
            row = session.get(InstitutionalMemory, identity)
            saved = MemoryRecord.model_validate_json(row.record_json)
            assert row.record_json == original and not saved.tombstoned and "forgotten_at" not in saved.metadata


@pytest.mark.parametrize("visibility", ["private", "legacy_shared"])
def test_my_recall_never_selects_or_shadows_with_another_founders_statement(legacy_fact_store, monkeypatch, visibility):
    from app.memory.context import context_for
    from app.ventures import founder_memory
    from app.chat.memory_commands import fact_reply
    from app.memory.model import MemoryKind, MemoryWrite
    d, adapter, _ = legacy_fact_store
    monkeypatch.setattr(founder_memory, "get_memory", lambda: adapter)
    with d.factory() as session:
        session.add(d.Conversation(id="cnv_shared", workspace_id="wsp_owned", venture_id="ven_owned",
            creator_person_id="usr_bob" if visibility == "private" else None,
            visibility=visibility, title="Shared", creation_key="shared",
            created_at=d.when, updated_at=d.when))
        session.commit()
    source = d.message("bob_shared", "Remember: Our audience is dentists.", day=3,
                       conversation_id="cnv_shared", author_person_id="usr_bob")
    with d.bind(actor="usr_bob"):
        asyncio.run(adapter.remember(MemoryWrite(venture_id="ven_owned", kind=MemoryKind.INSIGHT,
            subject="general", statement="Our audience is dentists.", evidence=[source["ref"]],
            author="usr_bob", confidence=1, dedupe_key="bob-shared")))
    with d.bind():
        d.write(d.message("alice_owned", "Our audience is teachers.", day=1))
        assert "teachers" in fact_reply("ven_owned", "idea.audience")["body_md"]
        assert "dentists" not in fact_reply("ven_owned", "idea.audience", history=True)["body_md"]
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned", purpose="plan.compile", query="audience")
        rows = founder_memory.list_founder_memories("ven_owned")
        assert context["statements"] == [] and all(row["author"] != "usr_bob" for row in rows)
    with d.factory() as session:
        session.get(d.Message, "bob_shared").body_md = "Source withdrawn."
        session.commit()
    with d.bind():
        assert "teachers" in fact_reply("ven_owned", "idea.audience")["body_md"]
    with d.bind(actor="usr_bob"):
        reply = fact_reply("ven_owned", "idea.audience")
        assert "forgotten, withdrawn or cannot be verified" in reply["body_md"] and "teachers" not in reply["body_md"]
