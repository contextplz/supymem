"""Source memory is exact attributed context, never execution authority."""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture
def memory_source(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/unused.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    with service_modules("company_os"):
        from app.db.models import ChatConversation, ChatMessage, Venture
        from app.memory import evidence, task_context
        from app.memory.model import ContextPack, MemoryHit, MemoryKind, MemoryRecord, MemoryWrite
        from app.tenancy import bind, current, current_or_none
        from app.ventures.founder_memory import _run_async

        venture = SimpleNamespace(id="ven_owned", workspace_id="wsp_owned")
        message = SimpleNamespace(id="msg_owned", workspace_id=venture.workspace_id,
                                  venture_id=venture.id, role="user", author_person_id="usr_founder",
                                  conversation_id="chat_private", body_md="Remember that never publish without current review.")
        chat = SimpleNamespace(id=message.conversation_id, workspace_id=venture.workspace_id,
                               venture_id=venture.id, creator_person_id=message.author_person_id, visibility="private")
        rows = {(Venture, venture.id): venture, (ChatMessage, message.id): message, (ChatConversation, chat.id): chat}

        class Session:
            def get(self, model, identity):
                return rows.get((model, identity))

        def write(**changes):
            return MemoryWrite(venture_id=venture.id, kind=MemoryKind.CONSTRAINT,
                               subject="publishing", statement=message.body_md.split("that ", 1)[1],
                               evidence=["chat:" + message.id], author=message.author_person_id,
                               confidence=1, dedupe_key="source-owned", **changes)

        yield SimpleNamespace(session=Session(), venture=venture, message=message, rows=rows,
                              evidence=evidence, projection=task_context, kinds=MemoryKind,
                              Pack=ContextPack, Hit=MemoryHit, Record=MemoryRecord, write=write,
                              bind=bind, current=current, current_or_none=current_or_none,
                              run_async=_run_async)


def bound(d):
    return d.bind(workspace_id=d.venture.workspace_id, venture_id=d.venture.id, actor="usr_founder")


def record(d, memory, proof):
    return d.Record(mem_id="mem_owned", **memory.model_dump(),
                    metadata={"workspace_id": d.venture.workspace_id, "source_evidence": proof})


def hit(d, identity="mem_owned", **changes):
    values = dict(mem_id=identity, venture_id=d.venture.id, kind=d.kinds.CONSTRAINT,
                  subject="publishing", statement="Never publish without current founder review.",
                  evidence=["chat:msg_owned"], author="usr_founder", confidence=1,
                  decayed_confidence=1, valid_from=datetime(2026, 10, 4, tzinfo=timezone.utc))
    return d.Hit(**(values | changes))


def install_pack(d, monkeypatch, *, pack=None, error=None):
    calls = []

    class Port:
        async def context_pack(self, **kwargs):
            calls.append((kwargs, d.current()))
            if error:
                raise error
            return pack

    monkeypatch.setattr(d.projection, "get_memory", lambda: Port())
    return calls


def project(d, **kwargs):
    return d.projection.for_task(d.session, workspace_id=d.venture.workspace_id,
                                venture_id=d.venture.id, title="Plan a reviewed launch", **kwargs)


def test_complete_attributed_founder_statement_is_source_linked(memory_source):
    d = memory_source
    statement = "Never publish without current review. " + "完整约束必须保留。" * 350
    d.message.body_md = "  Remember that " + statement + "  "
    with bound(d):
        value = d.write()
        # Source whitespace is retained in its digest; only the explicit
        # remember statement's outside whitespace is normalized.
        value.statement = statement
        proof = d.evidence.validate_write_evidence(d.session, value)
        saved = record(d, value, proof)
        assert proof[0]["statement"] == statement
        assert proof[0]["kind"] == "founder_statement"
        assert proof[0]["author_person_id"] == "usr_founder"
        assert d.evidence.evidence_current(d.session, saved)
        shortened = value.model_copy(update={"statement": statement[:80]})
        with pytest.raises(ValueError, match="memory_statement_source_mismatch"):
            d.evidence.validate_write_evidence(d.session, shortened)


@pytest.mark.parametrize("field,value", [
    ("role", "assistant"), ("role", "system"), ("role", "tool"),
    ("author_person_id", None), ("author_person_id", "usr_other"),
    ("workspace_id", "wsp_other"), ("venture_id", "ven_other"),
    ("body_md", "The assistant reports deployment complete."),
    ("body_md", "Remember that   "),
])
def test_unowned_or_nonfounder_chat_cannot_author_memory(memory_source, field, value):
    d = memory_source
    memory = d.write()
    setattr(d.message, field, value)
    with bound(d), pytest.raises(ValueError):
        d.evidence.validate_write_evidence(d.session, memory)


@pytest.mark.parametrize("change", [
    {"author": "usr_forged"}, {"author": "system"},
    {"kind": "outcome"}, {"kind": "failure"},
    {"evidence": []}, {"evidence": ["chat:msg_owned", "chat:msg_owned"]},
    {"evidence": ["chat:missing"]}, {"evidence": ["model:msg_owned"]},
    {"evidence": ["chat:msg_owned/other"]},
])
def test_chat_claim_cannot_be_retyped_forged_or_unlinked(memory_source, change):
    d = memory_source
    memory = d.write().model_copy(update=change)
    with bound(d), pytest.raises(ValueError):
        d.evidence.validate_write_evidence(d.session, memory)


@pytest.mark.parametrize("field,value", [
    ("body_md", "Remember that publish without review."), ("role", "assistant"),
    ("author_person_id", "usr_other"), ("conversation_id", "chat_other"),
    ("workspace_id", "wsp_other"), ("venture_id", "ven_other"),
])
def test_source_tamper_invalidates_previously_valid_memory(memory_source, field, value):
    d = memory_source
    with bound(d):
        memory = d.write()
        saved = record(d, memory, d.evidence.validate_write_evidence(d.session, memory))
        assert d.evidence.evidence_current(d.session, saved)
        setattr(d.message, field, value)
        assert not d.evidence.evidence_current(d.session, saved)


@pytest.mark.parametrize("field,value", [("creator_person_id", "usr_other"),
    ("visibility", "legacy_shared"), ("workspace_id", "wsp_other"), ("venture_id", "ven_other")])
def test_live_conversation_revocation_invalidates_copied_statement(memory_source, field, value):
    from app.db.models import ChatConversation
    d = memory_source
    with bound(d):
        memory = d.write()
        saved = record(d, memory, d.evidence.validate_write_evidence(d.session, memory))
        setattr(d.rows[(ChatConversation, d.message.conversation_id)], field, value)
        assert not d.evidence.evidence_current(d.session, saved)
        with pytest.raises(ValueError):
            d.evidence.validate_write_evidence(d.session, memory)


def test_other_actor_cannot_read_copied_private_statement(memory_source):
    d = memory_source
    with bound(d):
        memory = d.write()
        saved = record(d, memory, d.evidence.validate_write_evidence(d.session, memory))
    with d.bind(workspace_id=d.venture.workspace_id, venture_id=d.venture.id, actor="usr_other"):
        assert not d.evidence.evidence_current(d.session, saved)


@pytest.mark.parametrize("change", [
    {"author": "usr_forged"}, {"kind": "outcome"},
    {"statement": "Deployment is verified."}, {"evidence": ["chat:missing"]},
    {"metadata": {"workspace_id": "wsp_other", "source_evidence": []}},
])
def test_saved_record_tamper_cannot_change_source_authority(memory_source, change):
    d = memory_source
    with bound(d):
        memory = d.write()
        saved = record(d, memory, d.evidence.validate_write_evidence(d.session, memory))
        assert not d.evidence.evidence_current(d.session, saved.model_copy(update=change))


def test_source_write_requires_exact_active_workspace_and_venture(memory_source):
    d = memory_source
    for workspace, venture in [("wsp_other", d.venture.id), (d.venture.workspace_id, "ven_other")]:
        with d.bind(workspace_id=workspace, venture_id=venture, actor="usr_founder"):
            with pytest.raises(ValueError, match="memory_source_scope_invalid"):
                d.evidence.validate_write_evidence(d.session, d.write())


def test_task_context_preserves_whole_constraint_and_exact_actor_without_effects(memory_source, monkeypatch):
    d = memory_source
    statement = "Never send, publish, or spend before the separately reviewed source task is approved. " * 24
    item = hit(d, statement=statement)
    calls = install_pack(d, monkeypatch, pack=d.Pack(venture_id=d.venture.id, memories=[item]))
    with bound(d):
        result = project(d, byte_budget=6000, tags=["planning"])
        assert d.current().actor == "usr_founder"
    assert calls[0][1].workspace_id == d.venture.workspace_id
    assert calls[0][1].venture_id == d.venture.id and calls[0][1].actor == "usr_founder"
    assert calls[0][0]["task"].tags == ["planning"]
    assert result["availability"] == "available"
    assert result["statements"][0]["statement"] == statement
    assert result["statements"][0]["evidence"] == item.evidence
    assert result["statements"][0]["qualification"] == "saved_founder_statement_context_only"
    assert result["effect_authority"] is False and result["current_state_authority"] is False
    assert result["omitted_count"] == 0


def test_founder_insight_reaches_task_context_with_exact_source(memory_source, monkeypatch):
    d = memory_source
    d.message.body_md = "Remember that Our customers are independent local bookshops."
    with bound(d):
        memory = d.write().model_copy(update={"kind": d.kinds.INSIGHT, "subject": "idea.audience"})
        saved = record(d, memory, d.evidence.validate_write_evidence(d.session, memory))
        assert d.evidence.evidence_current(d.session, saved)
        item = hit(d, kind=memory.kind, subject=memory.subject, statement=memory.statement)
        install_pack(d, monkeypatch, pack=d.Pack(venture_id=d.venture.id, memories=[item]))
        result = project(d)
    assert result["availability"] == "available"
    assert result["statements"][0]["kind"] == "insight"
    assert result["statements"][0]["subject"] == "idea.audience"
    assert result["statements"][0]["statement"] == memory.statement
    assert result["statements"][0]["evidence"] == memory.evidence
    assert result["statements"][0]["author"] == "usr_founder"
    assert result["effect_authority"] is False and result["current_state_authority"] is False
    assert result["omitted_count"] == 0


def test_multibyte_oversize_is_whole_omitted_with_honest_bounded_metadata(memory_source, monkeypatch):
    d = memory_source
    short = hit(d, "mem_short")
    oversized = hit(d, "mem_whole", statement="不得发布或发送。" * 800)
    upstream = ["mem_upstream_" + str(i) + "x" * 200 for i in range(30)]
    pack = d.Pack(venture_id=d.venture.id, memories=[short, oversized],
                  omitted_memory_ids=upstream, omitted_count=35, truncated_kinds=["constraint"])
    install_pack(d, monkeypatch, pack=pack)
    with bound(d):
        result = project(d, byte_budget=1600)
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= 1600
    assert [row["id"] for row in result["statements"]] == ["mem_short"]
    assert result["statements"][0]["statement"] == short.statement
    assert oversized.statement[:24] not in encoded.decode()
    assert result["omitted_count"] == 36
    assert set(result["omitted_memory_ids"]) <= set(upstream + [oversized.mem_id])
    assert len(result["omitted_memory_ids"]) < 31
    assert result["truncated_kinds"] == ["constraint"]


def test_whole_omission_identity_is_retained_when_metadata_fits(memory_source, monkeypatch):
    d = memory_source
    item = hit(d, statement="完整约束。" * 800)
    install_pack(d, monkeypatch, pack=d.Pack(venture_id=d.venture.id, memories=[item]))
    with bound(d):
        result = project(d, byte_budget=1024)
    assert result["statements"] == []
    assert result["omitted_count"] == 1 and result["omitted_memory_ids"] == [item.mem_id]
    assert item.statement[:20] not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("field,value", [
    ("valid_to", datetime(2026, 10, 4, tzinfo=timezone.utc)),
    ("superseded_by", "mem_new"), ("evidence", []),
])
@pytest.mark.parametrize("kind", ["constraint", "insight"])
def test_retired_or_unlinked_constraints_are_explicitly_omitted(memory_source, monkeypatch, field, value, kind):
    d = memory_source
    item = hit(d, kind=d.kinds(kind), **{field: value})
    install_pack(d, monkeypatch, pack=d.Pack(venture_id=d.venture.id, memories=[item]))
    with bound(d):
        result = project(d)
    assert result["availability"] == "available" and result["statements"] == []
    assert result["omitted_count"] == 1 and result["omitted_memory_ids"] == [item.mem_id]


@pytest.mark.parametrize("mode", ["foreign_pack", "foreign_hit", "failure"])
def test_unavailable_or_foreign_memory_never_leaks_partial_context(memory_source, monkeypatch, mode):
    d = memory_source
    pack = d.Pack(venture_id="ven_other" if mode == "foreign_pack" else d.venture.id,
                  memories=[hit(d), hit(d, "mem_foreign", venture_id="ven_other")]
                  if mode == "foreign_hit" else [hit(d)])
    install_pack(d, monkeypatch, pack=pack, error=OSError("store unavailable") if mode == "failure" else None)
    with bound(d):
        result = project(d)
    assert result["availability"] == "unavailable"
    assert result["statements"] == [] and result["omitted_memory_ids"] == []
    assert result["effect_authority"] is False and result["current_state_authority"] is False
    assert "Never publish" not in json.dumps(result)


def test_foreign_task_scope_is_refused_before_memory_io(memory_source, monkeypatch):
    d = memory_source
    calls = install_pack(d, monkeypatch, pack=d.Pack(venture_id=d.venture.id))
    with d.bind(workspace_id="wsp_other", venture_id=d.venture.id, actor="usr_founder"):
        with pytest.raises(ValueError, match="memory_task_scope_invalid"):
            project(d)
    assert calls == []


@pytest.mark.parametrize("budget", [True, 1023, 16001, 2048.5])
def test_invalid_task_budget_is_refused_before_memory_io(memory_source, monkeypatch, budget):
    d = memory_source
    calls = install_pack(d, monkeypatch, pack=d.Pack(venture_id=d.venture.id))
    with bound(d), pytest.raises(ValueError, match="memory_task_budget_invalid"):
        project(d, byte_budget=budget)
    assert calls == []


def test_run_async_inside_running_loop_preserves_exact_scope_and_other_contextvars(memory_source):
    d = memory_source
    conversation = ContextVar("test_private_conversation", default=None)

    async def read():
        await asyncio.sleep(0)
        return d.current(), conversation.get()

    async def caller():
        with bound(d):
            token = conversation.set("conversation_owned")
            try:
                expected = d.current()
                actual, cid = d.run_async(read())
                assert actual == expected and cid == "conversation_owned"
                assert d.current() == expected and conversation.get() == "conversation_owned"
            finally:
                conversation.reset(token)
        assert d.current_or_none() is None and conversation.get() is None

    asyncio.run(caller())
