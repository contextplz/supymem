"""Fictional requests exercise plan retries, go-to-market advice and chat summaries."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


def intent(text):
    load_company_os_app()
    from app.chat.intent import classify_intent
    return classify_intent(text)


@pytest.mark.parametrize("text", [
    "I clicked retry for the bakery's order tracker. Please redo the plan.",
    "can you retry planning?",
    "replan this",
    "please redo the plan",
])
def test_retry_or_replan_posts_the_plan_generate_card(text):
    assert intent(text) == "plan_generate"


@pytest.mark.parametrize("text", ["Did planning run again?", "Why does planning keep failing?"])
def test_plan_status_questions_are_not_retries(text):
    assert intent(text) != "plan_generate"


@pytest.mark.parametrize("text", [
    "Can you help me with the GTM for a pottery class finder?",
    "I want to start with the GTM part for Kiln Ledger, specifically LinkedIn. What do you suggest?",
    "Can you help me with go-to-market?",
])
def test_gtm_requests_route_to_the_gtm_flow(text):
    assert intent(text) == "gtm_review"


def test_linkedin_advice_is_not_a_platform_scrape_but_a_link_still_is():
    assert intent("What do you suggest for LinkedIn outreach?") != "agent_reach"
    assert intent("Read https://www.linkedin.com/in/someone") == "agent_reach"


@pytest.mark.parametrize("text", ["please summarize the chat about class bookings", "summarize our conversation"])
def test_chat_summary_goes_to_the_assistant_with_history(text):
    assert intent(text) == "assistant"


def test_chat_summary_prompt_includes_long_history_and_instruction():
    load_company_os_app()
    from app.chat import assistant
    history = [{"role": "user" if i % 2 == 0 else "assistant", "body_md": f"message {i}"} for i in range(30)]
    msgs = assistant.build_messages(question="summarize the chat", brief={"_rendered": "brief"}, history=history,
                                    turns=assistant.CHAT_SUMMARY_TURNS, guidance="Summarize the Recent chat above")
    body = msgs[1]["content"]
    assert "message 0" in body and "message 29" in body and "Summarize the Recent chat above" in body
    short = assistant.build_messages(question="x", brief={"_rendered": "brief"}, history=history)
    assert "message 0" not in short[1]["content"] and "message 29" in short[1]["content"]
