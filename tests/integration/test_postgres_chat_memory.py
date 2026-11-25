"""Signed private conversations, append/compaction custody and shared PG memory."""
import asyncio
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, func
from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


def test_upgrade_existing_raw_history_preserves_source_columns(pg_database, monkeypatch):
    """The actual s036→head upgrade adds partitions, without rewriting history."""
    from datetime import datetime
    from pathlib import Path
    import hashlib
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from tests.integration.test_postgres_auth_security import service_modules

    root = Path(__file__).resolve().parents[2]
    admin = create_engine(make_url(pg_database[0]).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = "supymem_chat_upgrade_" + secrets.token_hex(6)
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(pg_database[0]).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        monkeypatch.setenv("COMPANY_OS_DB_URL", url)
        with service_modules("company_os"):
            cfg = Config(str(root / "services/company_os/alembic.ini"))
            cfg.set_main_option("script_location", str(root / "services/company_os/app/db/migrations"))
            command.upgrade(cfg, "s036touchventure")
            now = datetime(2026, 1, 2, 3, 4, 5, 123456)
            rows = [dict(id=f"msg_existing_{i}", vid=f"ven_existing_{i % 2}", role=role,
                         kind="confirmation" if role == "assistant" else "text",
                         body="Complete original Ω statement\nwith two lines " + str(i),
                         actions=json.dumps([{"action": "plan.approve", "payload": {"plan_id": "pln_existing"}}]),
                         citations=json.dumps([{"label": "Source", "url": "https://example.test/exact?x=%20"}]),
                         nonce=f"existing_nonce_{i}", created=now) for i, role in enumerate(["user", "assistant", "system"])]
            source_columns = "id,venture_id,role,kind,body_md,citations_json,actions_json,idempotency_key,created_at"
            with engine.begin() as conn:
                for i in range(2):
                    conn.execute(text("INSERT INTO ventures (id,workspace_id,name,raw_idea,stage,autonomy_level,created_at,updated_at) VALUES (:id,:wid,'Existing venture','Existing idea','idea','L1',:now,:now)"),
                                 {"id": f"ven_existing_{i}", "wid": f"wsp_existing_{i}", "now": now})
                for row in rows:
                    conn.execute(text("INSERT INTO chat_messages (id,venture_id,role,kind,body_md,citations_json,actions_json,idempotency_key,created_at) VALUES (:id,:vid,:role,:kind,:body,:citations,:actions,:nonce,:created)"), row)
                before = conn.execute(text(f"SELECT {source_columns} FROM chat_messages ORDER BY id")).all()
            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                assert conn.execute(text(f"SELECT {source_columns} FROM chat_messages ORDER BY id")).all() == before
                assert conn.execute(text("SELECT count(*) FROM chat_context_checkpoints")).scalar_one() == 0
                assert conn.execute(text("SELECT count(*) FROM institutional_memories")).scalar_one() == 0
                assert conn.execute(text("SELECT count(*) FROM memory_working_entries")).scalar_one() == 0
                for i in range(2):
                    vid, wid = f"ven_existing_{i}", f"wsp_existing_{i}"
                    cid = "cnv_" + hashlib.sha256(("legacy:" + vid).encode()).hexdigest()[:32]
                    conversation = conn.execute(text("SELECT id,workspace_id,visibility,creator_person_id FROM chat_conversations WHERE venture_id=:vid"), {"vid": vid}).one()
                    assert tuple(conversation) == (cid, wid, "legacy_shared", None)
                    partitions = conn.execute(text("SELECT conversation_id,workspace_id,author_person_id FROM chat_messages WHERE venture_id=:vid"), {"vid": vid}).all()
                    assert all(tuple(row) == (cid, wid, None) for row in partitions)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def conversations(journey_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api import chat_api
    from app.chat.store import append_message
    from app.tenancy import bind

    d = journey_db
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(chat_api.router)
    client = TestClient(app)

    async def no_provider(vid, text, **kwargs):
        append_message(vid, role="user", body_md=text)
        return append_message(vid, role="assistant", body_md="Saved reply in this conversation.")
    monkeypatch.setattr(chat_api, "handle_message", no_provider)

    def request(method, path="/chat/conversations", actor=None, **kwargs):
        path = f"/ventures/{d['vid']}" + path
        subject = actor or d["actor"]
        assertion = sign_assertion(secret, sub=subject, email="", role="user", path=path, method=method)
        return client.request(method, path, headers={"X-COS-Assertion": assertion, "X-Subject": subject}, **kwargs)

    def bound(actor=None):
        return bind(workspace_id=d["wid"], venture_id=d["vid"], actor=actor or d["actor"])
    return d, request, bound


def test_signed_new_chat_metadata_only_and_exact_history(conversations):
    from app.db.models import Job, ChatMessage
    from app.chat.store import append_message
    d, request, bound = conversations
    with bound():
        legacy_message = append_message(d["vid"], role="user", body_md="Earlier shared note")
    with d["factory"]() as session:
        before_jobs = session.scalar(select(func.count(Job.id)))
    catalog = request("GET").json()
    new = request("POST", json={"idempotency_key": "new-chat-0001"})
    assert new.status_code == 200, new.text
    cid = new.json()["id"]
    assert cid != catalog["legacy_conversation_id"] and new.json()["visibility"] == "private"
    assert request("POST", json={"idempotency_key": "new-chat-0001"}).json()["id"] == cid
    assert request("GET", "/chat", params={"conversation_id": cid}).json()["items"] == []
    reply = request("POST", "/chat", json={"message": "A private founder message", "conversation_id": cid})
    assert reply.status_code == 200, reply.text
    assert reply.json()["conversation_id"] == cid
    own = request("GET", "/chat", params={"conversation_id": cid}).json()["items"]
    assert len(own) == 2 and "Earlier shared" not in str(own)
    legacy = request("GET", "/chat").json()
    assert [row["id"] for row in legacy["items"]] == [legacy_message["id"]]
    with d["factory"]() as session:
        assert session.scalar(select(func.count(Job.id))) == before_jobs
        assert all(row.workspace_id == d["wid"] for row in session.scalars(select(ChatMessage).where(ChatMessage.venture_id == d["vid"])))


def test_member_cannot_read_private_conversation_or_cursor(conversations):
    from app.db.models import WorkspaceMember
    from app.authz.workspace import ensure_subject_person
    from core.clock import utcnow
    from core.ids import new_id
    d, request, bound = conversations
    other = "usr_private_member_" + secrets.token_hex(8)
    ensure_subject_person(other)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id=new_id("wmb"), workspace_id=d["wid"], person_id=other, role="member", created_at=utcnow()))
        session.commit()
    cid = request("POST", json={"idempotency_key": "owner-chat-001"}).json()["id"]
    msg = request("POST", "/chat", json={"message": "Private actor request", "conversation_id": cid}).json()
    assert request("GET", "/chat", actor=other, params={"conversation_id": cid}).status_code == 404
    assert request("POST", "/chat", actor=other, json={"message": "Foreign", "conversation_id": cid}).status_code == 404
    assert request("GET", actor=other).status_code == 200
    assert cid not in str(request("GET", actor=other).json())
    assert request("GET", "/chat", actor=other, params={"before": msg["id"]}).status_code == 404
    assert request("GET", actor="usr_foreign").status_code == 404


def test_concurrent_create_and_append_nonces_scoped_per_chat(conversations):
    from app.chat import conversations as svc
    from app.chat.store import append_message, list_messages
    d, request, bound = conversations
    barrier = Barrier(2)
    def create(_):
        with bound():
            barrier.wait()
            return svc.create(d["vid"], idempotency_key="concurrent-new")
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(create, range(2)))
    assert rows[0]["id"] == rows[1]["id"]
    with bound():
        other = svc.create(d["vid"], idempotency_key="separate-new")
        a = append_message(d["vid"], role="user", body_md="Exact original", idempotency_key="same", conversation_id=rows[0]["id"])
        b = append_message(d["vid"], role="user", body_md="Different original", idempotency_key="same", conversation_id=other["id"])
        assert a["id"] != b["id"]
        assert append_message(d["vid"], role="user", body_md="Exact original", idempotency_key="same", conversation_id=rows[0]["id"])["id"] == a["id"]
        with pytest.raises(ValueError, match="chat_idempotency_conflict"):
            append_message(d["vid"], role="user", body_md="Changed", idempotency_key="same", conversation_id=rows[0]["id"])
        assert [row["id"] for row in list_messages(d["vid"], conversation_id=other["id"])] == [b["id"]]


def test_compaction_whole_sources_tamper_and_retrieval(conversations):
    from app.chat.store import append_message
    from app.chat.conversations import bind_conversation
    from app.chat.compaction import compact, context_for_question
    from app.db.models import ChatMessage, ChatContextCheckpoint
    d, request, bound = conversations
    cid = request("POST", json={"idempotency_key": "compact-new-0001"}).json()["id"]
    with bound(), bind_conversation(d["vid"], cid):
        constraint = "Remember never publish under a provisional approval: " + "whole exact constraint " * 60
        first = append_message(d["vid"], role="user", body_md=constraint)
        for i in range(25):
            append_message(d["vid"], role="assistant", body_md=f"Historical model claim {i}: work done.")
        checkpoint = compact(d["vid"], byte_budget=3000)
        context = context_for_question(d["vid"], "provisional approval exact constraint", byte_budget=6000)
        assert context["checkpoint_valid"] and context["raw_history_retained"]
        original = next(row for row in context["items"] if row["source_message_id"] == first["id"])
        assert original["body_md"] == constraint and original["evidence_kind"] == "founder_statement"
        assert context["omitted_count"] > 0
        with d["factory"]() as session:
            row = session.get(ChatContextCheckpoint, checkpoint)
            value = json.loads(row.payload_json)
            value["items"][0]["body_md"] = "Injected false completion"
            row.payload_json = json.dumps(value)
            session.commit()
        context = context_for_question(d["vid"], "provisional approval", byte_budget=6000)
        assert context["checkpoint_valid"] is False
        assert "Injected false" not in str(context)
        with d["factory"]() as session:
            assert session.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == cid)) == 26


def test_private_pending_state_and_cross_replica_atomic_updates(conversations):
    from app.chat.conversations import bind_conversation, pending_key
    from app.memory.pg_working import PostgresWorkingMemory
    d, request, bound = conversations
    one = request("POST", json={"idempotency_key": "pending-one-0001"}).json()["id"]
    two = request("POST", json={"idempotency_key": "pending-two-0001"}).json()["id"]
    a, b = PostgresWorkingMemory(), PostgresWorkingMemory()
    with bound(), bind_conversation(d["vid"], one):
        a[pending_key(d["vid"])] = "review original"
        assert b[pending_key(d["vid"])] == "review original"
        a[f"counter:{d['vid']}"] = 0
        _, version = b.versioned(f"counter:{d['vid']}")
        assert a.compare_and_set(f"counter:{d['vid']}", version, 1)
        assert b.compare_and_set(f"counter:{d['vid']}", version, 99) is False
    with bound(), bind_conversation(d["vid"], two):
        assert b.get(pending_key(d["vid"])) is None
    def increment(_):
        with bound():
            b.update_value(f"counter:{d['vid']}", lambda old: old + 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(increment, range(20)))
    with bound():
        assert a[f"counter:{d['vid']}"] == 21


def _private_clarifier_job(conversations, monkeypatch):
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.credits.accounts import ensure_account
    from app.db.models import Workspace
    from app.db.supymem_models import IdeaSession
    from app.idea.service import post_message
    from tests.idea_fixtures import completion, install_provider
    d, request, bound = conversations
    cid = request("POST", json={"idempotency_key": "delayed-private-clarifier"}).json()["id"]
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_at, idea.confirmed_revision_id = "clarifying", None, None
        workspace = session.get(Workspace, d["wid"])
        workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
            "chat_entry_defer_clarification": True, "entry_goal": "startup"})
        session.commit()
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    calls = install_provider(monkeypatch, lambda *args: completion(proposal=False, text="Which bakery would try this first?"))
    with bound(), bind_conversation(d["vid"], cid):
        append_message(d["vid"], role="user", body_md="The first bakery has a paper order sheet.")
        result = post_message(d["sid"], "The first bakery has a paper order sheet.", actor_subject=d["actor"])
    return d, request, bound, cid, result["job_id"], calls


def _deliver_private_clarifier(d, job_id):
    from app.core.jobs import lease
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with d["factory"]() as session:
        jobs = lease(session, "private-chat-worker", kinds=["idea.clarify_turn"], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].id == job_id
    asyncio.run(_run_one(d["factory"], jobs[0], {job_id}))


def test_actual_leased_clarifier_delayed_reply_stays_in_source_private_chat(conversations, monkeypatch):
    from app.chat.conversations import bind_conversation
    from app.core.jobs import enqueue
    from app.db.models import Job
    d, request, bound, cid, job_id, calls = _private_clarifier_job(conversations, monkeypatch)
    with d["factory"]() as session:
        job = session.get(Job, job_id)
        assert json.loads(job.payload_json)["_chat_conversation"] == {"schema_version": 1, "conversation_id": cid,
            "venture_id": d["vid"], "workspace_id": d["wid"], "actor_id": d["actor"]}
    other = request("POST", json={"idempotency_key": "other-private-chat"}).json()["id"]
    with bound(), bind_conversation(d["vid"], other), d["factory"]() as session:
        with pytest.raises(ValueError, match="job_conversation_scope_mismatch"):
            enqueue(session, "idea.clarify_turn", {}, workspace_id=d["wid"], venture_id=d["vid"], dedupe_key=job.dedupe_key)
    _deliver_private_clarifier(d, job_id)
    own = request("GET", "/chat", params={"conversation_id": cid}).json()["items"]
    ready = [row for row in own if row["kind"] == "idea_clarification_ready"]
    assert len(ready) == 1 and ready[0]["conversation_id"] == cid
    assert "first?" in ready[0]["body_md"] and len(calls) == 1
    assert request("GET", "/chat", params={"conversation_id": other}).json()["items"] == []
    assert request("GET", "/chat").json()["items"] == []
    with d["factory"]() as session:
        assert session.get(Job, job_id).status == "succeeded"


def test_actual_failed_private_clarifier_posts_failure_to_source_chat_only(conversations, monkeypatch):
    from app.db.models import Job
    from tests.idea_fixtures import completion, install_provider
    d, request, _, cid, job_id, _ = _private_clarifier_job(conversations, monkeypatch)
    def rejected(*args):
        reply = completion(proposal=False)
        reply.choices[0].message.content = "This answer has no valid structured question."
        return reply
    calls = install_provider(monkeypatch, rejected)
    _deliver_private_clarifier(d, job_id)
    own = request("GET", "/chat", params={"conversation_id": cid}).json()["items"]
    # One transport call per attempt: rejected output is not auto-repaired in the same reservation.
    assert len(calls) == 1 and len(own) == 2
    assert own[-1]["conversation_id"] == cid and own[-1]["kind"].endswith("_failed")
    assert "did not finish" in own[-1]["body_md"]
    assert request("GET", "/chat").json()["items"] == []
    with d["factory"]() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed_dead" and job.last_error == "clarification_invalid_output"


@pytest.mark.parametrize("field,value", [("actor_id", "usr_foreign"), ("workspace_id", "wsp_foreign"),
    ("venture_id", "ven_foreign"), ("conversation_id", "cnv_missing"), ("schema_version", True)])
def test_private_job_bad_persisted_custody_refuses_before_provider(conversations, monkeypatch, field, value):
    from app.db.models import Job, ChatMessage
    d, request, _, cid, job_id, calls = _private_clarifier_job(conversations, monkeypatch)
    with d["factory"]() as session:
        job = session.get(Job, job_id)
        payload = json.loads(job.payload_json)
        payload["_chat_conversation"][field] = value
        job.payload_json = json.dumps(payload)
        before = session.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.venture_id == d["vid"]))
        session.commit()
    _deliver_private_clarifier(d, job_id)
    assert calls == []
    with d["factory"]() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed_dead" and "job_conversation_scope_mismatch" in job.last_error
        assert session.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.venture_id == d["vid"])) == before
    assert len(request("GET", "/chat", params={"conversation_id": cid}).json()["items"]) == 1


def test_pg_memory_source_corrects_forgets_and_revalidates(conversations):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite, RecallQuery, TaskRef
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    d, request, bound = conversations
    a, b = PostgresMemoryAdapter(), PostgresMemoryAdapter()
    def write(message, statement, key):
        return MemoryWrite(venture_id=d["vid"], kind=MemoryKind.FOUNDER_PREFERENCE,
            subject="style", statement=statement, evidence=["chat:" + message["id"]],
            confidence=1, confidence_source="founder", author=d["actor"], dedupe_key=key)
    with bound():
        first = append_message(d["vid"], role="user", body_md="Remember that concise answers work best")
        mid = asyncio.run(a.remember(write(first, "concise answers work best", "memory-1")))
        hits = asyncio.run(b.recall(RecallQuery(venture_id=d["vid"], text="concise")))
        assert [hit.mem_id for hit in hits] == [mid]
        replacement = append_message(d["vid"], role="user", body_md="Remember that detailed answers work best")
        new_id = asyncio.run(b.supersede(mid, by=write(replacement, "detailed answers work best", "memory-2"), reason="explicit correction"))
        hits = asyncio.run(a.recall(RecallQuery(venture_id=d["vid"], text="answers")))
        assert [hit.mem_id for hit in hits] == [new_id]
        pack = asyncio.run(a.context_pack(venture_id=d["vid"], task=TaskRef(title="answers"), token_budget=1))
        assert pack.memories == [] and pack.omitted_memory_ids == [new_id]
        with d["factory"]() as session:
            session.get(ChatMessage, replacement["id"]).body_md = "Remember that tampered source"
            session.commit()
        assert asyncio.run(b.recall(RecallQuery(venture_id=d["vid"], text="answers"))) == []
        asyncio.run(a.forget(venture_id=d["vid"], mem_id=new_id, reason="founder request"))
        assert asyncio.run(b.stats(d["vid"])).active == 0


def test_public_forget_rechecks_active_memory_after_concurrent_correction(conversations):
    from app.chat.store import append_message
    from app.db.models import InstitutionalMemory
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryRecord, MemoryWrite, RecallQuery
    d, _, bound = conversations
    adapter = PostgresMemoryAdapter()
    def write(source, statement, key):
        return MemoryWrite(venture_id=d["vid"], kind=MemoryKind.FOUNDER_PREFERENCE,
            subject="style", statement=statement, evidence=["chat:" + source["id"]],
            author=d["actor"], confidence=1, confidence_source="founder", dedupe_key=key)
    with bound():
        source = append_message(d["vid"], role="user", body_md="Remember that concise replies")
        old = asyncio.run(adapter.remember(write(source, "concise replies", "retired-forget-old")))
        forget = append_message(d["vid"], role="user", body_md="Forget memory " + old)
        replacement = append_message(d["vid"], role="user", body_md="Remember that detailed replies")
        new = asyncio.run(adapter.supersede(old, by=write(replacement, "detailed replies", "retired-forget-new"), reason="later correction"))
        with pytest.raises(ValueError, match="memory_forget_source_changed"):
            asyncio.run(adapter.forget(venture_id=d["vid"], mem_id=old, reason="explicit command", source_message_id=forget["id"]))
        with d["factory"]() as session:
            previous = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, old).record_json)
            assert previous.superseded_by == new and previous.valid_to is not None and not previous.tombstoned
        assert [hit.mem_id for hit in asyncio.run(adapter.recall(RecallQuery(venture_id=d["vid"], text="replies")))] == [new]


@pytest.mark.parametrize("operation", ["correct", "forget"])
@pytest.mark.parametrize("field,value", [("mem_id", "mem_mismatched"), ("venture_id", "ven_mismatched")])
def test_public_memory_transaction_rechecks_record_identity(conversations, operation, field, value):
    from app.chat.store import append_message
    from app.db.models import InstitutionalMemory
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryRecord, MemoryWrite
    d, _, bound = conversations
    adapter = PostgresMemoryAdapter()
    def write(source, statement, key):
        return MemoryWrite(venture_id=d["vid"], kind=MemoryKind.FOUNDER_PREFERENCE,
            subject="style", statement=statement, evidence=["chat:" + source["id"]],
            author=d["actor"], confidence=1, confidence_source="founder", dedupe_key=key)
    with bound():
        source = append_message(d["vid"], role="user", body_md="Remember that concise replies")
        mid = asyncio.run(adapter.remember(write(source, "concise replies", "identity-old")))
        command = append_message(d["vid"], role="user", body_md=(f"Correct memory {mid}: detailed replies" if operation == "correct" else f"Forget memory {mid}"), kind="memory_command", preserve_exact=True)
        with d["factory"]() as session:
            row = session.get(InstitutionalMemory, mid)
            record = MemoryRecord.model_validate_json(row.record_json)
            setattr(record, field, value)
            before = record.model_dump_json()
            row.record_json = before
            session.commit()
        with pytest.raises(ValueError, match="memory_" + ("correction" if operation == "correct" else "forget") + "_source_changed"):
            if operation == "correct":
                asyncio.run(adapter.supersede(mid, by=write(command, "detailed replies", "identity-new"), reason="explicit command"))
            else:
                asyncio.run(adapter.forget(venture_id=d["vid"], mem_id=mid, reason="explicit command", source_message_id=command["id"]))
        with d["factory"]() as session:
            assert session.get(InstitutionalMemory, mid).record_json == before
            assert session.scalar(select(func.count(InstitutionalMemory.id)).where(InstitutionalMemory.venture_id == d["vid"])) == 1


def test_memory_rejects_model_done_opaque_refs_and_cross_scope(conversations):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.chat.store import append_message
    from app.tenancy import bind
    d, request, bound = conversations
    adapter = PostgresMemoryAdapter()
    with bound():
        model = append_message(d["vid"], role="assistant", body_md="Remember that task was completed")
        write = MemoryWrite(venture_id=d["vid"], kind=MemoryKind.OUTCOME, subject="task",
            statement="task was completed", evidence=["chat:" + model["id"]], confidence=1,
            confidence_source="rule", dedupe_key="fake-done")
        with pytest.raises(ValueError):
            asyncio.run(adapter.remember(write))
        write.evidence = ["task:synthetic-hash"]
        with pytest.raises(ValueError):
            asyncio.run(adapter.remember(write))
    with bind(workspace_id="wsp_foreign", venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(PermissionError):
            asyncio.run(adapter.stats(d["vid"]))


def test_read_scope_required_and_credentials_never_enter_memory(conversations):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.pg_working import PostgresWorkingMemory
    from app.memory.model import MemoryWrite, MemoryKind
    from app.chat.store import append_message
    from app.tenancy import ScopeRequired, bind
    d, _, bound = conversations
    adapter = PostgresMemoryAdapter()
    with pytest.raises(ScopeRequired):
        asyncio.run(adapter.stats(d["vid"]))
    with pytest.raises(ScopeRequired):
        PostgresWorkingMemory().get(f"brief:{d['vid']}")
    with bind(workspace_id="none", venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(PermissionError):
            asyncio.run(adapter.stats(d["vid"]))
    with bound():
        credential = "sk-proj-" + secrets.token_hex(24)
        source = append_message(d["vid"], role="user", body_md="Remember that key " + credential)
        write = MemoryWrite(venture_id=d["vid"], kind=MemoryKind.FOUNDER_PREFERENCE,
            subject="key", statement="key " + credential, evidence=["chat:" + source["id"]],
            confidence=1, confidence_source="founder", dedupe_key="secret-refusal")
        with pytest.raises(ValueError, match="memory_credential_withheld"):
            asyncio.run(adapter.remember(write))
        assert asyncio.run(adapter.stats(d["vid"])).total == 0


def test_complete_dedupe_and_context_never_clip_constraint(conversations):
    from app.gbrain.verbs import remember
    from app.chat.store import append_message
    from app.chat.compaction import context_for_question
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import RecallQuery
    import app.memory as factory
    d, request, bound = conversations
    with bound():
        factory._ADAPTER = PostgresMemoryAdapter()
        prefix = "I prefer complete, precise source evidence for every founder statement " + "always " * 25
        a, b = prefix + "alpha", prefix + "beta"
        ids = []
        for statement in (a, b):
            source = append_message(d["vid"], role="user", body_md="Remember that " + statement)
            ids.append(asyncio.run(remember(venture_id=d["vid"], fact=statement,
                provenance="chat:" + source["id"], kind="preference"))["id"])
        assert len(set(ids)) == 2
        hits = asyncio.run(factory.get_memory().recall(RecallQuery(venture_id=d["vid"], text="precise")))
        assert {hit.statement for hit in hits} == {a, b}
        for i in range(35):
            append_message(d["vid"], role="user", body_md="large recent original " + str(i) + " " + "details " * 50)
        context = context_for_question(d["vid"], "precise evidence alpha", byte_budget=1800)
        assert len(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) <= 1800
        assert context["omitted_count"] == 37 - len(context["items"])
        assert context["omitted_count"] == len(context["omitted_source_ids"]) + context["omitted_source_ids_unlisted"]
        for item in context["items"]:
            assert not item["body_md"].endswith("…")


def test_append_during_compaction_survives_committed_prefix(conversations, monkeypatch):
    from threading import Event
    from app.chat import compaction
    from app.chat.store import append_message
    from app.chat.conversations import bind_conversation
    d, request, bound = conversations
    cid = request("POST", json={"idempotency_key": "concurrent-compact"}).json()["id"]
    with bound(), bind_conversation(d["vid"], cid):
        for i in range(20):
            append_message(d["vid"], role="user", body_md=f"Original {i}")
    started, release = Event(), Event()
    original = compaction._digest
    def pause(rows):
        started.set()
        assert release.wait(10)
        return original(rows)
    monkeypatch.setattr(compaction, "_digest", pause)
    def compact():
        with bound(), bind_conversation(d["vid"], cid):
            return compaction.compact(d["vid"])
    def append():
        with bound(), bind_conversation(d["vid"], cid):
            return append_message(d["vid"], role="user", body_md="Appended while compacting")
    with ThreadPoolExecutor(max_workers=2) as pool:
        checkpoint = pool.submit(compact)
        assert started.wait(10)
        latest = pool.submit(append)
        release.set()
        checkpoint.result(10)
        saved = latest.result(10)
    monkeypatch.setattr(compaction, "_digest", original)
    with bound(), bind_conversation(d["vid"], cid):
        context = compaction.context_for_question(d["vid"], "Appended")
        assert context["source_watermark"] == saved["id"]
        assert any(item["source_message_id"] == saved["id"] for item in context["items"])


def test_legacy_cached_response_projects_only_admitted_id(conversations):
    from app.chat.store import append_message, idempotent_message
    from app.db.models import ChatMessage
    d, request, bound = conversations
    with bound():
        original = append_message(d['vid'], role='user', body_md='Original historical user note', idempotency_key='old-nonce')
        with d['factory']() as session:
            row = session.get(ChatMessage, original['id'])
            row.conversation_id = None
            session.commit()
        cached = idempotent_message(d['vid'], 'user', 'old-nonce')
        assert cached['conversation_id'] == original['conversation_id']
        repeated = append_message(d['vid'], role='user', body_md='Original historical user note', idempotency_key='old-nonce')
        assert repeated['id'] == original['id'] and repeated['conversation_id'] == original['conversation_id']
        with d['factory']() as session:
            assert session.get(ChatMessage, original['id']).conversation_id is None


def test_server_memory_kinds_display_complete_exact_source_only(conversations):
    from app.chat.store import append_message, list_messages
    d, _, bound = conversations
    statement = "Complete correction — keep http://localhost:4500 and both lines\nunchanged."
    with bound():
        command = append_message(d["vid"], role="user", kind="memory_command", body_md=statement, preserve_exact=True)
        listed = append_message(d["vid"], role="assistant", kind="memory_list", body_md=statement, preserve_exact=True)
        ordinary = append_message(d["vid"], role="user", kind="text", body_md=statement, preserve_exact=True)
        assert command["body_md"] == statement and statement in listed["body_md"]
        assert "localhost" not in ordinary["body_md"] and "—" not in ordinary["body_md"]
        history = {row["id"]: row for row in list_messages(d["vid"])}
        assert history[command["id"]]["body_md"] == statement and statement in history[listed["id"]]["body_md"]
        assert history[ordinary["id"]]["body_md"] == ordinary["body_md"]


def test_real_consumers_keep_concurrent_ledger_mail_and_feedback(conversations, monkeypatch):
    from app import state
    from app.memory.pg_working import PostgresWorkingMemory
    from app.runtime import handlers
    from app.munder import ledger, mailbox
    d, request, bound = conversations
    memory = PostgresWorkingMemory()
    for module in (state, ledger, mailbox):
        monkeypatch.setattr(module, 'MEM', memory)
    def write(i):
        with bound():
            token = handlers.tool_context(venture_id=d['vid'], workspace_id=d['wid'])
            try:
                assert handlers._ingest_feedback({'text': 'feedback-' + str(i)})['stored']
                assert ledger.add_task(d['vid'], {'id': 'card-' + str(i), 'title': 'Saved card'})
                mailbox.send(d['vid'], {'id': f'{i:03}', 'to': 'god', 'subject': 'Saved message'}, 'worker')
            finally:
                handlers.reset_tool_context(token)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(20)))
    with bound():
        assert len(memory[f'harness_feedback:{d["vid"]}']) == 20
        assert len(ledger.snapshot(d['vid'])['tasks']) == 20
        assert len(mailbox.inbox(d['vid'], 'god')) == 20
    def drain(_):
        with bound():
            return mailbox.drain_for_stop(d['vid'], 'god')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(drain, range(2)))
    assert sum(result['block'] for result in results) == 1
    assert len(next(result['messages'] for result in results if result['block'])) == 20


def test_source_bound_internal_and_portfolio_scope(conversations, monkeypatch):
    from app import state
    from app.memory.pg_working import PostgresWorkingMemory
    from app.memory.scope import bound_memory_scope
    from app.tenancy import bind, current, ScopeRequired
    from app.gbrain.cycle import cycle_one
    from app.ventures.lifecycle import list_active
    d, request, bound = conversations
    monkeypatch.setattr(state, 'MEM', PostgresWorkingMemory())
    import app.gbrain.cycle as cycle
    monkeypatch.setattr(cycle, 'MEM', state.MEM)
    with bound():
        state.MEM[f'charter:{d["vid"]}'] = {'content': 'Founder context [[people/alice]]'}
    assert cycle_one(d['vid']) >= 1
    with bind(workspace_id='none', venture_id='', actor=d['actor']):
        result = list_active(workspace_ids={d['wid']})
        assert result[0]['id'] == d['vid']
        with bound_memory_scope(d['vid']):
            assert (current().workspace_id, current().venture_id) == (d['wid'], d['vid'])
        assert current().workspace_id == 'none'
    with bind(workspace_id='none', venture_id='', actor='usr_unrelated'):
        with pytest.raises(PermissionError):
            with bound_memory_scope(d['vid']):
                pass
    with pytest.raises(ScopeRequired):
        state.MEM.get(f'charter:{d["vid"]}')


def test_json_crew_survives_new_reader_without_foreign_scope(conversations, monkeypatch):
    from app import state
    from app.engagements import crew
    from app.memory.pg_working import PostgresWorkingMemory
    from app.db.models import Engagement
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id
    d, request, bound = conversations
    identity, now = new_id('eng'), utcnow()
    with d['factory']() as session:
        session.add(Engagement(id=identity, workspace_id=d['wid'], requested_by=d['actor'], size='task',
            title='Owned crew', request_verbatim='Read source', definition_of_done='Source reviewed',
            status='planned', created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setattr(state, 'MEM', PostgresWorkingMemory())
    monkeypatch.setattr(crew, 'MEM', state.MEM)
    with bound():
        created = crew.assemble(identity, workspace_id=d['wid'], capabilities=['answer', 'review'])
    monkeypatch.setattr(crew, 'MEM', PostgresWorkingMemory())
    with bind(workspace_id=d['wid'], venture_id='', actor=d['actor']):
        assert crew.crew_for(identity) == created
        assert set(crew.retire(identity)) == set(created.ids)
        assert crew.crew_for(identity) is None
    with bind(workspace_id='wsp_foreign', venture_id='', actor=d['actor']):
        with pytest.raises(PermissionError):
            crew.crew_for(identity)


def test_shared_memory_survives_actual_process_restart(conversations):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from app.memory.pg_working import PostgresWorkingMemory
    d, request, bound = conversations
    with bound():
        PostgresWorkingMemory()[f'restart:{d["vid"]}'] = {'count': 7}
    script = '''import json, os
from app.tenancy import bind
from app.memory.pg_working import PostgresWorkingMemory
with bind(workspace_id=os.environ['TEST_WID'], venture_id=os.environ['TEST_VID'], actor=os.environ['TEST_ACTOR']):
    print(json.dumps(PostgresWorkingMemory()["restart:" + os.environ['TEST_VID']]))
'''
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, COMPANY_OS_DB_URL=d['url'], TEST_WID=d['wid'], TEST_VID=d['vid'], TEST_ACTOR=d['actor'],
               PYTHONPATH=os.pathsep.join([str(root/'services/company_os'), str(root/'packages'), str(root)]))
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {'count': 7}


def test_compaction_withholds_old_credentials_without_erasing_private_originals(conversations):
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message, list_messages
    from app.chat.compaction import context_for_question
    from app.chat.assistant import build_messages
    d, request, bound = conversations
    cid = request('POST', json={'idempotency_key': 'credential-history-1'}).json()['id']
    synthetic = 'sk-proj-' + secrets.token_hex(24)
    with bound(), bind_conversation(d['vid'], cid):
        first = append_message(d['vid'], role='user', body_md='Previously pasted ' + synthetic)
        for i in range(24):
            append_message(d['vid'], role='user', body_md='Safe remembered topic ' + str(i))
        last = append_message(d['vid'], role='user', body_md='Repeated historical ' + synthetic)
        for _ in range(2):
            value = context_for_question(d['vid'], 'Previously pasted remembered topic')
            assert synthetic not in json.dumps(value)
            assert first['id'] not in {item['source_message_id'] for item in value['items']}
            assert last['id'] not in {item['source_message_id'] for item in value['items']}
            assert value['omitted_count'] == 26 - len(value['items'])
            prompt = build_messages(question='Summarize topics', brief={'_rendered': 'Current saved state'},
                history=list_messages(d['vid']), turns=100, conversation_context=value)
            assert synthetic not in json.dumps(prompt)
        originals = list_messages(d['vid'])
        assert len(originals) == 26 and synthetic in originals[0]['body_md'] and synthetic in originals[-1]['body_md']


def test_signed_gtm_quote_cannot_cross_private_conversations(conversations, monkeypatch):
    from app.api import chat_api
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.gtm.god import generation
    d, request, bound = conversations
    first = request('POST', json={'idempotency_key': "gtm-0001"}).json()['id']
    second = request('POST', json={'idempotency_key': "gtm-0002"}).json()['id']
    with bound(), bind_conversation(d['vid'], first):
        quote = append_message(d['vid'], role='assistant', body_md='Saved private quote', kind='gtm_diagnosis_quote', actions=[])
    calls = []
    monkeypatch.setattr(generation, 'start', lambda *args, **kwargs: calls.append(True))
    monkeypatch.setattr(chat_api, '_gtm_owner', lambda *args: d['wid'])
    response = request('POST', '/chat/gtm/confirm', json={'conversation_id': second, 'quote_message_id': quote['id']})
    assert response.status_code == 404 and response.json()['detail']['error']['code'] == 'gtm_quote_not_found'
    assert calls == []


@pytest.mark.parametrize("text", ["no", "cancel", "do it", "yes"])
def test_signed_new_venture_bare_confirmation_uses_legacy_scope(conversations, monkeypatch, text):
    """New migrated ventures have no backfilled row; signed chat creates it safely."""
    from app.api import chat_api
    from app.chat import llm_router
    from app.chat.router import handle_message
    from app.db.models import ChatConversation, Job, CreditAccount

    d, request, _ = conversations
    monkeypatch.setattr(chat_api, "handle_message", handle_message)

    async def unavailable(*_args, **_kwargs):
        return None
    monkeypatch.setattr(llm_router, "route", unavailable)
    with d["factory"]() as session:
        assert session.scalar(select(func.count(ChatConversation.id)).where(ChatConversation.venture_id == d["vid"])) == 0
        before_jobs = session.scalar(select(func.count(Job.id)))
        before_credit = [(row.id, row.reserved_micro, row.spent_micro) for row in session.scalars(select(CreditAccount))]
    response = request("POST", "/chat", json={"message": text})
    assert response.status_code == 200, response.text
    assert "Nothing is waiting" in response.json()["body_md"]
    catalog = request("GET").json()
    assert response.json()["conversation_id"] == catalog["legacy_conversation_id"]
    with d["factory"]() as session:
        assert session.scalar(select(func.count(Job.id))) == before_jobs
        assert [(row.id, row.reserved_micro, row.spent_micro) for row in session.scalars(select(CreditAccount))] == before_credit
        row = session.scalar(select(ChatConversation).where(ChatConversation.venture_id == d["vid"]))
        assert row.visibility == "legacy_shared" and row.workspace_id == d["wid"]


def test_signed_bare_confirmation_does_not_borrow_other_private_card(conversations, monkeypatch):
    from app.api import chat_api
    from app.chat import llm_router
    from app.chat.conversations import bind_conversation
    from app.chat.router import handle_message
    from app.chat.store import append_message

    d, request, bound = conversations
    monkeypatch.setattr(chat_api, "handle_message", handle_message)

    async def unavailable(*_args, **_kwargs):
        return None
    monkeypatch.setattr(llm_router, "route", unavailable)
    a = request("POST", json={"idempotency_key": "bare0001"}).json()["id"]
    b = request("POST", json={"idempotency_key": "bare0002"}).json()["id"]
    with bound(), bind_conversation(d["vid"], a):
        append_message(d["vid"], role="assistant", kind="confirmation", body_md="Private pending card",
                       actions=[{"label": "Private A action", "confirmation": "private-a"}])
    response = request("POST", "/chat", json={"message": "yes", "conversation_id": b})
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == b
    assert "Nothing is waiting" in response.json()["body_md"]
    assert "Private A action" not in str(response.json())
    a_rows = request("GET", "/chat", params={"conversation_id": a}).json()["items"]
    assert len(a_rows) == 1 and a_rows[0]["kind"] == "confirmation"
    same = request("POST", "/chat", json={"message": "yes", "conversation_id": a})
    assert same.status_code == 200, same.text
    assert same.json()["conversation_id"] == a
    assert "Nothing" in same.json()["body_md"]


@pytest.mark.parametrize("text", [
    "Without running any tasks, explain what this private QA venture is for, what information you can rely on, and what is still unknown.",
    "Please summarize what information is saved for this venture and what remains unproven.",
    "Could you compare the saved purpose with the known evidence without changing anything.",
    "Advice only: help me understand the information available in this private venture.",
    pytest.param("For this existing venture, recap the pilot’s target customer and two synthetic test scenarios. Do not start tasks or change anything.", id="specific_pilot_recap"),
    pytest.param("Please recap the pilot’s target customer and the two synthetic scenarios saved for testing.", id="polite_specific_recap"),
    pytest.param("Catch me up on the pilot’s customer and the two scenarios saved for its test.", id="specific_catch_up"),
])
def test_signed_private_read_request_does_not_answer_venture_question(conversations, monkeypatch, text):
    """A new conversation's read request cannot become a paid venture idea answer."""
    from app.api import chat_api
    from app.chat.idea_bridge import pending_question
    from app.chat.router import handle_message
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, CreditReservation, Job, Workspace
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import service
    from llm_gateway import sole
    from core.clock import utcnow
    from core.ids import new_id

    d, request, bound = conversations
    monkeypatch.setattr(chat_api, "handle_message", handle_message)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    provider_calls = []

    async def forbidden_provider(*args, **kwargs):
        provider_calls.append(True)
        raise AssertionError("read-only offline regression must not dial a provider")

    monkeypatch.setattr(sole, "transmit", forbidden_provider)
    question_id = new_id("ict")
    with d["factory"]() as session:
        workspace = session.get(Workspace, d["wid"])
        workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
            "chat_entry_defer_clarification": True, "entry_goal": "startup"})
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.questions_asked = "clarifying", 1
        session.add(ClarifierTurn(id=question_id, session_id=d["sid"], workspace_id=d["wid"],
            venture_id=d["vid"], turn=1, role="agent", kind="question",
            text="Who is testing this private venture?", options="{}", created_at=utcnow()))
        session.commit()
    created = request("POST", json={"idempotency_key": "read-question-private-1"})
    assert created.status_code == 200, created.text
    cid = created.json()["id"]
    history = request("GET", "/chat", params={"conversation_id": cid}).json()["items"]
    assert history == []  # no question card was offered in this fresh private chat
    # Ordinary chat reads initialize this account through the venture brief.
    # Establish it before measuring unchanged spend, holds and account identity.
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with bound():
        assert pending_question(d["vid"], "", force=True) == (d["sid"], question_id)
    with d["factory"]() as session:
        before_jobs = session.scalar(select(func.count(Job.id)))
        before_holds = session.scalar(select(func.count(CreditReservation.id)))
        before_credit = [(r.id, r.reserved_micro, r.spent_micro) for r in session.scalars(select(CreditAccount))]
        before_turns = [(r.id, r.role, r.kind, r.text) for r in session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"]).order_by(ClarifierTurn.turn))]
    answer_calls = []
    original = service.post_message

    def recorded_answer(*args, **kwargs):
        answer_calls.append((args[0], kwargs.get("expected_question_id")))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "post_message", recorded_answer)
    response = request("POST", "/chat", json={"message": text, "conversation_id": cid})
    assert response.status_code == 200, response.text
    assert answer_calls == []
    assert provider_calls == []
    reply = response.json()
    assert reply["conversation_id"] == cid and reply["kind"] != "idea_clarification_pending"
    assert "saved your answer" not in reply["body_md"].lower()
    assert not any(a.get("confirmation") for a in reply.get("actions") or [])
    with bound():
        assert pending_question(d["vid"], "", force=True) == (d["sid"], question_id)
    with d["factory"]() as session:
        assert session.scalar(select(func.count(Job.id))) == before_jobs
        assert session.scalar(select(func.count(CreditReservation.id))) == before_holds
        assert [(r.id, r.reserved_micro, r.spent_micro) for r in session.scalars(select(CreditAccount))] == before_credit
        assert [(r.id, r.role, r.kind, r.text) for r in session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"]).order_by(ClarifierTurn.turn))] == before_turns
    rows = request("GET", "/chat", params={"conversation_id": cid}).json()["items"]
    # An escaped request is marked so the clarifier never treats it as an answer.
    assert [row["role"] for row in rows] == ["user", "assistant"] and rows[1]["kind"] == reply["kind"]
    assert rows[0]["kind"] in {"text", "questionnaire_request"}
    assert rows[0]["body_md"] == text
    # The same saved question retains explicit answer authority. Wording alone
    # is refused above; a current owned question ID is separately authoritative.
    with bound():
        assert pending_question(d["vid"], text, force=True) == (d["sid"], question_id)
        assert pending_question("ven_missing", text, force=True) is None
    if text.startswith("For this existing venture, recap"):
        answered = request("POST", "/chat", json={"message": text,
            "conversation_id": cid, "reply_to_question": True, "question_id": question_id})
        assert answered.status_code == 200, answered.text
        # Founder rule (2026-10-30): a request typed while a question card is open is done, not saved as the answer.
        assert answered.json()["kind"] != "idea_clarification_pending"
        assert answer_calls == []
        assert provider_calls == []
        with d["factory"]() as session:
            assert session.scalar(select(func.count(Job.id))) == before_jobs
            assert session.scalar(select(ClarifierTurn).where(
                ClarifierTurn.session_id == d["sid"], ClarifierTurn.role == "founder")) is None
