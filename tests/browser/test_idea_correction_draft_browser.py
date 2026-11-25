"""Rendered Idea correction regression against a local Console with a mocked native bridge.

Set IDEA_CORRECTION_CONSOLE_URL to a locally running Console URL to opt in.
This test never calls a real gateway or changes a venture.
"""

import json
import os

import pytest


@pytest.mark.skipif(not os.getenv("IDEA_CORRECTION_CONSOLE_URL"), reason="local Console URL not supplied")
def test_unsent_answer_correction_survives_navigation_and_cancel_clears_it():
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["IDEA_CORRECTION_CONSOLE_URL"].rstrip("/")
    session = {
        "id": "ses_test", "state": "confirmed", "case": "know", "project_intent": "personal",
        "turns": [
            {"id": "q_test", "role": "agent", "kind": "question", "text": "Who is this for?",
             "options": {"field": "customer", "options": ["Just me", "My team"],
                         "allow_free_text": True, "selection_mode": "single"}},
            {"id": "a_test", "role": "founder", "kind": "answer", "text": "Just me"},
        ],
        "current_revision": {"id": "rev_test", "summary": "Personal bookmark helper",
                             "structured": {"customer": "Just me"}},
        "validation_runs": [],
    }
    fixtures = {
        "/auth/session": {"authenticated": True, "subject": "founder"},
        "/api/ventures/ven_test": {"id": "ven_test", "workspace_id": "ws_test",
                                   "name": "Personal Bookmark QA Idea", "raw_idea": "A bookmark helper"},
        "/api/ventures/ven_test/idea/sessions": session,
        "/api/ventures/ven_test/plans": [],
    }
    bridge = """window.__TAURI__ = {
      core: { invoke: async (command, args) => {
        if (command === 'gateway_url') return 'http://127.0.0.1:3200';
        if (command === 'desktop_auth_status') return { state: 'connected', subject: 'founder' };
        if (command === 'desktop_api_request') {
          const data = FIXTURES[args.path] ?? [];
          return { status: 200, data };
        }
        return null;
      } },
      event: { listen: async () => () => {} }
    };""".replace("FIXTURES", json.dumps(fixtures))

    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(bridge)
        idea_url = f"{base}/v/ven_test/idea"

        def open_editor():
            later = page.get_by_role("button", name="Do this later")
            if later.count():
                later.click()
            page.get_by_text("Review and revise previous answers").click()
            page.get_by_role("button", name="Change this answer").click()
            return page.get_by_role("textbox", name="Additional context or your own answer")

        page.goto(idea_url)
        editor = open_editor()
        page.get_by_role("radio", name="My team").check()
        editor.fill("Keep my private bookmark notes")
        assert editor.input_value() == "Keep my private bookmark notes"

        page.goto(f"{base}/v/ven_test")  # Cockpit unmounts Idea.
        page.goto(idea_url)
        editor = open_editor()
        assert editor.input_value() == "Keep my private bookmark notes"
        assert page.get_by_role("radio", name="My team").is_checked()

        page.get_by_role("button", name="Cancel").click()
        page.reload()
        editor = open_editor()
        assert editor.input_value() == ""
        assert page.get_by_role("radio", name="Just me").is_checked()
        browser.close()
