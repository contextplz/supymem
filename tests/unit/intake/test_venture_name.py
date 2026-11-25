"""Opt-in brand suggestions keep the original idea out of prompts/infrastructure logs."""
from __future__ import annotations

import asyncio
import importlib

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def names(tmp_path_factory):
    with pytest.MonkeyPatch.context() as setup:
        setup.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path_factory.mktemp('names')}/names.db")
        setup.setenv("PROVISIONING_MODE", "local")
        setup.setenv("LLM_OFFLINE", "1")
        setup.setenv("GUARDRAILS_MODE", "deterministic_only")
        load_company_os_app()
        yield importlib.import_module("app.intake.venture_name")


async def test_opt_in_uses_extracted_idea_and_audience_once(names, monkeypatch):
    calls = []
    async def model(messages, **kwargs):
        calls.append(kwargs)
        assert "studio managers" in messages[1]["content"]
        assert "booking calendars" in messages[1]["content"]
        assert "booking calendars" not in messages[0]["content"]
        return {"name": "Slotwise"}
    monkeypatch.setattr(names, "_metered_json", model)
    assert await names.suggest_name("booking calendars", "studio managers", actor_id="person_synthetic") == "Slotwise"
    assert len(calls) == 1
    assert calls[0]["purpose"] == "intake.suggest_name"
    assert calls[0]["max_tokens"] == 400


async def test_plain_audience_name_is_rejected_and_fallback_uses_offer(names, monkeypatch):
    async def model(*args, **kwargs):
        return {"name": "Mobile Parents"}
    monkeypatch.setattr(names, "_metered_json", model)
    idea = "Mobile parents need a way to organize meal plans for busy families."
    audience = "Mobile parents"
    assert await names.suggest_name(idea, audience, actor_id="p") is None
    result = await names.suggest_name_or_reason(idea, audience, actor_id="p")
    assert result == {"name": "MealNest", "source": "idea_words"}
    assert len(result["name"]) <= 30
    assert names._idea_words_name("Mobile parents struggle with weekend childcare") == "Weekend Childcare"


@pytest.mark.parametrize("result", [None, "bad", {}, {"name": "I Am"}, {"name": "ven_abc123"}, {"name": "fuck"}, {"name": "A full sentence with many words"}, {"name": "Slotwise", "claim": "available"}])
async def test_invalid_name_never_gets_automatic_fallback(names, monkeypatch, result):
    async def model(*args, **kwargs):
        return result
    monkeypatch.setattr(names, "_metered_json", model)
    assert await names.suggest_name("booking calendars", actor_id="person_synthetic") is None


async def test_timeout_does_not_retry_or_log_founder_text(names, monkeypatch, caplog):
    calls = []
    async def model(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(1)
    monkeypatch.setattr(names, "_metered_json", model)
    assert await names.suggest_name("synthetic-private-name-marker", actor_id="person_synthetic", budget_seconds=0.02) is None
    assert calls == [1]
    assert "synthetic-private-name-marker" not in caplog.text


async def test_eligible_person_uses_metered_model_suggestion(names, monkeypatch):
    import app.intake.smart as smart
    async def model(*args, **kwargs):
        return {"name": "Slotwise"}
    monkeypatch.setattr(smart, "_consumer_account", lambda actor_id: object())
    monkeypatch.setattr(names, "_metered_json", model)
    assert await names.suggest_name_or_reason("booking calendars for studios", actor_id="p") == {"name": "Slotwise", "source": "model"}


@pytest.mark.parametrize("eligible,model_result", [(False, None), (True, None), (True, {"name": "I Am"})])
async def test_ineligible_offline_or_failed_model_falls_back_to_idea_key_words(names, monkeypatch, eligible, model_result):
    import app.intake.smart as smart
    calls = []
    async def model(*args, **kwargs):
        calls.append(1)
        return model_result
    monkeypatch.setattr(smart, "_consumer_account", lambda actor_id: object() if eligible else None)
    monkeypatch.setattr(names, "_metered_json", model)
    out = await names.suggest_name_or_reason(
        "Supplier catalog normalization and variant safe change packs for Shopify agencies.", actor_id="p")
    assert out == {"name": "CatalogNest", "source": "idea_words"}
    assert calls == [1]  # every account tries the metered path; its gate refuses unpayable calls


@pytest.mark.parametrize("idea", ["hello", "hey there!", "   "])
async def test_greeting_or_empty_idea_returns_precise_reason_not_a_greeting_name(names, monkeypatch, idea):
    import app.intake.smart as smart
    monkeypatch.setattr(smart, "_consumer_account", lambda actor_id: None)
    out = await names.suggest_name_or_reason(idea, actor_id="p")
    assert "name" not in out and out["reason"] in {"name_unavailable", "idea_missing"} and out["message"]


async def test_repeated_model_name_is_rejected_and_fallbacks_stay_related(names, monkeypatch):
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        return {"name": "Booking Calendars"}
    monkeypatch.setattr(names, "_metered_json", model)
    previous = []
    for _ in range(6):
        result = await names.suggest_name_or_reason("booking calendars for studios", actor_id="p", avoid=previous)
        assert result["name"].casefold() not in {n.casefold() for n in previous}
        assert "booking" in result["name"].lower() or "calendars" in result["name"].lower()
        previous.append(result["name"])
    assert len(calls) == 6
    assert "Booking Calendars" in calls[1][1]["content"]


@pytest.mark.parametrize("idea,audience,tokens", [
    ("app for gyms", "help gyms get more members", ("gym", "member", "fit")),
    ("a tool for tutors", "help tutors book more lessons", ("tutor", "lesson", "book")),
    ("an app for restaurants", "help restaurants reduce food waste", ("restaurant", "food", "waste")),
    ("software for farmers", "help farms sell crops", ("farm", "crop")),
    ("a platform for dog walkers", "help them schedule walks", ("dog", "walk", "schedule")),
    ("app for gyms\nAudience: help gyms get more members", "gyms", ("gym", "member", "fit")),
])
@pytest.mark.parametrize("model_name", [None, "AppNest", "IdeaFlow", "VentureHub", "Shopify"])
async def test_vague_ideas_never_get_generic_or_unrelated_names(names, monkeypatch, idea, audience, tokens, model_name):
    async def model(*args, **kwargs):
        return {"name": model_name} if model_name else None
    monkeypatch.setattr(names, "_metered_json", model)
    previous = []
    for _ in range(3):
        result = await names.suggest_name_or_reason(idea, audience, actor_id="p", avoid=previous)
        name = result["name"].casefold()
        assert any(token in name for token in tokens)
        assert name not in {"appnest", "ideaflow", "venturehub", "productpilot", "startupspark"}
        assert name not in {used.casefold() for used in previous}
        assert result["source"] == "idea_words"
        previous.append(result["name"])
    if "gyms" in idea:
        assert previous[0] == "GymGrow"


@pytest.mark.parametrize("name,accepted", [("GymGrow", True), ("MemberMagnet", True), ("FitFunnel", True),
                                           ("GymSpotify", False), ("GymNike", False), ("AppNest", False)])
async def test_model_names_require_domain_grounding_without_known_brands(names, monkeypatch, name, accepted):
    async def model(*args, **kwargs):
        assert kwargs["validate_output"]({"name": name}) == ({"name": name} if accepted else None)
        return {"name": name}
    monkeypatch.setattr(names, "_metered_json", model)
    assert await names.suggest_name("app for gyms", "help gyms get more members", actor_id="p") == (name if accepted else None)


async def test_slow_model_returns_grounded_fallback_without_replaying(names, monkeypatch):
    calls = []
    wait_for = asyncio.wait_for
    async def quick_timeout(awaitable, *, timeout):
        assert timeout == 6.0
        return await wait_for(awaitable, timeout=0.01)
    async def model(*args, **kwargs):
        calls.append(kwargs["request_id"])
        await asyncio.sleep(10)
    monkeypatch.setattr(names.asyncio, "wait_for", quick_timeout)
    monkeypatch.setattr(names, "_metered_json", model)
    monkeypatch.setattr(names, "recover_original_json", lambda **kwargs: None)
    assert await names.suggest_name_or_reason("app for gyms", "help gyms get more members", actor_id="p",
                                            request_id="original") == {"name": "GymGrow", "source": "idea_words"}
    assert calls == ["original"]


async def test_uncertain_paid_outcome_is_not_replaced_or_replayed(names, monkeypatch):
    from llm_gateway.errors import ProviderCallError
    calls = []
    async def model(*args, **kwargs):
        calls.append(kwargs["request_id"])
        raise ProviderCallError("provider_outcome_unknown", "Synthetic uncertain settlement")
    monkeypatch.setattr(names, "_metered_json", model)
    with pytest.raises(ProviderCallError):
        await names.suggest_name_or_reason("app for gyms", "help gyms get more members", actor_id="p", request_id="original")
    assert calls == ["original"]


async def test_placeholder_rejected_even_when_part_of_it_matches_the_domain(names, monkeypatch):
    async def model(*args, **kwargs):
        return {"name": "AppNest"}
    monkeypatch.setattr(names, "_metered_json", model)
    assert await names.suggest_name("monitor bird nests", actor_id="p") is None


@pytest.mark.parametrize("idea", ["app", "a tool", "software for people"])
async def test_only_generic_words_offer_manual_naming_instead_of_placeholders(names, monkeypatch, idea):
    async def model(*args, **kwargs):
        return None
    monkeypatch.setattr(names, "_metered_json", model)
    result = await names.suggest_name_or_reason(idea, actor_id="p")
    assert result["reason"] == "name_unavailable" and "Type a name" in result["message"]


async def test_names_vary_when_provider_is_offline(names, monkeypatch):
    async def model(*args, **kwargs):
        return None
    monkeypatch.setattr(names, "_metered_json", model)
    previous = []
    for _ in range(30):
        result = await names.suggest_name_or_reason("Tutor scheduling calendar", actor_id="p", avoid=previous)
        assert "name" in result and result["name"].casefold() not in {n.casefold() for n in previous}
        assert any(word in result["name"].lower() for word in ("tutor", "calendar", "scheduling"))
        previous.append(result["name"])


def test_idea_word_names_skip_founder_hedges_across_domains():
    from app.intake.venture_name import _idea_words_name

    for idea, root in (("idk something for mobile pet groomers, they waste so much time texting clients back and forth", "Mobile"),
                       ("honestly maybe stuff for bakery owners who lose orders in their inbox", "Bakery")):
        name = _idea_words_name(idea)
        assert name and name.split()[0] == root
        assert not any(word in name.casefold().split() for word in ("idk", "honestly", "maybe", "something", "stuff"))


async def test_founder_website_names_the_product_across_domains(names, monkeypatch):
    async def model(*args, **kwargs):
        raise AssertionError("a stated website needs no model call")
    monkeypatch.setattr(names, "_metered_json", model)
    for idea, expected in (("A voice sleep log for new parents. Website is lullalog.app", "Lullalog"),
                           ("Invoice reminders for plumbers, see https://www.pipe-pay.io", "PipePay")):
        assert await names.suggest_name_or_reason(idea, actor_id="p", request_id="r") == {"name": expected, "source": "website"}
    assert (await names.suggest_name_or_reason("Invoice reminders, see pipe-pay.io", actor_id="p", avoid=["PipePay"], request_id="r2")).get("name") != "PipePay"
