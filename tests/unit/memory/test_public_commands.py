import asyncio
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules
from tests.unit.chat.test_chat_request_status import chat_request


@pytest.fixture(autouse=True)
def company_memory():
    with service_modules("company_os"):
        yield


@pytest.mark.parametrize("text,action", [
    ("List saved venture memories", "memory.list"),
    ("Remember that I prefer complete notes; do not publish.", "memory.remember"),
    ("Correct memory mem_exact: I prefer complete notes; never send.", "memory.correct"),
    ("Forget memory mem_exact", "memory.forget"),
    ("Forget memory mem_exact; send an email", "memory.help"),
])
def test_exact_memory_command_preserves_whole_statement_and_skips_work_split(text, action):
    from app.chat.intent import classify_intent, split_speech_acts
    assert classify_intent(text) == action
    assert split_speech_acts(text) == [(action, text)]


@pytest.mark.parametrize("text", [
    "Do you remember my preference?", "Do not forget memory mem_exact",
    "How do I correct a saved preference?", "Forget everything about all ventures",
])
def test_question_prohibition_or_fuzzy_memory_target_is_not_a_mutation(text):
    from app.chat.memory_commands import parse
    assert parse(text) is None


@pytest.mark.parametrize("prefix", [
    "Remember:", "Please remember:", "Remember that:", "Please remember this:",
    "Remember that", "Please remember this",
])
def test_common_explicit_remember_syntax_keeps_complete_statement(prefix):
    from app.chat.intent import classify_intent, split_speech_acts
    from app.chat.memory_commands import parse
    statement = 'For this venture, I prefer concise labels, including "Why?", and complete saved statements.'
    raw = prefix + " " + statement
    command = parse(raw)
    assert command.action == "memory.remember" and command.statement == statement
    assert classify_intent(raw) == "memory.remember"
    assert split_speech_acts(raw) == [("memory.remember", raw)]


@pytest.mark.parametrize("statement", [
    "This venture prefers complete statements.", "That plan remains unapproved.",
    "I prefer labels ending in ?", 'The label "How?" should stay complete.',
])
def test_colon_preserves_whole_statement_including_demonstratives_and_question_punctuation(statement):
    from app.chat.memory_commands import parse
    assert parse("Remember: " + statement).statement == statement


@pytest.mark.parametrize("raw", [
    "Remember:", "Please remember", "Remember that:", "Please remember this:",
    "Remember:   ", "Remember:: complete labels", "Remember?", "Remember what did I say?",
])
def test_malformed_or_question_shaped_explicit_remember_is_guidance(raw):
    from app.chat.intent import classify_intent
    from app.chat.memory_commands import parse
    assert parse(raw).action == "memory.help"
    assert classify_intent(raw) == "memory.help"


@pytest.mark.parametrize("raw", [
    "Please do not remember this preference.", "Do you remember: my preference?",
    "Please don't remember that statement.", "Remembering the old launch is useful.",
])
def test_common_syntax_does_not_expand_negation_or_question_authority(raw):
    from app.chat.memory_commands import parse
    assert parse(raw) is None


def test_unbound_command_source_refuses_before_database_or_write():
    from app.chat.memory_commands import dispatch
    reply = asyncio.run(dispatch("ven_unbound", "msg_unbound"))
    assert "No memory was changed" in reply["body_md"]
    assert reply["actions"] == [] and reply["citations"] == []


@pytest.mark.parametrize("statement", ["Invoice reports use fiscal quarters.", "Garden reminders use the local season."])
def test_local_copy_registered_memory_action_requires_its_exact_owned_source(monkeypatch, statement):
    from app.chat import memory_commands as commands
    from app.ventures import founder_memory
    from app.ventures import state_brief

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(commands, "_source", lambda *args: ("Remember: " + statement, "owner"))
    writes = []
    async def remember(**kwargs):
        writes.append(kwargs)
    monkeypatch.setattr(founder_memory, "remember", remember)
    monkeypatch.setattr(state_brief, "invalidate_venture_state_brief", lambda *args: None)
    rejected = asyncio.run(commands.dispatch("ven_fixture", "msg_fixture", action_name="memory.list"))
    assert not writes and "No memory was changed" in rejected["body_md"]
    saved = asyncio.run(commands.dispatch("ven_fixture", "msg_fixture", action_name="memory.remember"))
    assert saved["kind"] == "memory_saved"
    assert writes == [{"venture_id": "ven_fixture", "fact": statement, "provenance": "chat:msg_fixture", "kind": writes[0]["kind"]}]
    def unowned_source(*args):
        raise ValueError("source is not yours")
    monkeypatch.setattr(commands, "_source", unowned_source)
    rejected = asyncio.run(commands.dispatch("ven_fixture", "msg_fixture", action_name="memory.remember"))
    assert len(writes) == 1 and "No memory was changed" in rejected["body_md"]


@pytest.mark.parametrize("preference", ["Use metric units for freight estimates.", "Keep family meal instructions brief."])
def test_remember_receipt_has_human_source_and_settings_next_step(monkeypatch, preference):
    from app.chat import memory_commands as commands

    async def remember(**kwargs):
        assert kwargs["fact"] == preference and kwargs["provenance"] == "chat:msg_fixture"
        return "mem_fixture"

    monkeypatch.setattr(commands, "_source", lambda *_: ("Remember: " + preference, "usr_fixture"))
    monkeypatch.setattr("app.memory.get_memory", lambda: SimpleNamespace(name="sqlite"))
    monkeypatch.setattr("app.ventures.founder_memory.remember", remember)
    monkeypatch.setattr("app.ventures.state_brief.invalidate_venture_state_brief", lambda *_: None)
    reply = asyncio.run(commands.dispatch("ven_fixture", "msg_fixture"))
    assert "Settings" in reply["body_md"] and "you" in reply["body_md"].lower()
    assert "venture context" not in reply["body_md"] and "execution approval" not in reply["body_md"]
    assert reply["citations"] == [{"label": "Your saved note", "href": "/v/ven_fixture/settings#settings-memory"}]


@pytest.mark.parametrize("value", [None, "0", "false", "not-a-boolean"])
def test_unqualified_fact_schema_and_extraction_are_default_off(monkeypatch, value):
    import sys
    for name in ("COMPANY_OS_MEMORY_FACTS_ENABLED", "COMPANY_OS_MEMORY_MODEL_EXTRACT_ENABLED"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    from app.config import settings
    from app.db.models import ALL_TABLE_NAMES, Base
    from app.memory import extraction
    from app.memory.context import subject_for_query
    assert not settings.company_os_memory_facts_enabled
    assert not settings.company_os_memory_model_extract_enabled
    assert "venture_facts" not in ALL_TABLE_NAMES and "venture_facts" not in Base.metadata.tables
    assert "app.db.fact_models" not in sys.modules
    assert extraction.capture_chat_message("msg_source") == []
    assert subject_for_query("audience") == "idea.audience"
    assert "app.db.fact_models" not in sys.modules


def test_memory_list_is_byte_bounded_and_omits_whole_statements():
    from app.chat.memory_commands import LIST_BYTES, list_reply
    huge = SimpleNamespace(mem_id="mem_huge", statement="完整必须保留" * 1800)
    short = SimpleNamespace(mem_id="mem_short", statement="A complete short statement.")
    reply = list_reply([huge, short])
    assert len(reply["body_md"].encode()) < LIST_BYTES - 512
    assert huge.statement not in reply["body_md"] and "完整" not in reply["body_md"]
    assert short.statement in reply["body_md"] and "Omitted 1 whole memory statement" in reply["body_md"]
    assert [item["label"] for item in reply["citations"]] == [short.mem_id]


def test_many_long_memory_statements_cannot_exceed_whole_list_bound():
    from app.chat.memory_commands import LIST_BYTES, list_reply
    items = [SimpleNamespace(mem_id=f"mem_item_{i}", statement=f"{i} " + "完整引用。" * 200) for i in range(100)]
    reply = list_reply(items)
    assert len(reply["body_md"].encode()) < LIST_BYTES - 512
    kept = {item["label"] for item in reply["citations"]}
    assert 0 < len(kept) < 100 and "whole memory statement(s)" in reply["body_md"]
    for item in items:
        assert (item.statement in reply["body_md"]) is (item.mem_id in kept)


def test_memory_listing_withholds_credentials_but_keeps_removal_ids():
    from app.chat.memory_commands import list_reply
    for index, domain in enumerate(["Freight planning", "Family meal planning"]):
        credential = "sk-proj-" + "fixture-value-not-a-real-key" + str(index)
        item = SimpleNamespace(mem_id=f"mem_private_{index}", statement=f"{domain}: {credential}")
        reply = list_reply([item])
        assert credential not in reply["body_md"]
        assert item.mem_id in reply["body_md"]
        assert "Keys and connections" in reply["body_md"]
        assert reply["citations"] == [{"label": item.mem_id, "href": "#memory:" + item.mem_id}]


@pytest.mark.parametrize("raw", [
    "Remember, I changed the audience to architects.",
    "Remember our previous discussion about architects.",
    "Please remember when we met.",
])
def test_conversational_remember_is_not_a_memory_command(raw):
    from app.chat.memory_commands import parse
    assert parse(raw) is None


@pytest.fixture
def local_memories(chat_request, tmp_path, monkeypatch):
    from datetime import timedelta
    from app.authz.workspace import ensure_subject_person
    from app.chat.conversations import bind_conversation, create
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    adapter = LocalMemoryAdapter(tmp_path / "commands.sqlite3")
    monkeypatch.setattr("app.memory.get_memory", lambda: adapter)
    monkeypatch.setattr("app.ventures.state_brief.invalidate_venture_state_brief", lambda *_: None)
    rows = []
    for index, statement in enumerate(["Use metric units for freight estimates.", "Keep family meal instructions brief."]):
        actor = ensure_subject_person(f"usr_memory_{index}")
        workspace = create_workspace(kind="company", name=statement, owner_id=actor)
        venture_id = f"ven_memory_{index}"
        with chat_request["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=workspace.id, name=statement,
                raw_idea=statement, stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        with bind(workspace_id=workspace.id, venture_id=venture_id, actor=actor):
            conversation = create(venture_id, idempotency_key="memory-conversation")
            with bind_conversation(venture_id, conversation["id"]):
                source = append_message(venture_id, role="user", body_md="Remember: " + statement)
                mem_id = asyncio.run(adapter.remember(MemoryWrite(venture_id=venture_id,
                    kind=MemoryKind.FOUNDER_PREFERENCE, subject="general", statement=statement,
                    evidence=["chat:" + source["id"]], confidence=.7, confidence_source="founder",
                    author=actor, dedupe_key="original", valid_from=utcnow() - timedelta(days=20000))))
                for extra in range(101):
                    asyncio.run(adapter.remember(MemoryWrite(venture_id=venture_id, kind=MemoryKind.INSIGHT,
                        subject="general", statement="Unrelated system note", confidence=.7,
                        dedupe_key=f"system-{extra}")))
        rows.append(dict(venture_id=venture_id, workspace_id=workspace.id, actor=actor,
            conversation_id=conversation["id"], statement=statement, mem_id=mem_id))
    yield adapter, rows, chat_request["factory"]
    adapter.close()


def memory_command(row, text):
    from app.chat.conversations import bind_conversation
    from app.chat.memory_commands import dispatch
    from app.chat.store import append_message
    from app.tenancy import bind
    with bind(workspace_id=row["workspace_id"], venture_id=row["venture_id"], actor=row["actor"]):
        with bind_conversation(row["venture_id"], row["conversation_id"]):
            source = append_message(row["venture_id"], role="user", body_md=text)
            return asyncio.run(dispatch(row["venture_id"], source["id"]))


def test_local_list_includes_old_source_backed_notes_and_keeps_tenants_separate(local_memories):
    _, rows, _ = local_memories
    for index, row in enumerate(rows):
        reply = memory_command(row, "List saved venture memories")
        assert reply["kind"] == "memory_list"
        assert row["statement"] in reply["body_md"]
        assert rows[1 - index]["statement"] not in reply["body_md"]
        assert "Unrelated system note" not in reply["body_md"]


def test_local_correct_preserves_history_and_rejects_other_tenant(local_memories):
    from app.memory.model import RecallQuery
    adapter, rows, _ = local_memories
    for index, row in enumerate(rows):
        denied = memory_command(row, f"Correct memory {rows[1 - index]['mem_id']}: {row['statement']}")
        assert "No memory was changed" in denied["body_md"]
        replacement = row["statement"] + " Include a worked example."
        reply = memory_command(row, f"Correct memory {row['mem_id']}: {replacement}")
        assert reply["kind"] == "memory_corrected"
        current = asyncio.run(adapter.recall(RecallQuery(venture_id=row["venture_id"], include_decayed=True, limit=200)))
        assert replacement in [hit.statement for hit in current]
        assert row["statement"] not in [hit.statement for hit in current]
        history = asyncio.run(adapter.timeline(venture_id=row["venture_id"], subject="general"))
        original = next(hit for hit in history if hit.mem_id == row["mem_id"])
        assert original.valid_to and original.superseded_by
        assert replacement in memory_command(row, "List saved venture memories")["body_md"]


def test_local_explicit_memory_writes_refuse_credentials(local_memories):
    from app.memory.model import RecallQuery
    adapter, rows, _ = local_memories
    for index, row in enumerate(rows):
        credential = "sk-proj-" + "fixture-value-not-a-real-key" + str(index)
        statement = row["statement"] + " " + credential
        for command in [f"Remember: {statement}", f"Correct memory {row['mem_id']}: {statement}"]:
            reply = memory_command(row, command)
            assert credential not in reply["body_md"]
            assert "Keys and connections" in reply["body_md"]
            current = asyncio.run(adapter.recall(RecallQuery(venture_id=row["venture_id"], include_decayed=True, limit=200)))
            assert row["statement"] in [hit.statement for hit in current]
            assert all(credential not in hit.statement for hit in current)


def test_local_forget_tombstones_and_rejects_other_tenant(local_memories):
    adapter, rows, _ = local_memories
    for index, row in enumerate(rows):
        denied = memory_command(row, f"Forget memory {rows[1 - index]['mem_id']}")
        assert "No memory was changed" in denied["body_md"]
        reply = memory_command(row, f"Forget memory {row['mem_id']}")
        assert reply["kind"] == "memory_forgotten"
        assert row["statement"] not in memory_command(row, "List saved venture memories")["body_md"]
        with adapter._db.connect() as conn:
            saved = conn.execute("SELECT tombstoned, statement FROM memories WHERE mem_id = ?", (row["mem_id"],)).fetchone()
        assert saved["tombstoned"] == 1 and saved["statement"] == row["statement"]


def test_local_memory_write_storage_error_does_not_claim_no_change(local_memories, monkeypatch):
    import sqlite3
    adapter, rows, _ = local_memories
    supersede = adapter.supersede
    async def uncertain(mem_id, *, by, reason):
        await supersede(mem_id, by=by, reason=reason)
        raise sqlite3.OperationalError("fixture-private-detail")
    monkeypatch.setattr(adapter, "supersede", uncertain)
    for row in rows:
        replacement = row["statement"] + " Include a worked example."
        reply = memory_command(row, f"Correct memory {row['mem_id']}: {replacement}")
        assert "could not be confirmed" in reply["body_md"]
        assert "list saved venture memories" in reply["body_md"]
        assert "No memory was changed" not in reply["body_md"] and "fixture-private-detail" not in reply["body_md"]
        assert replacement in memory_command(row, "List saved venture memories")["body_md"]
