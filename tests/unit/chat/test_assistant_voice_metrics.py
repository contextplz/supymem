"""Voice and model-skip visibility for the venture chat assistant."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app


def test_normalize_assistant_dashes_and_fallback_voice():
    load_company_os_app()
    from app.chat.assistant import (
        _fallback_answer,
        normalize_assistant_dashes,
        reset_assistant_model_skip_counts,
        note_assistant_model_skip,
        assistant_model_skip_counts,
    )

    assert "\u2014" not in normalize_assistant_dashes("Yes \u2014 Gmail is connected.")
    assert "Yes, Gmail is connected." == normalize_assistant_dashes("Yes \u2014 Gmail is connected.")
    assert "1-4 short" == normalize_assistant_dashes("1\u20134 short")
    assert "draft, nothing" == normalize_assistant_dashes("draft\u2014nothing")

    brief = {
        "idea": {"name": "LabHire"},
        "website": {"waitlist_count": 0},
        "connections": {"gmail": {"connected": True, "allowed": ["read drafts"]}},
        "attention": {},
        "builds": {},
        "research": {},
        "device_work": {},
    }
    gmail = _fallback_answer("Is my Gmail connected?", brief)
    wait = _fallback_answer("Show me how the waitlist is doing.", brief)
    assert "\u2013" not in gmail and "\u2014" not in gmail
    assert "Gmail is connected" in gmail
    assert "signup(s) recorded right now" not in wait
    assert "no consented requests" in wait.lower()
    brief["website"] = {"url": "https://labhire.example.test/", "live": True,
                        "waitlist_count": 3, "waitlist_enabled": True}
    count_answer = _fallback_answer("How is the waitlist doing?", brief)
    assert "3 consented early-access requests" in count_answer
    assert "not verified users" in count_answer

    reset_assistant_model_skip_counts()
    note_assistant_model_skip("no_credit_account")
    note_assistant_model_skip("no_hold")
    note_assistant_model_skip("no_credit_account")
    counts = assistant_model_skip_counts()
    assert counts["no_credit_account"] == 2
    assert counts["no_hold"] == 1
