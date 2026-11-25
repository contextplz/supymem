"""The next-user answer must not erase a live site because an older build failed."""

from __future__ import annotations

import asyncio

from tests.company_os_harness import load_company_os_app


def test_next_user_question_reads_fresh_venture_state_without_model_history(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    seen = {}

    def brief(venture_id, **kwargs):
        seen.update(venture_id=venture_id, **kwargs)
        return {
            "idea": {"name": "Example"},
            "website": {"url": "https://example.supymem.com/", "live": True,
                        "waitlist_enabled": True, "waitlist_count": 3},
            "builds": {"last": {"state": "failed", "retry_policy": "manual"}},
            "plan": {"current_step": "Build the authenticated product"},
            "attention": {"needs_you": []},
        }

    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("next-user answer must use current saved state, not old chat")))
    result = asyncio.run(assistant.answer_question(
        "ven_example",
        "What is the single next action toward a real user today? Tell me what needs approval.",
    ))

    assert seen == {"venture_id": "ven_example", "force": True, "mark_visit": True}
    answer = result["body_md"]
    assert "https://example.supymem.com/" in answer
    assert "3 consented requests" in answer
    assert "not verified product users" in answer
    assert "Build the authenticated product" in answer
    assert "cannot verify a functioning product" in answer
    assert "retry" not in answer.lower()
    assert "reviewed channel and message" in answer
    assert result["actions"] == [{
        "label": "Review GTM and waitlist",
        "href": "/gtm?venture=ven_example",
        "style": "primary",
    }]


def test_other_questions_still_use_normal_chat_path():
    load_company_os_app()
    from app.chat.assistant import _asks_next_step_toward_user
    from app.chat.intent import classify_intent

    actual = ("What is the single next action that would move LabHire toward a real user today? "
              "Distinguish the published landing page and waitlist from the unfinished product app, "
              "state the exact live URL and consented request count if known, and tell me what needs approval.")
    assert classify_intent(actual) == "assistant"
    assert not _asks_next_step_toward_user("What is the waitlist count?")
    assert not _asks_next_step_toward_user("Open the browser for the next user signup")
    assert not _asks_next_step_toward_user("What happened in the latest Mac task?")


def test_one_pilot_user_question_does_not_revive_completed_landing_work(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": "LabHire"},
        "website": {"url": "https://labhire.supymem.com/", "live": True,
                    "waitlist_enabled": True, "waitlist_count": 3},
        "plan": {"current_step": "Design and build the static landing artifact"},
        "attention": {"needs_you": ["Last build failed: build_provider_provider_down"]},
    })
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("the old failed build must not own this answer")))
    question = ("Given LabHire has a live landing page and waitlist but no hosted product user yet, "
                "what one founder-approved action can you actually do next to find a real pilot user? "
                "Tell me what is still unimplemented. Do not send or publish anything.")
    result = asyncio.run(assistant.answer_question("ven_example", question))
    body = result["body_md"]
    assert "https://labhire.supymem.com/" in body
    assert "needs reconciliation" in body
    assert "should not ask you to rebuild" in body
    assert "prepare an honest early-access invitation" in body
    assert "No invitation has been sent" in body
    assert "does not execute acquisition strategy" in body
    assert "cannot verify a functioning product application" in body
    assert result["actions"][0]["href"] == "/gtm?venture=ven_example"


def test_next_user_answer_prioritizes_changes_requested_over_stale_plan_step(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": "LabHire"},
        "website": {"url": "https://labhire.supymem.com/", "live": True,
                    "waitlist_enabled": True, "waitlist_count": 3},
        "builds": {"last": {"state": "succeeded", "review_status": "rejected"}},
        "plan": {"current_step": "Build honest early-access static artifact"},
        "attention": {"needs_you": ["Latest build has changes requested: review repair estimate"]},
    })
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("read-only guidance must use saved state without starting work")))

    result = asyncio.run(assistant.answer_question(
        "ven_example",
        "What is the single next action toward a real user today? Tell me what needs approval. Do not start anything.",
    ))

    body = result["body_md"]
    assert "3 consented requests" in body
    assert "not verified product users" in body
    assert "changes requested" in body
    assert "repair estimate" in body
    assert "Build honest early-access static artifact" not in body
    assert "saved plan's next open step" not in body
    assert result["actions"] == [{
        "label": "Review GTM and waitlist",
        "href": "/gtm?venture=ven_example",
        "style": "primary",
    }]


def test_next_user_action_stays_in_selected_venture_without_a_live_waitlist(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": "Early venture"},
        "website": {"live": False, "waitlist_enabled": False},
        "plan": {"present": True, "current_step": "Review a pilot plan"},
        "attention": {"needs_you": []},
    })
    result = asyncio.run(assistant.answer_question(
        "ven_early", "What is the single next action toward a real user?"
    ))
    assert result["actions"] == [{
        "label": "Review pilot plan", "href": "/v/ven_early/plan", "style": "primary",
    }]
    assert "cannot verify a live landing page" in result["body_md"]


def test_pilot_question_with_do_not_send_constraint_is_one_read_only_lookup():
    load_company_os_app()
    from app.chat.intent import split_speech_acts

    question = ("Given LabHire has a live landing page and waitlist but no hosted product user yet, "
                "what one founder-approved action can you actually do next to find a real pilot user? "
                "Tell me what is still unimplemented. Do not send or publish anything.")
    assert split_speech_acts(question) == [("assistant", question)]
    assert split_speech_acts("How many signups? Draft an email to Priya, do not send")[1][0] == "outreach_draft"
