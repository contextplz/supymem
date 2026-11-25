"""Actual signed chat commands reach shared memory without a provider or job."""
import asyncio
import secrets

import pytest
from sqlalchemy import func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_planning import planning as planning  # noqa: F401


@pytest.fixture
def public_memory(planning, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api import chat_api, voice_api
    from app.chat import llm_router
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    import app.memory as memory
    d = planning
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setattr(memory, "_ADAPTER", PostgresMemoryAdapter())
    async def forbidden_model(*args, **kwargs):
        raise AssertionError("explicit memory command must not call a model")
    monkeypatch.setattr(llm_router, "route", forbidden_model)
    app = FastAPI()
    app.include_router(chat_api.router)
    app.include_router(voice_api.router)
    client = TestClient(app)
    def request(method, suffix="/chat", actor=None, **kwargs):
        path = f"/ventures/{d['vid']}" + suffix
        subject = actor or d["actor"]
        assertion = sign_assertion(secret, sub=subject, email="", role="user", path=path, method=method)
        return client.request(method, path, headers={"X-COS-Assertion": assertion, "X-Subject": subject}, **kwargs)
    def post(text, cid=None, actor=None):
        return request("POST", actor=actor, json={"message": text, "conversation_id": cid})
    return d, request, post


def _memory_id(response):
    from app.db.models import ChatMessage, InstitutionalMemory
    from app.memory.model import MemoryRecord
    from app.state import db

    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] in {"memory_saved", "memory_corrected"}, response.text
    with db()() as session:
        source = session.get(ChatMessage, reply["reply_to_message_id"])
        assert source is not None and source.role == "user"
        assert reply["citations"] == [{"label": "Your saved note",
            "href": f"/v/{source.venture_id}/settings#settings-memory"}]
        records = [MemoryRecord.model_validate_json(value) for value in session.scalars(
            select(InstitutionalMemory.record_json).where(
                InstitutionalMemory.workspace_id == source.workspace_id,
                InstitutionalMemory.venture_id == source.venture_id))]
        [record] = [record for record in records if record.evidence == ["chat:" + source.id]]
        assert record.author == source.author_person_id
        return record.mem_id


def test_public_common_remember_syntax_saves_exactly_once_without_provider_or_work(public_memory):
    from app.db.models import ChatMessage, CreditLedgerEntry, CreditReservation, InstitutionalMemory, Job
    from app.db.supymem_models import FounderAction
    from app.memory import get_memory
    from app.memory.model import RecallQuery
    from app.tenancy import bind
    d, request, post = public_memory
    counted = [Job, FounderAction, CreditLedgerEntry, CreditReservation]
    with d["factory"]() as session:
        before = {model.__name__: session.scalar(select(func.count(model.id))) for model in counted}
    cid = request("POST", "/chat/conversations", json={"idempotency_key": "memory-colon-private"}).json()["id"]
    variants = ["Remember:", "Please remember:", "Remember that:", "Please remember this:",
                "Remember that", "Please remember this"]
    saved = {}
    for index, prefix in enumerate(variants):
        statement = ('For this QA venture, I prefer concise labels and complete saved memory statements.'
                     if index == 0 else f'I prefer complete statement {index}, including the label "Why?".')
        raw = prefix + " " + statement
        identity = _memory_id(post(raw, cid))
        repeated = post(raw, cid)
        repeated_id = _memory_id(repeated)
        assert repeated_id != identity and repeated.json()["actions"] == []
        saved[identity] = statement
        saved[repeated_id] = statement
        with d["factory"]() as session:
            sources = list(session.scalars(select(ChatMessage).where(
                ChatMessage.venture_id == d["vid"], ChatMessage.role == "user", ChatMessage.body_md == raw)))
            assert len(sources) == 2 and all(row.conversation_id == cid and row.author_person_id == d["actor"] for row in sources)
            assert session.scalar(select(func.count(InstitutionalMemory.id)).where(
                InstitutionalMemory.venture_id == d["vid"])) == 2 * (index + 1)
    fresh = request("POST", "/chat/conversations", json={"idempotency_key": "memory-colon-next-private"}).json()["id"]
    assert post("Remember: I prefer a foreign scope.", cid, actor="usr_foreign_memory_colon").status_code == 404
    listed = post("List saved venture memories", fresh)
    assert listed.status_code == 200 and listed.json()["kind"] == "memory_list"
    assert {c["label"] for c in listed.json()["citations"]} == set(saved)
    for identity, statement in saved.items():
        assert listed.json()["body_md"].count(identity) == 1 and statement in listed.json()["body_md"]
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert {row.mem_id: row.statement for row in asyncio.run(get_memory().recall(
            RecallQuery(venture_id=d["vid"], text="", limit=100)))} == saved
    with d["factory"]() as session:
        assert {model.__name__: session.scalar(select(func.count(model.id))) for model in counted} == before
    assert listed.json()["actions"] == [] and d["calls"] == []


def test_public_malformed_remember_syntax_is_guidance_without_write_or_provider(public_memory):
    from app.db.models import CreditLedgerEntry, CreditReservation, InstitutionalMemory, Job
    from app.db.supymem_models import FounderAction
    d, _, post = public_memory
    counted = [Job, FounderAction, CreditLedgerEntry, CreditReservation, InstitutionalMemory]
    with d["factory"]() as session:
        before = {model.__name__: session.scalar(select(func.count(model.id))) for model in counted}
    for text in ["Remember:", "Please remember", "Remember that:", "Please remember this:",
                 "Remember:: complete labels", "Remember what did I say?"]:
        response = post(text)
        assert response.status_code == 200 and 'Remember: <complete statement>' in response.json()["body_md"]
        assert response.json()["actions"] == [] and response.json()["citations"] == []
    with d["factory"]() as session:
        assert {model.__name__: session.scalar(select(func.count(model.id))) for model in counted} == before
    assert d["calls"] == []


def test_public_remember_new_chat_correct_plan_brief_forget(public_memory):
    from app.db.models import ChatMessage, InstitutionalMemory, Job
    from app.memory.model import MemoryRecord
    from app.planning.execution import inputs
    from app.tenancy import bind
    from app.ventures.state_brief import get_venture_state_brief
    d, request, post = public_memory
    with d["factory"]() as session:
        before_jobs = session.scalar(select(func.count(Job.id)))
    first_cid = request("POST", "/chat/conversations", json={"idempotency_key": "memory-private-first"}).json()["id"]
    mid = _memory_id(post("Remember that I prefer short answers and complete source references.", first_cid))
    second_cid = request("POST", "/chat/conversations", json={"idempotency_key": "memory-private-second"}).json()["id"]
    assert first_cid != second_cid
    assert request("GET", params={"conversation_id": second_cid}).json()["items"] == []
    listed = post("List saved venture memories", second_cid)
    assert listed.status_code == 200 and mid in listed.json()["body_md"]
    replacement = "I prefer detailed answers — with complete source references at http://localhost:4500; never treat a model's claim as verified work."
    new_id = _memory_id(post(f"Correct memory {mid}: {replacement}", second_cid))
    assert new_id != mid
    assert replacement in post("List saved venture memories", second_cid).json()["body_md"]
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        brief = get_venture_state_brief(d["vid"])
        assert [x["statement"] for x in brief["memory"]["preferences"]] == [replacement]
        with d["factory"]() as session:
            plan = inputs(session, d["wid"], d["vid"])
        assert [x["statement"] for x in plan["context"]["institutional_memory"]["statements"]] == [replacement]
    forgotten = post(f"Forget memory {new_id}", second_cid)
    assert forgotten.status_code == 200 and forgotten.json()["kind"] == "memory_forgotten", forgotten.text
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert get_venture_state_brief(d["vid"])["memory"]["preferences"] == []
    with d["factory"]() as session:
        old = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, mid).record_json)
        new = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, new_id).record_json)
        assert old.superseded_by == new_id and old.valid_to is not None
        assert new.tombstoned is True and new.statement == replacement
        sources = list(session.scalars(select(ChatMessage).where(ChatMessage.role == "user")))
        assert any(x.body_md == f"Correct memory {mid}: {replacement}" and x.conversation_id == second_cid for x in sources)
        assert any(x.body_md.startswith("Remember that ") and x.conversation_id == first_cid for x in sources)
        assert session.scalar(select(func.count(Job.id))) == before_jobs
    assert d["calls"] == []


def test_public_wrong_actor_private_conversation_and_foreign_target_refuse(public_memory):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember, InstitutionalMemory
    from app.memory.model import MemoryRecord
    from core.clock import utcnow
    from core.ids import new_id
    d, request, post = public_memory
    other = "usr_memory_member_" + secrets.token_hex(5)
    ensure_subject_person(other)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id=new_id("wmb"), workspace_id=d["wid"], person_id=other, role="member", created_at=utcnow()))
        session.commit()
    own_cid = request("POST", "/chat/conversations", json={"idempotency_key": "memory-owner-private"}).json()["id"]
    mid = _memory_id(post("Remember that I prefer a reviewed source before launch.", own_cid))
    assert post(f"Forget memory {mid}", own_cid, actor=other).status_code == 404
    other_cid = request("POST", "/chat/conversations", actor=other, json={"idempotency_key": "memory-member-private"}).json()["id"]
    refusal = post(f"Forget memory {mid}", other_cid, actor=other)
    assert refusal.status_code == 200 and "No memory was changed" in refusal.json()["body_md"]
    assert mid not in post("List saved venture memories", other_cid, actor=other).json()["body_md"]
    assert post(f"Correct memory {mid}: I prefer another launch.", own_cid, actor="usr_foreign_memory").status_code == 404
    with d["factory"]() as session:
        record = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, mid).record_json)
        assert not record.tombstoned and record.valid_to is None
    assert d["calls"] == []


def test_public_secret_correction_and_malformed_commands_do_not_change_memory(public_memory):
    from app.db.models import InstitutionalMemory
    from app.memory.model import MemoryRecord
    d, _, post = public_memory
    mid = _memory_id(post("Remember that I prefer safe complete notes."))
    credential = "sk-" + secrets.token_hex(24)
    reply = post(f"Correct memory {mid}: my API key is {credential}")
    assert reply.status_code == 200 and credential not in reply.text
    malformed = post(f"Forget memory {mid}; send an email")
    assert malformed.status_code == 200 and "Use an exact ID" in malformed.json()["body_md"]
    with d["factory"]() as session:
        record = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, mid).record_json)
        assert record.valid_to is None and not record.tombstoned
        assert session.scalar(select(func.count(InstitutionalMemory.id)).where(InstitutionalMemory.venture_id == d["vid"])) == 1
    assert d["calls"] == []


def test_public_memory_target_cannot_cross_workspace_or_venture(public_memory):
    from app.chat.store import append_message
    from app.db.models import InstitutionalMemory, Venture
    from app.gbrain.verbs import remember
    from app.memory.model import MemoryRecord
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id
    d, _, post = public_memory
    other_workspace = create_workspace(kind="company", name="Separate saved context", owner_id=d["actor"])
    other_vid = new_id("ven")
    with d["factory"]() as session:
        now = utcnow()
        session.add(Venture(id=other_vid, workspace_id=other_workspace.id, name="Separate venture",
                           raw_idea="Separate context", stage="draft", autonomy_level="L1",
                           created_at=now, updated_at=now))
        session.commit()
    with bind(workspace_id=other_workspace.id, venture_id=other_vid, actor=d["actor"]):
        source = append_message(other_vid, role="user", body_md="Remember that I prefer an independent workspace.")
        target = asyncio.run(remember(venture_id=other_vid, fact="I prefer an independent workspace.",
                                     provenance="chat:" + source["id"], kind="preference"))["id"]
    for command in [f"Forget memory {target}", f"Correct memory {target}: I prefer a changed workspace."]:
        reply = post(command)
        assert reply.status_code == 200 and "No memory was changed" in reply.json()["body_md"]
    assert target not in post("List saved venture memories").json()["body_md"]
    with d["factory"]() as session:
        foreign = MemoryRecord.model_validate_json(session.get(InstitutionalMemory, target).record_json)
        assert foreign.valid_to is None and not foreign.tombstoned
    assert d["calls"] == []


def test_correction_source_target_and_private_source_are_revalidated(public_memory):
    from app.chat.conversations import bind_conversation
    from app.chat.memory_commands import dispatch
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.memory import get_memory
    from app.memory.model import MemoryWrite, MemoryKind, RecallQuery
    from app.tenancy import bind
    d, request, post = public_memory
    cid = request("POST", "/chat/conversations", json={"idempotency_key": "memory-source-first"}).json()["id"]
    other_cid = request("POST", "/chat/conversations", json={"idempotency_key": "memory-source-second"}).json()["id"]
    old_id = _memory_id(post("Remember that I prefer short notes.", cid))
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), bind_conversation(d["vid"], cid):
        source = append_message(d["vid"], role="user", body_md=f"Correct memory {old_id}: I prefer long notes.")
        write = MemoryWrite(venture_id=d["vid"], kind=MemoryKind.FOUNDER_PREFERENCE,
            subject="general", statement="I prefer long notes.", evidence=["chat:" + source["id"]],
            author=d["actor"], confidence=1, dedupe_key="exact-correction-source")
        with pytest.raises(ValueError, match="memory_correction_target_mismatch"):
            asyncio.run(get_memory().remember(write))
        with pytest.raises(ValueError, match="memory_correction_target_mismatch"):
            # Existing target is intentionally not the source-selected target.
            second = _memory_id(post("Remember that I prefer exact evidence.", cid))
            asyncio.run(get_memory().supersede(second, by=write, reason="wrong target"))
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), bind_conversation(d["vid"], other_cid):
        reply = asyncio.run(dispatch(d["vid"], source["id"]))
        assert "No memory was changed" in reply["body_md"]
    new_id = _memory_id(post(f"Correct memory {old_id}: I prefer long notes.", cid))
    with d["factory"]() as session:
        current = session.scalar(select(ChatMessage).where(ChatMessage.body_md == f"Correct memory {old_id}: I prefer long notes.").order_by(ChatMessage.created_at.desc()))
        current.body_md = "Correct memory mem_wrong_target: I prefer long notes."
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        hits = asyncio.run(get_memory().recall(RecallQuery(venture_id=d["vid"], text="")))
        assert new_id not in {hit.mem_id for hit in hits}


def test_non_source_qualified_local_adapter_cannot_claim_exact_list_or_mutation(public_memory, monkeypatch):
    import app.memory as memory
    d, _, post = public_memory
    class UnsupportedLocal:
        name = "local"
        async def recall(self, query):
            raise AssertionError("public source-backed listing must not trust a local replica")
    monkeypatch.setattr(memory, "_ADAPTER", UnsupportedLocal())
    for command in ["List saved venture memories", "Forget memory mem_local",
                    "Correct memory mem_local: I prefer a reviewed record."]:
        reply = post(command)
        assert reply.status_code == 200 and "unavailable in this storage mode" in reply.json()["body_md"]
        assert reply.json()["citations"] == [] and reply.json()["actions"] == []
    assert d["calls"] == []


def test_public_memory_list_preserves_whole_items_under_response_byte_bound(public_memory):
    from app.chat.memory_commands import LIST_BYTES
    d, _, post = public_memory
    statements = {}
    for index in range(8):
        statement = "I prefer " + "完整引用和来源记录。" * 90 + f" entry {index}."
        statements[_memory_id(post("Remember that " + statement))] = statement
    listed = post("List saved venture memories")
    assert listed.status_code == 200 and listed.json()["kind"] == "memory_list"
    body = listed.json()["body_md"]
    assert len(body.encode()) <= LIST_BYTES and "Omitted " in body
    kept = {item["label"] for item in listed.json()["citations"]}
    assert 0 < len(kept) < len(statements)
    for identity, statement in statements.items():
        assert (statement in body) is (identity in kept)
    assert d["calls"] == []


def test_voice_summary_routes_founder_memory_once_in_owned_private_chat(public_memory):
    from app.analytics.events import emit
    from app.db.models import ChatMessage, InstitutionalMemory
    d, request, _post = public_memory
    conversation = request("POST", "/chat/conversations", json={"idempotency_key":"voice-memory-private"}).json()["id"]
    receipt = emit("voice.session_started", person_id=d["actor"], workspace_id=d["wid"], venture_id=d["vid"], props={})
    statement = "I prefer the newest audience: independent architects. Do not publish."
    payload = {"session_event_id":receipt, "conversation_id":conversation, "turns":[
        {"id":"founder_1", "role":"founder", "text":"Remember that " + statement, "posted":False}]}
    first = request("POST", "/voice/summary", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["pending"] is False
    second = request("POST", "/voice/summary", json=payload)
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    assert request("POST", "/voice/summary", json=payload, actor="usr_foreign_voice").status_code == 404
    assert request("GET").json()["items"] == []
    with d["factory"]() as session:
        saved = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == d["vid"], ChatMessage.kind == "voice_summary")))
        assert len(saved) == 1 and saved[0].conversation_id == conversation
        assert session.scalar(select(func.count(InstitutionalMemory.id)).where(InstitutionalMemory.venture_id == d["vid"])) == 1
    assert d["calls"] == []


@pytest.mark.parametrize("later_posted", [True, False])
@pytest.mark.parametrize("earlier_state", [None, "pending", "unknown"])
def test_voice_summary_never_applies_older_unposted_words_after_later_completed_turn(public_memory, later_posted, earlier_state):
    from app.analytics.events import emit
    from app.db.models import ChatMessage, InstitutionalMemory
    from app.db.supymem_models import FounderRequest
    from app.tenancy import bind
    from app.ventures.state_brief import get_venture_state_brief
    d, request, _post = public_memory
    conversation = request("POST", "/chat/conversations", json={"idempotency_key":"voice-order-private"}).json()["id"]
    receipt = emit("voice.session_started", person_id=d["actor"], workspace_id=d["wid"], venture_id=d["vid"], props={})
    earlier = "Remember that I prefer the audience to be dentists."
    latest_statement = "I prefer the newest audience to be independent architects."
    later = "Remember that " + latest_statement
    if earlier_state:
        from app.chat.conversations import bind_conversation
        from app.chat import requests as admissions
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), bind_conversation(d["vid"], conversation):
            admission, _ = admissions.admit(d["vid"], d["actor"], f"voice_{receipt}_earlier",
                {"text":earlier, "reply_to_question":False, "question_id":None, "source":"voice"})
            if earlier_state == "unknown":
                admissions.uncertain(admission)
    live = request("POST", json={"message":later, "conversation_id":conversation,
        "source":"voice", "idempotency_key":f"voice_{receipt}_later"})
    _memory_id(live)
    with d["factory"]() as session:
        before = session.scalar(select(func.count(FounderRequest.id)).where(FounderRequest.venture_id == d["vid"]))
    payload = {"session_event_id":receipt, "conversation_id":conversation, "turns":[
        {"id":"earlier", "role":"founder", "text":earlier, "posted":False},
        {"id":"later", "role":"founder", "text":later, "posted":later_posted}]}
    first = request("POST", "/voice/summary", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["needs_edit"] is (earlier_state is None)
    assert first.json()["pending"] is bool(earlier_state)
    assert earlier in first.json()["body_md"] and later in first.json()["body_md"]
    second = request("POST", "/voice/summary", json=payload)
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    assert second.json()["needs_edit"] is (earlier_state is None)
    assert second.json()["pending"] is bool(earlier_state)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert [item["statement"] for item in get_venture_state_brief(d["vid"])["memory"]["preferences"]] == [latest_statement]
    with d["factory"]() as session:
        assert session.scalar(select(func.count(FounderRequest.id)).where(FounderRequest.venture_id == d["vid"])) == before
        assert session.scalar(select(func.count(InstitutionalMemory.id)).where(InstitutionalMemory.venture_id == d["vid"])) == 1
        assert not session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == d["vid"], ChatMessage.role == "user", ChatMessage.body_md == earlier))
        assert session.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.venture_id == d["vid"], ChatMessage.kind == "voice_summary")) == 1
        if earlier_state:
            assert session.get(FounderRequest, admission.id).outcome == earlier_state
    assert d["calls"] == []
