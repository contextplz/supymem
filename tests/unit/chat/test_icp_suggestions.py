"""Unclear audience in chat: Charlie proposes three customer groups, a tap saves one and offers the search card."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db  # noqa: F401 - fixture

GROUPS = [
    {"who": "Adult children living in a different city from aging parents", "problem": "They cannot check on their parents every day",
     "why": "They feel the worry most and already pay for help.", "where": "Facebook groups for expat families, LinkedIn",
     "reach": "Medium, active online"},
    {"who": "Home-care agencies and geriatric care managers", "problem": "Families keep calling them for updates",
     "why": "They coordinate care for many families.", "where": "Local agency directories, care manager associations",
     "reach": "Easy, published business emails"},
    {"who": "Resident welfare associations in senior living communities", "problem": "No shared view of residents' daily needs",
     "why": "One contact reaches many families.", "where": "Senior living community offices", "reach": "Hard, mostly offline"},
]


@pytest.mark.parametrize("audience", ["Industrial maintenance purchasers", "Weekend kayaking groups"])
def test_local_offline_audience_gate_does_not_call_a_model(monkeypatch, audience):
    from unittest.mock import AsyncMock
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import customers
    from app.core import local_mode

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setattr(local_mode, "is_sample_mode", lambda: False)
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    suggest = AsyncMock(side_effect=AssertionError("An offline audience gate must not call a model"))
    monkeypatch.setattr(customers, "_suggest", suggest)
    expected = {"kind": "text", "body_md": f"Your saved audience is {audience}. Choose your next step.", "actions": []}
    monkeypatch.setattr(customers, "render", lambda *args: expected)
    reply = asyncio.run(customers.icp_reply("venture", {"icp": {"who": audience}}, gate=True))
    suggest.assert_not_awaited()
    assert reply == expected


def _stub(monkeypatch, *, fail=False, groups=GROUPS, ok=False):
    monkeypatch.delenv("LLM_OFFLINE", raising=False)  # the model is stubbed, so the offline skip must be off
    monkeypatch.delenv("LLM_CASSETTE_MODE", raising=False)
    from app.core.local_mode import is_local
    from tests.idea_fixtures import install_provider

    if is_local():
        with monkeypatch.context() as provider_patch:
            install_provider(provider_patch)
    from app.credits import metered

    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(kwargs | {"messages": messages})
        if fail:
            raise RuntimeError("provider down")
        content = json.dumps({"groups": groups, "saved_audience_ok": ok})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(metered, "llm", fake_llm)
    return calls


def _no_hidden_pages(reply):
    assert "/gtm" not in reply["body_md"] and "/v/" not in reply["body_md"] and "—" not in reply["body_md"]
    assert not any(a.get("href") for a in reply["actions"])


def _saved_audience(data, audience, problem):
    from app.core.local_mode import is_local

    if not is_local():
        return
    from app.db.models import Venture
    from app.ventures import founder_facts

    with data["factory"]() as session:
        founder_facts.save(session, session.get(Venture, data["vid"]), {
            "audience": founder_facts._fact(audience, "website", .8),
            "problem": founder_facts._fact(problem),
        })
        session.commit()


@pytest.fixture
def unclear(journey_db, monkeypatch):
    from app.gtm import journey

    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {}})
    return journey_db


def test_unclear_audience_explicit_icp_question_suggests_three_groups(unclear, monkeypatch):
    from app.chat import customers

    calls = _stub(monkeypatch)
    reply = asyncio.run(customers.find_reply(unclear["vid"], "Who should I reach out to?"))
    _no_hidden_pages(reply)
    assert len(calls) == 1 and calls[0]["purpose"] == "venture.chat.assistant" and calls[0]["tier"] == "flash"
    chips = [a for a in reply["actions"] if a["action"] == "chat.reply"]
    assert [c["label"] for c in chips[:3]] == [g["who"] for g in GROUPS]
    assert chips[-1]["text"] == customers._NONE_OF_THESE and len(chips) == 4
    for text in ("assumption to test", "Where to find them: Facebook groups", "How reachable: Easy"):
        assert text in reply["body_md"]


def test_picking_a_group_saves_the_audience_and_offers_the_search_card(unclear, monkeypatch):
    from app.chat import customers

    _stub(monkeypatch)
    chips = asyncio.run(customers.find_reply(unclear["vid"], "Who should I reach out to?"))["actions"]
    name, match = customers.command(unclear["vid"], chips[1]["text"])
    assert name == "audience"
    picked = asyncio.run(customers.run_command(unclear["vid"], name, match, chips[1]["text"], unclear["actor"]))
    _no_hidden_pages(picked)
    view = customers._view(unclear["vid"])
    assert view["icp"]["who"] == GROUPS[1]["who"] and view["icp"]["basis"] == "Founder confirmed"
    assert view["state"] not in {"queued", "running"}
    card = next(a for a in picked["actions"] if a["action"] == "gtm.find")
    assert not card.get("confirmation") and 'credit' not in picked['body_md'].lower()
    assert "Saved your audience" in picked["body_md"]


def test_none_of_these_asks_for_the_founders_words_and_saves_them(unclear, monkeypatch):
    from app.chat import customers

    _stub(monkeypatch)
    asyncio.run(customers.find_reply(unclear["vid"], "Who should I reach out to?"))
    name, match = customers.command(unclear["vid"], customers._NONE_OF_THESE)
    asked = asyncio.run(customers.run_command(unclear["vid"], name, match, customers._NONE_OF_THESE, unclear["actor"]))
    _no_hidden_pages(asked)
    assert name == "describe" and "Who are you trying to reach?" in asked["body_md"]
    assert asked["body_md"].startswith("No problem. Who are you trying to reach?")
    assert customers._INTRO not in asked["body_md"]


def test_suggestions_unavailable_still_leaves_a_typed_answer(unclear, monkeypatch):
    from app.chat import customers

    _stub(monkeypatch, fail=True)
    reply = asyncio.run(customers.find_reply(unclear["vid"], "Who should I reach out to?"))
    _no_hidden_pages(reply)
    assert "Who are you trying to reach?" in reply["body_md"]


@pytest.mark.parametrize("text", ["who should I reach out to?", "Who is my ICP?", "who are my customers", "what is my target audience"])
def test_icp_questions_route_to_suggestions_even_with_an_audience(journey_db, monkeypatch, text):
    from app.chat import customers
    from app.chat.intent import classify_intent, split_speech_acts

    assert classify_intent(text) == "gtm_review" and split_speech_acts(text) == [("gtm_review", text)]
    _stub(monkeypatch)
    reply = asyncio.run(customers.find_reply(journey_db["vid"], text))
    _no_hidden_pages(reply)
    assert sum(1 for a in reply["actions"] if a["action"] == "chat.reply") >= 4


def test_stopped_research_and_audience_blocked_tasks_explain_themselves_in_chat(journey_db):
    from app.authz.workspace import subject_person_id
    from app.tasks.chat import stalled_work
    from tests.unit.tasks.test_projection import task

    d, owner = journey_db, subject_person_id(journey_db["actor"])
    task(d["factory"], id="tsk_research", state="canceled", workspace=d["wid"], venture=d["vid"], owner=owner)
    task(d["factory"], id="tsk_prospects", state="input_required", workspace=d["wid"], venture=d["vid"], owner=owner,
         skill="gtm.journey.prepare", blocker={"class": "needs_input", "reason": "Prospect research needs a clearer audience or a contact you know."})
    lines, actions = stalled_work(d["vid"])
    assert "stopped before it finished" in lines[0] and "clearer audience" in lines[1]
    assert [a["text"] for a in actions] == ["Run research again", "Skip research", "Who should I reach out to?"]
    assert not any(a.get("href") for a in actions)


CAKE_GROUPS = [
    {"who": "Home bakers taking cake orders on WhatsApp", "problem": "Cake orders get lost in WhatsApp chats",
     "why": "They run the orders and would use the tool daily.", "where": "Instagram bakery pages, home baker WhatsApp and Facebook groups",
     "reach": "Easy, they publish order numbers"},
    {"who": "Small cake shops that take custom orders on WhatsApp", "problem": "Custom orders and deposits are tracked by hand",
     "why": "More orders, same mess.", "where": "Google Maps listings, local food directories", "reach": "Easy, public shop contacts"},
    {"who": "Cloud kitchens selling desserts through WhatsApp", "problem": "Orders arrive across many chats",
     "why": "High order volume.", "where": "Swiggy and Zomato listings, food business groups", "reach": "Medium, owners are busy"},
]


@pytest.mark.parametrize("raw,problem,groups", [
    ("her customers who order cakes on whatsapp, its messy", "WhatsApp cake orders are messy", CAKE_GROUPS),
    ("their relatives who get picked up after school, its messy", "Coordinating pickups at different schools is hard",
     [{"who": "Parents coordinating school pickups", "problem": "Pickup times overlap",
       "why": "They coordinate pickups every day", "where": "Public parent groups", "reach": "Medium"},
      {"who": "Parents sharing pickup duties with neighbors"}, {"who": "Grandparents handling after-school pickup"}]),
])
def test_raw_audience_of_the_buyers_customers_gets_clean_icp_options_before_any_search_card(journey_db, monkeypatch, raw, problem, groups):
    from app.chat import customers
    from app.gtm import journey

    _saved_audience(journey_db, raw, problem)
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": raw, "problem": problem,
                                                                    "confirmed": True}, "research": {}})
    calls = _stub(monkeypatch, groups=groups)
    buyer = groups[0]["who"]
    text = "who should i talk to first? find me people"
    from app.chat.intent import classify_intent
    assert classify_intent(text) == "gtm_review"
    for query in (text, "find me customers"):
        reply = asyncio.run(customers.find_reply(journey_db["vid"], query))
        _no_hidden_pages(reply)
        assert "recipients for your outreach drafts" not in reply["body_md"]
        assert not any(a.get("confirmation") for a in reply["actions"])  # no search card for the raw audience
        assert "its messy" not in reply["body_md"] and not any("its messy" in a["label"] for a in reply["actions"])
        assert reply["actions"][0]["label"] == buyer
    assert raw in calls[-1]["messages"][1]["content"]  # the model judges the saved words, the founder never sees them echoed
    chip = reply["actions"][0]
    name, match = customers.command(journey_db["vid"], chip["text"])
    picked = asyncio.run(customers.run_command(journey_db["vid"], name, match, chip["text"], journey_db["actor"]))
    assert picked["kind"] == "gtm_search_progress"
    assert not any(action.get("confirmation") for action in picked["actions"])
    assert buyer in picked["body_md"]
    assert "its messy" not in json.dumps(picked)
    assert customers._view(journey_db["vid"])["icp"]["who"] == buyer


@pytest.mark.parametrize("audience,problem", [
    ("Manufacturers ordering replacement bearings", "Unplanned maintenance interrupts production"),
    ("Parents coordinating childcare swaps", "Last-minute schedule changes leave childcare gaps"),
])
def test_clean_saved_buyer_audience_uses_its_source_before_search(journey_db, monkeypatch, audience, problem):
    from app.chat import customers
    from app.core.local_mode import is_local
    from app.gtm import journey

    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {
        "audience": audience, "problem": problem, "confirmed": True}, "research": {}})
    _saved_audience(journey_db, audience, problem)
    calls = _stub(monkeypatch, ok=True)
    reply = asyncio.run(customers.find_reply(journey_db["vid"], "find me customers"))
    if is_local():
        chip, = reply["actions"]
        assert chip["action"] == "chat.reply" and chip["label"] == f"Use {audience}" and not chip.get("confirmation")
        assert calls == [] and not customers._view(journey_db["vid"])["discovery_started"]
        name, match = customers.command(journey_db["vid"], chip["text"])
        picked = asyncio.run(customers.run_command(journey_db["vid"], name, match, chip["text"], journey_db["actor"]))
        assert picked["kind"] == "gtm_search_progress" and not any(action.get("confirmation") for action in picked["actions"])
        return
    assert reply["actions"][0]["action"] == "gtm.find" and not reply["actions"][0].get("confirmation") and len(calls) == 1
    asyncio.run(customers.find_reply(journey_db["vid"], "find me customers"))
    assert len(calls) == 1  # checked once per saved audience


@pytest.mark.parametrize("offer,angle,first_group", [
    ("Regional pickles delivered to homes", "targeted at people living away from India who miss a specific regional taste",
     "Indian expats looking for their home region's pickles"),
    ("Audio lessons for language learners", "targeted at night-shift hospital staff learning a language during commutes",
     "Night-shift hospital staff learning languages on their commute"),
])
def test_suggestions_use_current_idea_fields_and_durable_chosen_angle(journey_db, monkeypatch, offer, angle, first_group):
    from app.chat import customers
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import append_revision
    from app.state import MEM
    from core.clock import utcnow

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = "An older idea for local customers"
        idea = IdeaSession(id="ids_angle", workspace_id=data["wid"], venture_id=data["vid"],
                           case="know", state="clarifying", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        append_revision(session, idea, {"offer": "An outdated domestic offer"}, source="agent")
        revision = append_revision(session, idea, {"offer": offer, "constraints": [angle],
            "workflow": "Order during the commute", "desired_outcome": "One useful delivery",
            "first_user_path": "Reach their existing communities", "pricing_hypothesis": "Per delivery"}, source="agent")
        session.add(ClarifierTurn(id="ict_angle", session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="founder", kind="chip", text=angle,
            options=json.dumps({"provenance": "suggested_choice", "suggested_text": angle, "founder_text": ""}),
            revision_id=revision.id, created_at=utcnow()))
        session.commit()
    MEM.pop(f"questions:{data['vid']}", None)
    MEM.pop(f"answers:{data['vid']}", None)
    groups = [{**GROUPS[0], "who": first_group}, *GROUPS[1:]]
    calls = _stub(monkeypatch, groups=groups)
    reply = asyncio.run(customers.icp_reply(data["vid"], {"icp": {"needs_input": True}}))
    context = calls[0]["messages"][1]["content"]
    assert angle in context and offer in context
    for field in ("workflow", "desired_outcome", "first_user_path", "pricing_hypothesis", "constraints"):
        assert field in context
    assert "newest first; chosen options are assumptions" in context
    assert "The first group must follow the founder's latest chosen angle" in calls[0]["messages"][0]["content"]
    assert "An outdated domestic offer" not in context
    assert reply["actions"][0]["label"] == first_group
    _no_hidden_pages(reply)


@pytest.mark.parametrize("audience", ["everyone who likes pickles, mostly people in india", "people who like language podcasts"])
def test_broad_saved_audience_gets_three_chips_even_if_model_calls_it_clean(journey_db, monkeypatch, audience):
    from app.chat import customers

    calls = _stub(monkeypatch, ok=True)
    view = {"icp": {"who": audience, "needs_input": False}, "can_prepare": True}
    reply = asyncio.run(customers.icp_reply(journey_db["vid"], view, gate=True))
    assert "too broad to reach" in reply["body_md"]
    assert [chip["label"] for chip in reply["actions"][:3]] == [group["who"] for group in GROUPS]
    assert reply["actions"][-1]["text"] == customers._NONE_OF_THESE
    assert audience in calls[0]["messages"][1]["content"]
    assert not any(chip.get("confirmation") for chip in reply["actions"])
