"""A same-page query navigation must start a fresh venture, not reuse a receipt.

Set NEW_VENTURE_CONSOLE_URL to a local Console. All API responses are fixtures;
this test never writes a real venture or invokes a real desktop helper.
"""
import json
import os

import pytest


@pytest.mark.skipif(not os.getenv("NEW_VENTURE_CONSOLE_URL"), reason="local Console URL not supplied")
def test_new_venture_navigation_clears_committed_conversation_and_target():
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["NEW_VENTURE_CONSOLE_URL"].rstrip("/")
    receipt = {"status": "committed", "route": "startup", "venture_id": "ven_old", "workspace_id": "ws_old",
               "next_step": "idea", "action": {"kind": "idea", "venture_id": "ven_old", "href": "/v/ven_old/idea"},
               "message": "Old venture was created"}
    history = [{"text": "An old idea", "proposal": {"id": "pr_old", "route": "startup", "state": "committed",
               "message": "Old idea", "skipped_steps": [], "next_step": "idea", "expires_at": "2099-01-01T00:00:00Z",
               "receipt": receipt}}]
    fixtures = {"/auth/session": {"authenticated": True, "subject": "founder"},
                "/api/ui-config": {"founder_ui": "classic"},  # classic sidebar workspace picker
                "/api/ventures/ven_old": {"id": "ven_old", "workspace_id": "ws_old", "name": "Old fixture venture", "raw_idea": "An old idea"},
                "/api/ventures": {"items": [{"id": "ven_old", "name": "Old fixture venture"}]},
                "/api/entry/chat/history?target_venture_id=ven_old": {"items": history},
                "/api/entry/chat/history": {"items": history},
                "/api/ventures/ven_old/chat": {"messages": []}}
    script = """window.__requests = [];
    window.__TAURI__ = { core: { invoke: async (command, args) => {
      if (command === 'gateway_url') return 'http://127.0.0.1:3200';
      if (command === 'desktop_auth_status') return { state: 'connected', subject: 'founder' };
      if (command === 'desktop_permission_status') return { permissions: {screen_recording:true, accessibility:true,event_synthesizing:true} };
      if (command === 'desktop_api_request') {
        window.__requests.push({method: args.method, path: args.path, body: args.body});
        if (args.path === '/api/entry/chat/preview') return {status:200,data:{status:'clarify',question:'Who is this for?',choices:['A new audience']}};
        return {status:200, data: FIXTURES[args.path] ?? []};
      }
      return null;
    } }, event: {listen: async () => () => {}}};""".replace("FIXTURES", json.dumps(fixtures))
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(script)
        page.goto(f"{base}/new?venture=ven_old")
        page.get_by_role("heading", name="Old fixture venture").wait_for()
        # Same Next page, query change only: unlike page.goto this must preserve
        # the route component and reproduce the founder's actual navigation.
        # The native sidebar rail reveals its workspace picker on hover.
        page.locator("aside.sidebar").hover()
        page.locator("button[aria-haspopup='listbox']").click()
        page.get_by_role("link", name="+ New venture").click()
        page.wait_for_url(f"{base}/new")
        page.get_by_role("heading", name="What is your idea?").wait_for(timeout=5000)
        assert not page.get_by_role("heading", name="Old fixture venture").count()
        composer = page.get_by_role("textbox", name="What is your idea?")
        assert composer.input_value() == ""
        composer.fill("日本語の新しいアイデア")
        composer.evaluate("el => el.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, isComposing:true}))")
        assert not [r for r in page.evaluate("window.__requests") if r["path"] == "/api/entry/chat/preview"]
        composer.fill("A wholly new idea")
        page.get_by_role("button", name="Continue", exact=True).click()
        audience = page.get_by_role("textbox", name="Who is it for?", exact=True)
        audience.wait_for()
        assert not [r for r in page.evaluate("window.__requests") if r["path"] == "/api/entry/chat/preview"]
        audience.fill("Independent tutors")
        page.get_by_role("button", name="Continue", exact=True).click()
        page.get_by_text("Who is this for?", exact=True).wait_for()
        sent = [r for r in page.evaluate("window.__requests") if r["path"] == "/api/entry/chat/preview"]
        assert len(sent) == 1
        body = sent[0]["body"]
        assert body["text"] == "A wholly new idea\nAudience: Independent tutors"
        assert "target_venture_id" not in body
        browser.close()
