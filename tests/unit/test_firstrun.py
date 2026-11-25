"""Your first screen and saved answers work across unrelated products."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db


@pytest.mark.parametrize("mode", ["idea", "product", "existing_company"])
@pytest.mark.parametrize("idea,audience", [
    ("A tool to reconcile supplier invoices for manufacturers", "Finance teams at manufacturers"),
    ("An app to plan accessible holidays for families", "Parents planning family holidays"),
])
def test_entry_creation_keeps_its_receipt_without_a_welcome(journey_db, idea, audience, mode):
    from app.db.models import ChatMessage, Venture
    from app.entry import chat_start, service

    data = journey_db
    proposal = chat_start.preview(text=idea, actor_id=data["actor"])["proposal"]
    receipt = asyncio.run(chat_start.confirm(proposal["id"], data["actor"]))
    assert asyncio.run(chat_start.confirm(proposal["id"], data["actor"])) == receipt
    with data["factory"]() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == receipt["venture_id"])).all()
        assert len(messages) == 2 and {message.role for message in messages} == {"user", "assistant"}
        assert all(message.kind != "workspace_welcome" for message in messages)
    direct = asyncio.run(service.enter(text=idea, explicit_mode=mode, options={"start": False}, actor_id=data["actor"]))
    assert direct["committed"] is True
    with data["factory"]() as session:
        assert session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == direct["venture_id"],
            ChatMessage.kind == "workspace_welcome")) is None
    with service.venture_scope(receipt["venture_id"], actor_id=data["actor"], workspace_id=receipt["workspace_id"]):
        service._welcome(receipt["venture_id"], receipt["workspace_id"])
        service._welcome(receipt["venture_id"], receipt["workspace_id"])
    with data["factory"]() as session:
        rows = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == receipt["venture_id"],
            ChatMessage.kind == "workspace_welcome").order_by(ChatMessage.created_at, ChatMessage.id)).all()
        assert rows[0].kind == "workspace_welcome"
        assert sum(row.kind == "workspace_welcome" for row in rows) == 1
        assert "you" in rows[0].body_md and "\u2014" not in rows[0].body_md
        actions = json.loads(rows[0].actions_json)
        assert [action["label"] for action in actions] == ["How to use Supymem"]
        assert actions[0]["href"] == f'/v/{receipt["venture_id"]}/guide'
        venture = session.get(Venture, receipt["venture_id"])
        sibling = Venture(id=receipt["venture_id"] + "_sibling", workspace_id=venture.workspace_id,
            name=audience, raw_idea=idea, stage="draft", created_at=venture.created_at, updated_at=venture.updated_at)
        session.add(sibling)
        session.commit()
        sibling_id = sibling.id
    with service.venture_scope(sibling_id, actor_id=data["actor"], workspace_id=receipt["workspace_id"]):
        service._welcome(sibling_id, receipt["workspace_id"])
    with data["factory"]() as session:
        assert session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == sibling_id,
            ChatMessage.kind == "workspace_welcome")) is None


@pytest.mark.parametrize("idea,audience", [
    ("A tool to reconcile supplier invoices for manufacturers", "Finance teams at manufacturers"),
    ("An app to plan accessible holidays for families", "Parents planning family holidays"),
])
def test_welcome_only_starts_main_chat(journey_db, monkeypatch, idea, audience):
    from types import SimpleNamespace
    from app.api.schemas import CreateVenture
    from app.api.ventures import create_venture
    from app.chat import conversations, store
    from app.entry import service
    from app.onboarding import spawn

    async def start_onboarding(*args, **kwargs):
        return SimpleNamespace(id="run_first", status="queued", phase="classify", phases_done=0, phases_total=1)

    monkeypatch.setattr(spawn, "start_onboarding", start_onboarding)
    data = journey_db
    private = conversations.create(data["vid"], idempotency_key="firstrun-private")
    with conversations.bind_conversation(data["vid"], private["id"]):
        receipts = [asyncio.run(create_venture(CreateVenture(name=audience if start is not None else None, raw_idea=idea, start=start),
            x_subject=data["actor"])) for start in (False, True, None)]
        assert store.list_messages(data["vid"]) == []
    for receipt, start in zip(receipts, (False, True, None)):
        assert (receipt["onboarding"] is not None) == (start is not False)
        with service.venture_scope(receipt["id"], actor_id=data["actor"], workspace_id=receipt["workspace_id"]):
            main = conversations.list_conversations(receipt["id"])["legacy_conversation_id"]
            messages = store.list_messages(receipt["id"])
            assert [message["kind"] for message in messages] == (["workspace_welcome"] if start else [])
            assert all(message["conversation_id"] == main for message in messages)
            private = conversations.create(receipt["id"], idempotency_key="firstrun-private")
            with conversations.bind_conversation(receipt["id"], private["id"]):
                assert store.list_messages(receipt["id"]) == []
            assert store.list_messages(receipt["id"]) == messages
            assert store.page_messages(receipt["id"]) == (messages, None)


@pytest.mark.parametrize("audience,offer", [
    ("Finance teams at manufacturers", "A tool to reconcile supplier invoices"),
    ("Parents with young children", "An app to plan accessible holidays"),
])
def test_followup_keeps_prefilled_audience(audience, offer):
    from app.entry.chat_start import _onboarding_progress

    intake = {"path": "startup", "extracted": {"idea": "A tool", "audience": audience},
        "prefill_answers": {"customer": audience}}
    progress = _onboarding_progress(intake, "A tool\nClarification: " + offer)
    assert progress["completed"] == ["offer", "customer"]
    assert not progress.get("question")
    intake["website_answers"] = {"offer": offer, "customer": audience}
    assert not _onboarding_progress(intake, "A tool").get("question")


@pytest.mark.parametrize("venture_id", ["ven_supplier_invoices", "ven_family_holidays"])
def test_first_run_reply_is_short_and_offline(monkeypatch, venture_id):
    from app.entry.service import first_run_reply
    from app.chat import link_context

    async def forbidden(*args, **kwargs):
        raise AssertionError("The first-run reply must not fetch pages or call providers")

    monkeypatch.setattr(link_context, "linked_context_async", forbidden)
    reply = first_run_reply(venture_id)
    assert len(reply["body_md"].split()) < 40
    assert "\u2014" not in reply["body_md"]
    assert len(reply["actions"]) == 2
    assert reply["actions"][1]["href"] == f'/v/{venture_id}/guide'


@pytest.mark.parametrize("audience,idea", [
    ("Finance teams at manufacturers", "A tool to reconcile supplier invoices"),
    ("Parents with young children", "An app to plan family holidays"),
])
def test_empty_chat_experiments_explain_one_first_step(monkeypatch, audience, idea):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.experiments import autonomous, engine, ledger
    from app import state

    monkeypatch.setattr(autonomous, "available", lambda venture_id: [])
    monkeypatch.setattr(engine, "rank_assumptions", lambda inputs: {})
    monkeypatch.setattr(state, "MEM", {})
    snapshot = {}
    monkeypatch.setattr(autonomous, "_snapshot", lambda venture_id: snapshot or None)
    monkeypatch.setattr(ledger, "list_for_venture", lambda venture_id: [])
    reply = experiments.list_reply("ven_first")
    assert "No experiments yet." in reply["body_md"]
    assert "small tests" in reply["body_md"] and "you" in reply["body_md"]
    assert "\u2014" not in reply["body_md"]
    assert reply["actions"] == [{"label": "Review my idea", "action": "idea.review", "style": "primary"}]
    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    snapshot.update(idea_problem=idea, journey={"icp": {"who": audience, "problem": idea}},
                    app_id="app_first", waitlist={"enabled": True})
    reply = experiments.list_reply("ven_first")
    assert "No experiments yet." in reply["body_md"]
    if is_local():
        assert "Settings > Keys and connections > Your AI provider" in reply["body_md"]
        assert "Chat model" in reply["body_md"] and "temporarily unavailable" not in reply["body_md"]
    else:
        assert "temporarily unavailable" in reply["body_md"]
    manual = [action for action in reply["actions"] if action.get("confirmation")]
    if is_local():
        assert not manual
        prepared = [action for action in reply["actions"] if action.get("action") == "experiment.run_review"]
        assert len(prepared) == 1 and prepared[0]["payload"]["run"] is True
        assert audience in prepared[0]["payload"]["hypothesis"]
    else:
        assert len(manual) == 1 and manual[0]["action"] == "experiment.run_review"
        assert audience in manual[0]["confirmation"]["resulting_state"]["hypothesis"]
    assert any(action["action"] == "experiment.page.guide" for action in reply["actions"])


@pytest.mark.parametrize("domain,offer,audience,problem", [
    ("invoice-harbor.io", "Match supplier invoices", "Finance teams at manufacturers", "Supplier bills need duplicate entry"),
    ("pantry-bloom.app", "Save recipes and plan dinners", "Home cooks planning weekly dinners", "Planning dinner takes too long"),
])
def test_first_website_message_hands_off_to_customer_action_without_forced_research(journey_db, monkeypatch, domain, offer, audience, problem):
    from app.chat import customers
    from app.db.models import ChatMessage, VentureDocument
    from app.entry import chat_start
    from app.intake import assets, smart
    from app.intake.website import extract_website

    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    origin = f"https://{domain}"
    html = f"<h1>{offer}</h1><p>Audience: {audience}</p><p>Problem: {problem}</p><p>Pricing: $9/month</p><a>Start your free trial</a>"
    website = extract_website(html, origin)
    reads, models, rendered = [], [], []
    monkeypatch.setattr("app.chat.link_context._safe_host", lambda host: True)

    def crawl(url, **kwargs):
        reads.append(url)
        return [{"url": url, "text": website["content"], "venture_name": website["name"], "website_answers": website["answers"]}]

    async def model(text, context, **kwargs):
        models.append(text)
        result = smart.classify_fallback(text, page_context=context).to_dict()
        result["prefill_facts"] = {"pricing_hypothesis": {"value": "$9/month", "quote": "$9/month", "source": "website", "confidence": .95}}
        return result

    def unexpected_idea(*args, **kwargs):
        raise AssertionError("Your stated goal must not force idea research")

    def customer_view(venture_id):
        with journey_db["factory"]() as session:
            saved = session.get(VentureDocument, f"intake_decision:{venture_id}")
            decision = json.loads(saved.body_md)
            assert decision["extracted"]["audience"] == audience
            assert decision["prefill_answers"]["pricing_hypothesis"] == "$12/month"
            assert "pricing_hypothesis" not in decision["website_answers"] and "pricing_hypothesis" not in decision["prefill_facts"]
        return {"icp": {"who": audience, "problem": offer}}

    def render(venture_id, view):
        rendered.append(view)
        return {"body_md": f"Your next step is to find {audience} you can reach. Nothing is sent.",
                "actions": [{"label": "Find customers", "action": "chat.reply", "text": "Find customers", "style": "primary"}], "citations": []}

    monkeypatch.setattr("app.intake.crawl.crawl_site", crawl)
    monkeypatch.setattr(smart, "_call_model", model)
    monkeypatch.setattr("app.idea.service.open_session", unexpected_idea)
    monkeypatch.setattr(assets, "import_site", lambda *args: {"site": {"url": origin, "status": "unverified"}})
    monkeypatch.setattr(customers, "_view", customer_view)
    monkeypatch.setattr(customers, "render", render)
    goal = "Help me get more paying customers." if domain.endswith(".io") else "Help me increase revenue."
    text = f"I run {domain}. {goal}"
    proposal = asyncio.run(chat_start.preview_async(text=text, actor_id=journey_db["actor"], request_id="first-site-message"))["proposal"]
    assert proposal["route"] == "gtm" and not proposal.get("onboarding_question")
    assert proposal["website_answers"]["customer"] == audience
    assert proposal["venture_name"] == website["name"]
    assert asyncio.run(chat_start.preview_async(text=text, actor_id=journey_db["actor"], request_id="first-site-message"))["proposal"] == proposal
    assert reads == [origin] and models == [text]
    receipt = asyncio.run(chat_start.confirm(proposal["id"], journey_db["actor"], name="Your edited name", website_answers={"pricing_hypothesis": "$12/month"}))
    assert receipt["route"] == receipt["next_step"] == "gtm" and len(rendered) == 1
    assert "Your next step" in receipt["message"] and "research your existing product" not in receipt["message"]
    with journey_db["factory"]() as session:
        from app.db.models import Venture
        from app.ventures import current_idea, state_brief
        venture = session.get(Venture, receipt["venture_id"])
        assert venture.name == "Your edited name"
        intake = json.loads(session.get(VentureDocument, f"intake_decision:{venture.id}").body_md)
        assert intake["prefill_answers"]["desired_outcome"] == goal
        assert "Desired Outcome: " + goal in current_idea.current_idea(venture, session=session)
        assert "Customer: " + audience in current_idea.current_idea(venture, session=session)
        assert "Problem: " + problem in current_idea.current_idea(venture, session=session)
        reply = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == receipt["venture_id"], ChatMessage.role == "assistant"))
        assert reply.kind == "gtm_route_confirmed" and json.loads(reply.actions_json)[0]["action"] == "chat.reply"
        assert not any(action.get("action") == "idea.pressure_test.prepare" for action in json.loads(reply.actions_json))
    with chat_start.service.venture_scope(receipt["venture_id"], actor_id=journey_db["actor"], workspace_id=receipt["workspace_id"]):
        brief = state_brief.get_venture_state_brief(receipt["venture_id"], force=True)
    assert brief["idea"]["audience"] == audience and brief["idea"]["desired_outcome"] == goal
    assert brief["idea"]["problem"] == problem
    assert asyncio.run(chat_start.confirm(proposal["id"], journey_db["actor"])) == receipt
