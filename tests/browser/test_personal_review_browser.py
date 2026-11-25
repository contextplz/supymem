"""Rendered personal-project review must not replay an old startup verdict."""

import json
import os

import pytest


@pytest.mark.skipif(not os.getenv("IDEA_JOB_CONSOLE_URL"), reason="local Console URL not supplied")
def test_saved_personal_validation_stays_concise_and_confirms_without_another_model_call():
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["IDEA_JOB_CONSOLE_URL"].rstrip("/")
    session = {
        "id": "ses_personal", "state": "reviewing", "case": "explore",
        "project_intent": "personal", "turns": [],
        "current_revision": {"id": "rev_personal", "summary": "No funding potential in this market.",
                             "structured": {"project_intent": "personal",
                                            "_corrects_revision_id": "rev_prior",
                                            "desired_outcome": "Sort my saved web pages by topic",
                                            "offer": "A private bookmark sorter"}},
        "validation_runs": [{"id": "val_old", "revision_id": "rev_personal", "state": "complete",
                             "method": "gstack_office_hours", "verdict": "needs_evidence",
                             "report": {"summary": "Weak market score; get paying customers.",
                                        "findings": [], "gaps": ["No venture funding identified."]}}],
    }
    fixtures = {
        "/auth/session": {"authenticated": True, "subject": "founder"},
        "/api/ventures/ven_personal": {"id": "ven_personal", "workspace_id": "ws_personal",
                                       "name": "Bookmarks", "raw_idea": "Sort my bookmarks"},
        "/api/ventures/ven_personal/idea/sessions": session,
        "/api/ventures/ven_personal/idea/sessions/ses_personal/confirm": {
            "session": {**session, "state": "confirmed"}},
        "/api/ventures/ven_personal/plans": [],
    }
    script = """window.__requests = [];
      window.__TAURI__ = {
        core: { invoke: async (command, args) => {
          if (command === 'gateway_url') return 'http://127.0.0.1:3200';
          if (command === 'desktop_auth_status') return { state: 'connected', subject: 'founder' };
          if (command === 'desktop_permission_status') return { permissions: {
            screen_recording: true, accessibility: true, event_synthesizing: true } };
          if (command === 'desktop_api_request') {
            window.__requests.push({ method: args.method, path: args.path });
            const data = args.path.startsWith('/api/workspaces/ws_personal/jobs?')
              ? { items: [] } : FIXTURES[args.path] ?? null;
            return { status: 200, data };
          }
          return null;
        } }, event: { listen: async () => () => {} }
      };""".replace("FIXTURES", json.dumps(fixtures))
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(script)
        page.goto(f"{base}/v/ven_personal/idea")
        page.get_by_role("heading", name="Review your personal project").wait_for()
        assert page.get_by_text("Your goal: Sort my saved web pages by topic", exact=True).is_visible()
        assert page.get_by_text("Smallest first version: A private bookmark sorter", exact=True).is_visible()
        assert page.get_by_text("Next test:").is_visible()
        assert page.get_by_role("button", name="Confirm personal project").is_visible()
        assert not page.get_by_role("button", name="Validate updated idea").count()
        assert not page.get_by_text("No funding potential").count()
        assert not page.get_by_text("Weak market score").count()
        page.get_by_role("button", name="Confirm personal project").click()
        page.get_by_role("button", name="Review local build").wait_for()
        requests = page.evaluate("window.__requests")
        assert any(item["path"].endswith("/confirm") and item["method"] == "POST" for item in requests)
        assert not any("/validate" in item["path"] or "/messages" in item["path"] for item in requests)
        browser.close()
