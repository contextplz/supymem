"""Rendered saved-plan guide against a local Console and mocked native bridge.

Set EXECUTION_GUIDE_CONSOLE_URL to opt in. No hosted API or paid job is called.
"""

import json
import os

import pytest


@pytest.mark.skipif(not os.getenv("EXECUTION_GUIDE_CONSOLE_URL"), reason="local Console URL not supplied")
def test_guarded_saved_plan_guides_directly_to_edit_without_starting_work():
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["EXECUTION_GUIDE_CONSOLE_URL"].rstrip("/")
    proposal = {
        "summary": "Test the product with a private pilot.",
        "goal_first_paid_user": "Learn from a private pilot first.",
        "target_days": None,
        "assumptions": [], "exclusions": [], "evidence_gaps": [],
        "milestones": [],
    }
    plan = {
        "id": "plan_saved", "state": "draft", "version": 3, "edit_version": 1,
        "review_token": "review_test", "generation_state": "completed",
        "stale_dependencies": False, "reviewable": True,
        "proposal": proposal, "milestones": [], "limitations": [],
        "review_findings": [{"code": "unsupported_claim", "path": "summary",
                             "message": "Remove the unsupported claim."}],
    }
    fixtures = {
        "/auth/session": {"authenticated": True, "subject": "founder"},
        "/api/ui-config": {"founder_ui": "classic"},  # /v/<id>/plan is chat-first hidden in "home"
        "/api/ventures/ven_test": {"id": "ven_test", "workspace_id": "ws_test", "name": "Existing venture"},
        "/api/ventures/ven_test/plans": {"items": [{"id": "plan_saved", "version": 3, "state": "draft"}]},
        "/api/ventures/ven_test/plans/plan_saved": plan,
        "/api/ventures/ven_test/resources": {"inventory": {"complete": True, "assets": []}},
        "/api/workspaces/ws_test/credits/summary": {"status": "available", "available": 100,
                                                      "reserved": 0, "spent": 0},
    }
    bridge = """window.__calls = [];
    window.__TAURI__ = {
      core: { invoke: async (command, args) => {
        if (command === 'gateway_url') return 'http://127.0.0.1:3200';
        if (command === 'desktop_auth_status') return { state: 'connected', subject: 'founder' };
        if (command === 'desktop_api_request') {
          window.__calls.push({ path: args.path, method: args.method || 'GET' });
          return { status: 200, data: FIXTURES[args.path] ?? { items: [] } };
        }
        return null;
      } },
      event: { listen: async () => () => {} }
    };""".replace("FIXTURES", json.dumps(fixtures))

    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(bridge)
        page.goto(f"{base}/v/ven_test/plan")
        later = page.get_by_role("button", name="Do this later")
        if later.count():
            later.click()
        guide = page.get_by_role("region", name="Next execution step")
        guide.get_by_role("button", name="Edit plan manually").click()
        assert page.get_by_role("heading", name="Edit the plan").is_visible()
        assert guide.get_by_role("link", name="Continue editing").is_visible()
        calls = page.evaluate("window.__calls")
        assert any(call["path"] == "/api/ventures/ven_test/plans/plan_saved" for call in calls)
        assert not any("estimate-generation" in call["path"] or "/compile" in call["path"] for call in calls)
        browser.close()
