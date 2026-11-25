"""Rendered recovery for malformed successful job responses from a local Console.

Set IDEA_JOB_CONSOLE_URL to a locally running Console. The native bridge and
gateway responses are mocked; this test never changes a real venture or job.
"""

import json
import os

import pytest


@pytest.mark.skipif(not os.getenv("IDEA_JOB_CONSOLE_URL"), reason="local Console URL not supplied")
@pytest.mark.parametrize("malformed_endpoint", ["list", "read"])
def test_malformed_saved_job_status_is_visible_and_does_not_resubmit(malformed_endpoint):
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["IDEA_JOB_CONSOLE_URL"].rstrip("/")
    session = {
        "id": "ses_test", "state": "validating", "case": "know", "project_intent": "startup",
        "initial_job_id": "job_test" if malformed_endpoint == "read" else None,
        "turns": [], "current_revision": {"id": "rev_test", "summary": "A saved idea",
                                         "structured": {"problem": "Test"}},
        "validation_runs": [],
    }
    fixtures = {
        "/auth/session": {"authenticated": True, "subject": "founder"},
        "/api/ventures/ven_test": {"id": "ven_test", "workspace_id": "ws_test",
                                   "name": "Job status QA", "raw_idea": "An idea"},
        "/api/ventures/ven_test/idea/sessions": session,
        "/api/ventures/ven_test/plans": [],
        "/api/jobs/job_test": None,
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
            const data = args.path.startsWith('/api/workspaces/ws_test/jobs?')
              ? { items: [null] } : FIXTURES[args.path] ?? null;
            return { status: 200, data };
          }
          return null;
        } }, event: { listen: async () => () => {} }
      };""".replace("FIXTURES", json.dumps(fixtures))

    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(script)
        page.goto(f"{base}/v/ven_test/idea")
        if malformed_endpoint == "read":
            page.get_by_role("heading", name="Background status unavailable").wait_for()
            assert page.get_by_role("button", name="Check saved status").is_visible()
        else:
            page.get_by_text("Background work could not be synchronized.", exact=False).wait_for()
        requests = page.evaluate("window.__requests")
        assert any(item["path"] == "/api/ventures/ven_test/idea/sessions" for item in requests)
        assert not any("/validate" in item["path"] or "/messages" in item["path"] for item in requests)
        browser.close()


@pytest.mark.skipif(not os.getenv("IDEA_JOB_CONSOLE_URL"), reason="local Console URL not supplied")
def test_check_saved_status_keeps_current_idea_visible_during_slow_reconciliation():
    """A manual status check must not replace saved work with a blank/loading route."""
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["IDEA_JOB_CONSOLE_URL"].rstrip("/")
    script = """window.__requests = [];
      window.__delayNextSession = false;
      window.__TAURI__ = {
        core: { invoke: async (command, args) => {
          if (command === 'gateway_url') return 'http://127.0.0.1:3200';
          if (command === 'desktop_auth_status') return { state: 'connected', subject: 'founder' };
          if (command === 'desktop_permission_status') return { permissions: {
            screen_recording: true, accessibility: true, event_synthesizing: true } };
          if (command === 'desktop_api_request') {
            window.__requests.push({ method: args.method, path: args.path });
            if (args.path === '/api/ventures/ven_test/idea/sessions' && window.__delayNextSession) {
              window.__delayNextSession = false;
              await new Promise((resolve) => setTimeout(resolve, 1200));
            }
            const data = args.path.startsWith('/api/workspaces/ws_test/jobs?')
              ? { items: [] } : FIXTURES[args.path] ?? null;
            return { status: 200, data };
          }
          return null;
        } }, event: { listen: async () => () => {} }
      };""".replace("FIXTURES", json.dumps({
        "/auth/session": {"authenticated": True, "subject": "founder"},
        "/api/ventures/ven_test": {"id": "ven_test", "workspace_id": "ws_test",
                                      "name": "Status QA", "raw_idea": "A saved idea"},
        "/api/ventures/ven_test/idea/sessions": {
            "id": "ses_test", "state": "validating", "case": "know",
            "project_intent": "startup", "turns": [], "current_revision": {"id": "rev_test"},
            "validation_runs": [],
        },
        "/api/ventures/ven_test/plans": [],
    }))

    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(script)
        page.goto(f"{base}/v/ven_test/idea")
        page.get_by_role("heading", name="Your saved journey is waiting for its next step").wait_for()
        page.evaluate("window.__delayNextSession = true")
        page.get_by_role("button", name="Check saved validation status").click()
        page.get_by_role("button", name="Checking saved status…").wait_for()
        assert page.get_by_role("heading", name="Your saved journey is waiting for its next step").is_visible()
        assert page.get_by_text("A saved idea", exact=True).is_visible()
        assert not page.get_by_role("heading", name="Preparing your journey").count()
        page.get_by_role("button", name="Check saved validation status").wait_for()
        assert not any("/messages" in item["path"] or "/validate" in item["path"]
                       for item in page.evaluate("window.__requests"))
        browser.close()


@pytest.mark.skipif(not os.getenv("IDEA_JOB_CONSOLE_URL"), reason="local Console URL not supplied")
def test_unresolved_validation_status_offers_read_only_recovery():
    """A stalled native job read must not leave a founder watching an endless spinner."""
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["IDEA_JOB_CONSOLE_URL"].rstrip("/")
    script = """window.__requests = [];
      window.__clockOffset = 0;
      const realNow = Date.now.bind(Date);
      Date.now = () => realNow() + window.__clockOffset;
      window.__TAURI__ = {
        core: { invoke: async (command, args) => {
          if (command === 'gateway_url') return 'http://127.0.0.1:3200';
          if (command === 'desktop_auth_status') return { state: 'connected', subject: 'founder' };
          if (command === 'desktop_permission_status') return { permissions: {
            screen_recording: true, accessibility: true, event_synthesizing: true } };
          if (command === 'desktop_api_request') {
            window.__requests.push({ method: args.method, path: args.path });
            if (args.path === '/api/jobs/job_test') return new Promise(() => {});
            const data = args.path.startsWith('/api/workspaces/ws_test/jobs?')
              ? { items: [] } : FIXTURES[args.path] ?? null;
            return { status: 200, data };
          }
          return null;
        } }, event: { listen: async () => () => {} }
      };""".replace("FIXTURES", json.dumps({
        "/auth/session": {"authenticated": True, "subject": "founder"},
        "/api/ventures/ven_test": {"id": "ven_test", "workspace_id": "ws_test",
                                      "name": "Status QA", "raw_idea": "A saved idea"},
        "/api/ventures/ven_test/idea/sessions": {
            "id": "ses_test", "state": "validating", "case": "know",
            "project_intent": "startup", "initial_job_id": "job_test", "turns": [],
            "current_revision": {"id": "rev_test"}, "validation_runs": [],
        },
        "/api/ventures/ven_test/plans": [],
    }))
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(script)
        page.goto(f"{base}/v/ven_test/idea")
        page.get_by_role("heading", name="Reviewing your saved idea").wait_for()
        page.evaluate("window.__clockOffset = 46000")
        page.get_by_role("heading", name="Background status unavailable").wait_for(timeout=9000)
        assert page.get_by_role("button", name="Check saved status").is_visible()
        assert page.get_by_text("A saved idea", exact=True).is_visible()
        page.get_by_role("button", name="Check saved status").click()
        page.get_by_role("heading", name="Reviewing your saved idea").wait_for()
        requests = page.evaluate("window.__requests")
        assert sum("/idea/sessions" in item["path"] for item in requests) >= 2
        assert not any("/validate" in item["path"] or "/messages" in item["path"] for item in requests)
        browser.close()
