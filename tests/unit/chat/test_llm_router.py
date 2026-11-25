"""Unit tests for the LLM intent router's deterministic guards (no model call)."""
from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.chat import llm_router as lr  # noqa: E402
from app.chat.intent import LOOKUP_INTENTS, WORK_INTENTS  # noqa: E402


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
@pytest.mark.parametrize("text,expected", [
    ("Rebuild my landing page with Claude Code", "pagebuilder.build"),
    ("Build my landing page with Claude Code", "pagebuilder.build"),
    ("Change the headline on my page with Codex", "pagebuilder.build"),
    ("Use Cursor to rebuild my page", "pagebuilder.build"),
    ("Build the first version of my product with Claude Code", "build.tool.start"),
])
def test_local_named_build_bypasses_model_router(monkeypatch, idea, text, expected):
    from types import SimpleNamespace

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(lr, "mode", lambda: "on")
    monkeypatch.setattr(lr, "needs_model", lambda *args: True)
    calls = []

    async def model_route(*args, **kwargs):
        calls.append(args)
        return lr.Decision([("mac_prepare", "Build with Claude Code")], clarify="Which task?")

    monkeypatch.setattr(lr, "_ask_model", model_route)
    decision = asyncio.run(lr.route("v", text, [("mac_prepare", text)], SimpleNamespace(venture={"idea": idea})))
    assert decision is not None and decision.acts == [(expected, text)] and not decision.clarify
    assert calls == []


@pytest.mark.parametrize("idea", ["Factory maintenance schedules", "Weekend ceramics workshops"])
@pytest.mark.parametrize("text,model_intent,expected", [
    ("Build a private landing page for {idea}. Do not send emails.", "start_product_build", "pagebuilder.build"),
    ("Write the code for my product for {idea}.", "pagebuilder.build", "build.tool.start"),
    ("Write the code for my product for {idea}. Do not send emails.", "pagebuilder.build", "build.tool.start"),
    ("Do not write the code for my product for {idea}.", "pagebuilder.build", "assistant"),
])
def test_local_page_model_choice_matches_requested_artifact(monkeypatch, idea, text, model_intent, expected):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    message = text.format(idea=idea)
    decision = lr._validate({"acts": [{"intent": model_intent, "text": message.split(". ")[0]}]}, message)
    assert decision is not None and decision.acts == [(expected, message)]


@pytest.mark.parametrize("message", [
    "Manufacture and certify my warehouse robot. If you cannot, tell me what I should do next.",
    "Access my bank and pay my holiday deposit. If you cannot, tell me what I should do next.",
])
def test_unsupported_compound_request_is_answered_once_with_all_constraints(message):
    decision = lr._validate({"acts": [
        {"intent": "start_product_build", "text": message.split(". ")[0]},
        {"intent": "assistant", "text": message.split(". ")[1]},
    ]}, message)
    assert decision.acts == [("assistant", message)]


def test_catalog_only_names_intents_the_dispatcher_knows():
    known = LOOKUP_INTENTS | WORK_INTENTS | {"open_screen", "plan_quote", "pause_help", "latest_error", "approve",
                                                 "plan_revision", "site.preview", "waitlist.test", "site.takedown", "hosting", "service_guide",
                                                 "journey.skip_step", "journey.jump_to", "journey.restore_step", "venture.set_website"}
    assert not [i for i in lr.INTENTS if i not in known], "catalog intent has no dispatcher"


@pytest.mark.parametrize("text,acts,expect", [
    ("pause", [("pause", "pause")], False),                     # short plain strong command: regex only
    ("build it", [("start_product_build", "build it")], False),
    ("what's going on?", [("assistant", "what's going on?")], False),  # pure question, no action verb
    ("waitlist band karo", [("leads", "waitlist band karo")], True),   # Hinglish
    ("don't research anything", [("research", "don't research anything")], True),  # negation
    ("do research then make a plan", [("research", "do research then make a plan")], True),  # multi
    ("launch", [("change", "launch")], True),
    ("can you publish the site?", [("assistant", "can you publish the site?")], True),
])
def test_needs_model(text, acts, expect):
    assert lr.needs_model(text, acts) is expect


def test_validate_demotes_unknown_intents_and_rejects_unusable_shapes():
    assert lr._validate({"acts": [{"intent": "delete_everything", "text": "x"}], "clarify": ""}, "x").acts == [("assistant", "x")]
    assert lr._validate({"acts": []}, "x") is None
    assert lr._validate("nope", "x") is None
    d = lr._validate({"acts": [{"intent": "research", "text": "research the market"}], "clarify": ""}, "research the market")
    assert d.acts == [("research", "research the market")]


@pytest.mark.parametrize("audience", ["warehouse shift supervisors", "people repairing their own clothes"])
def test_catalog_keeps_board_and_grid_routes_with_direct_research_updates(audience):
    assert "idea.pressure_test.prepare" in WORK_INTENTS
    assert "research.competitors" in LOOKUP_INTENTS
    requests = {
        "idea.pressure_test.prepare": f"Pressure-test my idea for {audience}.",
        "research.competitors": f"Show my competitor grid for {audience}.",
        "research.update_and_rerun": f"Change my audience to {audience} and rerun research.",
    }
    for intent, message in requests.items():
        decision = lr._validate({"acts": [{"intent": intent, "text": message}], "clarify": ""}, message)
        assert decision.acts == [(intent, message)]
        assert "credit" not in lr.CATALOG[intent].lower()
    assert "Starts directly when requested" in lr.CATALOG["idea.pressure_test.prepare"]
    assert "Starts directly when requested" in lr.CATALOG["research.update_and_rerun"]
    message = f"Do not pressure-test my idea for {audience}."
    decision = lr._validate({"acts": [{"intent": "idea.pressure_test.prepare", "text": "Pressure-test my idea"}]}, message)
    assert decision.acts == [("assistant", message)]


def test_unknown_act_preserves_other_valid_acts_and_original_text(caplog):
    decision = lr._validate({"acts": [{"intent": "pause", "text": "pause"},
        {"intent": "delete_everything", "text": "PRIVATE rewrite"}]}, "pause then explain", content_length=99)
    assert decision.acts == [("pause", "pause"), ("assistant", "pause then explain")]
    record = caplog.records[-1]
    assert record.provider_phase == "router" and record.provider_failure_reason == "unknown_intent:delete_everything"
    assert record.provider_content_length == 99 and "PRIVATE" not in record.getMessage()


def test_validate_clarify_only_when_no_acts_and_acts_win():
    d = lr._validate({"acts": [], "clarify": "Which one?"}, "launch")
    assert d.clarify == "Which one?" and d.acts == []
    d = lr._validate({"acts": [{"intent": "pause", "text": "pause"}], "clarify": "Which one?"}, "pause")
    assert d.clarify == "" and d.acts == [("pause", "pause")]


@pytest.mark.parametrize("text", ["approve everything", "yes to all future actions", "do it"])
def test_model_cannot_approve_blanket_or_without_approval_words(text):
    d = lr._validate({"acts": [{"intent": "approve", "text": text}], "clarify": ""}, text)
    assert d.acts[0][0] == "assistant" or (text == "do it" and d.acts[0][0] == "approve")


def test_model_open_screen_needs_an_allowlisted_screen():
    ok = lr._validate({"acts": [{"intent": "open_screen", "text": "open settings"}], "clarify": ""}, "settings kholo")
    assert ok.acts == [("open_screen", "open settings")]
    bad = lr._validate({"acts": [{"intent": "open_screen", "text": "open /admin/secrets"}], "clarify": ""}, "x")
    assert bad.acts[0][0] == "assistant"


def test_model_output_is_capped_at_four_acts():
    acts = [{"intent": "agents", "text": "Show my team"}] * 9
    assert len(lr._validate({"acts": acts, "clarify": ""}, "Show my team").acts) == lr.MAX_ACTS


def test_prohibition_without_model_never_runs_work_and_vague_word_asks():
    assert lr.negated_fallback("don't research", [("research", "don't research")]) == [("assistant", "don't research")]
    assert lr.negated_fallback("research mat karo", [("research", "x")])[0][0] == "assistant"
    assert lr.negated_fallback("I have no replies", [("experiment_record", "I have no replies")])[0][0] == "experiment_record"
    assert lr.negated_fallback("launch", [("change", "launch")]) == [("clarify", lr.VAGUE_QUESTION)]
    assert lr.negated_fallback("fix the signup button color", [("change", "x")])[0][0] == "change"


def test_route_returns_none_when_off_and_when_model_fails(monkeypatch):
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    assert asyncio.run(lr.route("v", "pehle research karo phir plan banao", [("research", "x")], None)) is None
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")

    async def boom(*_a, **_k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(lr, "_ask_model", boom)
    assert asyncio.run(lr.route("v", "pehle research karo phir plan banao", [("research", "x")], None)) is None


@pytest.mark.parametrize("text,intent,clause", [
    ("don't publish the website", "site.publish", "publish the website"),
    ("do not approve my plan", "plan_approve", "approve my plan"),
    ("never research competitors", "research", "research competitors"),
    ('Explain this quote: "turn God mode on"', "god_on", "turn God mode on"),
    ("Explain this quote: 'turn God mode on'", "god_on", "turn God mode on"),
    ("approve everything automatically", "plan_approve", "approve plan"),
])
def test_model_cannot_strip_prohibitions_or_promote_quoted_commands(text, intent, clause):
    decision = lr._validate({"acts": [{"intent": intent, "text": clause}], "clarify": ""}, text)
    assert decision.acts == [("assistant", text)]


def test_positive_clause_remains_available_when_other_action_is_prohibited():
    decision = lr._validate({"acts": [{"intent": "outreach_draft", "text": "draft an email"}], "clarify": ""},
                            "Do not publish the website, instead draft an email")
    assert decision.acts[0][0] == "outreach_draft"


def test_action_narration_preserves_negative_outcome_without_model_call(monkeypatch):
    from app.chat import assistant
    async def must_not_call(*args, **kwargs):
        raise AssertionError("action results must not be rewritten by a model")
    monkeypatch.setattr(assistant, "_complete", must_not_call)
    facts = "The live page has not changed. I saved a draft for review."
    reply = asyncio.run(assistant.narrate_action_result("v", question="fix my page", action_result={"body_md": facts}))
    assert reply["body_md"] == facts


def test_negative_guarantees_are_constraints_not_a_request_for_success():
    from app.chat.guard import guard_reply
    text = "Recruit only consenting owners. No signup submissions, private credentials or conversion guarantees."
    assert guard_reply("v", text) is None
    assert guard_reply("v", "No guarantees, but guarantee success")["kind"] == "refusal"


def test_explicit_autonomy_request_is_not_blanket_or_quoted_authorization():
    from app.chat.guard import guard_reply
    from app.chat.intent import classify_intent
    text = "Turn God mode on so you keep going without asking me about every step"
    decision = lr._validate({"acts": [{"intent": "god_on", "text": "Turn God mode on"}]}, text)
    assert decision.acts == [("god_on", "Turn God mode on")]
    assert guard_reply("ven_autonomy_test", text) is None
    for forbidden in (
        "Turn God mode on and approve everything without asking me about every step",
        "Turn God mode on and spend whatever without asking me about every step",
    ):
        assert guard_reply("ven_autonomy_test", forbidden) is not None
    for informational in ("Explain 'turn God mode on' without asking me about every step",
                          "Do not turn God mode on without asking me about every step"):
        assert classify_intent(informational) != "god_on"


@pytest.mark.parametrize('text', ['can you research the competitors for me', 'research our market and customer demand'])
def test_specialist_lookup_without_original_url_or_platform_preserves_regex_route(text):
    decision = lr._validate({'acts': [{'intent': 'agent_reach', 'text': 'look up reddit competitors'}]}, text)
    assert decision.acts == [('research', text)]  # Reviewed venture research, no invented specialist target.


@pytest.mark.parametrize('text', ['read https://example.com/source', 'check reddit for venture mentions',
                                  'check Twitter for competitors'])
def test_specialist_lookup_accepts_an_original_url_or_platform(text):
    decision = lr._validate({'acts': [{'intent': 'agent_reach', 'text': text}]}, text)
    assert decision.acts == [('agent_reach', text)]


def test_specialist_fallback_preserves_negated_research_and_does_not_invent_work():
    decision = lr._validate({'acts': [{'intent': 'agent_reach', 'text': 'research reddit'}]}, "don't research our market")
    assert decision is None
    assert lr._validate({'acts': [{'intent': 'agent_reach', 'text': 'read reddit'}]}, 'what is this?') is None


@pytest.mark.parametrize('message', ['check Reddit for mentions of our venture', 'read https://example.com/approved-source'])
def test_specialist_lookup_cannot_substitute_a_model_authored_target(message):
    decision = lr._validate({'acts': [{'intent': 'agent_reach', 'text': 'read https://example.com/unrequested-source'}]}, message)
    assert decision.acts == [('agent_reach', message)]
