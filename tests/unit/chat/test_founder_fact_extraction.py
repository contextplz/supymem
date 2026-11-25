"""Founder replies never wait for optional model fact extraction."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event
from time import monotonic

import pytest

from tests.unit.gtm.test_journey import journey_db


@pytest.mark.parametrize("audience", ["independent cargo surveyors", "families restoring old houses"])
def test_local_literal_memory_needs_no_legacy_flag(journey_db, monkeypatch, audience):
    from app.config import settings
    from app.db.models import Venture, VentureDocument
    from app.ventures import founder_facts as facts, startup_review

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(settings, "new_user_flow", "off")
    data = journey_db
    with data["factory"]() as session:
        marker = session.get(VentureDocument, f"internal_new_user_flow:{data['vid']}")
        if marker:
            session.delete(marker)
            session.flush()
        venture = session.get(Venture, data["vid"])
        startup_review.pin_at_creation(session, venture)
        facts.capture(session, venture, f"Our customers are {audience}.")
        assert facts.read(session, venture)["audience"]["value"] == audience


@pytest.mark.parametrize("audience,recipient", [
    ("warehouse operators", "my logistics pilot reviewers"),
    ("home gardeners", "my community workshop reviewers"),
])
def test_work_instructions_preserve_saved_customer_and_allow_corrections(journey_db, audience, recipient):
    from app.db.models import Venture
    from app.chat.store import append_message
    from app.ventures import founder_facts as facts, startup_review

    with journey_db["factory"]() as session:
        startup_review._document(session, journey_db["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
        venture = session.get(Venture, journey_db["vid"])
        facts.capture(session, venture, f"Our customers are {audience}.")
        assert facts.read(session, venture)["audience"]["value"] == audience
        session.commit()
    instruction = f"Create an execution plan for {recipient}. Do not build or publish anything. Keep my target customer unchanged."
    append_message(journey_db["vid"], role="user", body_md=instruction)
    extracted = {field: None for field in facts.FIELDS}
    extracted["audience"] = {"value": recipient, "quote": instruction, "source": "founder message", "confidence": .95}
    assert not facts.validated_extraction(extracted, instruction, "founder message")
    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        assert facts.read(session, venture)["audience"]["value"] == audience
        facts.capture(session, venture, f"Correction: our customers are regional {audience}.")
        assert facts.read(session, venture)["audience"]["value"] == "regional " + audience


@pytest.fixture()
def background(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.ventures import founder_facts as facts, startup_review
    with journey_db["factory"]() as session:
        startup_review._document(session, journey_db["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
    pool = ThreadPoolExecutor(max_workers=2)
    release, started = Event(), Event()
    monkeypatch.setattr(facts, "_POOL", pool)
    monkeypatch.setattr(facts, "_SLOTS", BoundedSemaphore(2))
    monkeypatch.setenv("LLM_OFFLINE", "0")
    if is_local():
        from app.core.local_secrets import update_settings

        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
        update_settings("capabilities", {"background_work": True}, {})
    yield facts, journey_db, started, release
    release.set()
    pool.shutdown(wait=True)


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("audience", ["independent bakery owners", "families planning weekend outings"])
def test_append_message_does_not_wait_for_hanging_or_failing_extraction(background, monkeypatch, caplog, fails, audience):
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.tenancy import current
    facts, data, started, release = background
    actors = []

    async def extract(*args, **kwargs):
        actors.append(current().actor)
        started.set()
        if fails:
            raise TimeoutError("must not leak founder text or provider details")
        release.wait(3)

    monkeypatch.setattr(facts, "extract", extract)
    before = monotonic()
    message = f"We sell to {audience}."
    reply = append_message(data["vid"], role="user", body_md=message)
    assert monotonic() - before < 1
    assert reply["body_md"] == message
    assert started.wait(1)
    assert actors == [data["actor"]]
    with data["factory"]() as session:
        assert facts.read(session, session.get(Venture, data["vid"]))["audience"]["value"] == audience
    release.set()
    facts._POOL.shutdown(wait=True)
    if fails:
        assert [record.message for record in caplog.records if record.name == facts.__name__] == ["founder_fact_extraction_unavailable"]


@pytest.mark.parametrize("idea", [
    "A purchase-order reconciliation app for independent hospital buyers",
    "A family shopping list app for households managing food allergies",
])
def test_entry_start_replies_without_waiting_for_model(background, monkeypatch, idea):
    from app.entry import chat_start
    facts, data, started, release = background
    finished = Event()

    async def extract(*args, **kwargs):
        started.set()
        release.wait(10)
        finished.set()

    monkeypatch.setattr(facts, "extract", extract)
    proposal = chat_start.preview(text=idea,
        actor_id=data["actor"], target_venture_id=data["vid"])
    result = asyncio.run(chat_start.confirm(proposal["proposal"]["id"], data["actor"]))
    assert result["status"] == "committed" and result["message"]
    assert started.wait(5)
    assert not finished.is_set()
    release.set()


def test_new_venture_commit_keeps_founder_messages_in_separate_bubbles(journey_db):
    from app.entry import chat_start
    from app.db.models import ChatMessage
    from sqlalchemy import select
    for idea, answer in [("A startup to reconcile supplier invoices for manufacturers", "Finance teams review disputed charges"),
                         ("A startup to help families plan accessible holidays", "Parents choose activities for their children")]:
        text = idea + "\nClarification: " + answer
        preview = chat_start.preview(text=text, actor_id=journey_db["actor"])
        proposal = preview["proposal"]
        result = asyncio.run(chat_start.confirm(proposal["id"], journey_db["actor"]))
        assert result["status"] == "committed"
        with journey_db["factory"]() as session:
            rows = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == result["venture_id"],
                ChatMessage.role == "user").order_by(ChatMessage.created_at)).all()
            assert [row.body_md for row in rows] == [idea, answer]
        assert asyncio.run(chat_start.confirm(proposal["id"], journey_db["actor"])) == result
        with journey_db["factory"]() as session:
            assert session.query(ChatMessage).filter_by(venture_id=result["venture_id"], role="user").count() == 2


@pytest.mark.parametrize("idea", [
    "A startup to reconcile supplier invoices for manufacturers",
    "A startup to help families plan accessible holidays",
])
def test_new_venture_commit_restores_source_conversation(journey_db, monkeypatch, idea):
    from app.chat.conversations import bind_conversation, current_conversation, create
    from app.chat.store import list_messages
    from app.entry import chat_start
    from app.entry.service import venture_scope

    data = journey_db
    conversation_id = create(data["vid"], idempotency_key="new-venture-source")["id"]
    with bind_conversation(data["vid"], conversation_id) as source:
        before = list_messages(data["vid"])
        proposal = chat_start.preview(text=idea, actor_id=data["actor"])["proposal"]
        receipt = asyncio.run(chat_start.confirm(proposal["id"], data["actor"]))
        assert receipt["status"] == "committed" and receipt["venture_id"] != data["vid"]
        assert current_conversation() == source and list_messages(data["vid"]) == before
        assert asyncio.run(chat_start.confirm(proposal["id"], data["actor"])) == receipt
        from app.chat import new_venture

        async def failed_receipt(*args, **kwargs):
            raise RuntimeError("The destination write failed after creation")

        monkeypatch.setattr(chat_start, "confirm", failed_receipt)
        recovered = asyncio.run(new_venture.execute(data["vid"], {"proposal_id": proposal["id"]}, data["actor"]))
        assert "was created" in recovered["body_md"] and "Nothing was created" not in recovered["body_md"]
        assert recovered["actions"][0]["href"] == f"/v/{receipt['venture_id']}"
    with venture_scope(receipt["venture_id"], actor_id=data["actor"], workspace_id=receipt["workspace_id"]):
        assert any(message["body_md"] == idea for message in list_messages(receipt["venture_id"]))


@pytest.mark.parametrize("source_idea,idea", [
    ("A family meal planner", "Start a new venture: an invoice tool. Customers are shipping companies. Pricing: per container."),
    ("An inventory tool for factories", "Start a new venture: a holiday planner. Customers are families with toddlers. Pricing: per trip."),
])
def test_separate_venture_never_enters_current_facts_evidence_or_prompt(journey_db, monkeypatch, source_idea, idea):
    from app.chat import assistant
    from app.chat.store import append_message, list_messages
    from app.config import settings
    from app.db.models import Venture
    from app.intake.evidence import save_statement, venture_evidence
    from app.ventures import founder_facts as facts, startup_review
    from types import SimpleNamespace

    data = journey_db
    submitted = []
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(facts, "_POOL", SimpleNamespace(submit=lambda *args: submitted.append(args)))
    for source in ("founder message", "clarification", "gstack"):
        assert facts.literal_facts(idea, source=source, field="audience") == {}
    extracted = dict.fromkeys(facts.FIELDS)
    extracted["audience"] = {"value": idea, "quote": idea, "source": "founder message", "confidence": .95}
    assert facts.validated_extraction(extracted, idea, "founder message") == {}
    monkeypatch.setattr(settings, "new_user_flow", "on")
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = source_idea
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": facts._fact(source_idea, "founder message"),
                               "pricing": facts._fact("A monthly subscription", "founder message")}})
        session.commit()
        before = facts.read(session, session.get(Venture, data["vid"]))
        assert before["audience"]["value"] == source_idea and before["pricing"]["value"] == "A monthly subscription"
    save_statement(data["vid"], source_idea)
    evidence = venture_evidence(data["vid"])
    save_statement(data["vid"], idea)
    append_message(data["vid"], role="user", body_md=idea)
    append_message(data["vid"], role="assistant", body_md="Your separate venture proposal: " + idea)
    with data["factory"]() as session:
        assert facts.read(session, session.get(Venture, data["vid"])) == before
    assert venture_evidence(data["vid"]) == evidence
    assert not submitted
    history = list_messages(data["vid"])
    assert any(message["body_md"] == idea for message in history)
    messages = assistant.build_messages(question="What do you remember?", brief={"idea": {"raw_idea": source_idea}}, history=history)
    assert idea not in str(messages)
    messages = assistant.build_messages(question="What do you remember?", brief={"idea": {"raw_idea": source_idea}}, history=history[-1:])
    assert idea not in str(messages)
    from app.chat.compaction import context_for_question
    assert idea not in str(context_for_question(data["vid"], "What do you remember?"))


def test_saturated_pool_never_blocks_or_submits_more_model_calls(background, monkeypatch, caplog):
    from app.chat.store import append_message
    facts, data, started, release = background
    calls = []

    async def extract(*args, **kwargs):
        calls.append(args[1])
        started.set()
        release.wait(3)

    monkeypatch.setattr(facts, "extract", extract)
    append_message(data["vid"], role="user", body_md="Pricing: twenty dollars")
    assert started.wait(1)
    append_message(data["vid"], role="user", body_md="Progress: zero paying customers")
    before = monotonic()
    append_message(data["vid"], role="user", body_md="Priorities: book customer calls")
    assert monotonic() - before < 1
    assert len(calls) <= 2
    assert "founder_fact_extraction_busy" in caplog.messages


@pytest.mark.parametrize("audience", ["independent cargo surveyors", "families restoring old houses"])
def test_saved_background_switch_stops_optional_extraction(background, monkeypatch, audience):
    from app import main
    from app.chat.store import append_message
    from app.db.models import Venture
    from tests.company_os_harness import company_os_test_client

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    facts, data, started, release = background
    submitted = []
    monkeypatch.setattr(facts._POOL, "submit", lambda *args: submitted.append(args))
    with company_os_test_client(main.create_app()) as client:
        response = client.put("/local/capabilities/background_work", json={"on": False})
        assert response.status_code == 200, response.text
    append_message(data["vid"], role="user", body_md=f"Our customers are {audience}.")
    assert submitted == []
    with data["factory"]() as session:
        assert facts.read(session, session.get(Venture, data["vid"]))["audience"]["value"] == audience
