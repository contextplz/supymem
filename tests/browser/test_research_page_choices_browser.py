"""Rendered Research/AppShell composition with disclosed synthetic transport.

Reuse the existing isolated native-availability and Gateway fixture. The Next
renderer/components are real; authentication, saved research and admission
responses are synthetic. This is not actual Core/Gateway/native/founder proof.
Set GTM_CONSOLE_URL to the separately started matched isolated Console.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.browser.test_signin_permissions_browser import _context


pytestmark = pytest.mark.skipif(not os.getenv("GTM_CONSOLE_URL"), reason="isolated Console URL not supplied")


def test_one_research_choice_pair_retires_during_and_after_rerun(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    assert urlsplit(base).hostname in {"127.0.0.1", "localhost", "::1"}, "isolated loopback Console required"
    vid, wid, sid, rid = "ven_c6_fixture", "ws_c6_fixture", "idea_c6_fixture", "revision_c6_fixture"
    step = {"key": "site.build", "stage": "Page", "title": "Choose your next step",
            "detail": "Choose a private preview or customers first.", "chat_action": None, "payload": None,
            "cta_label": "Build the landing page and explore customers", "href": f"/v/{vid}/site?build=1",
            "alternatives": [{"choice": "build", "label": "Build the landing page", "href": f"/v/{vid}/site?build=1"},
                             {"choice": "skip", "label": "Find customers first", "href": f"/gtm?venture={vid}"}]}
    report = {"id": "dossier_c6_fixture", "kind": "deep", "state": "partial", "revision_id": rid,
              "findings": {"source": [{"id": "source_c6_fixture", "title": "Synthetic related page", "category": "source"}]},
              "summary": {"coverage_gaps": ["Demand remains unverified."],
                          "evidence_dimensions": {"pricing": {"status": "unknown"}},
                          "verdict": {"title": "Review saved evidence", "verdict": "test_first", "source_count": 1,
                                      "why": ["Synthetic saved research only."], "why_not": ["No customer proof."]}}}
    idea = {"id": sid, "state": "confirmed", "project_intent": "startup", "venture_id": vid,
            "workspace_id": wid, "current_revision": {"id": rid, "structured": {"project_intent": "startup"}}}
    job = {"id": "job_c6_fixture", "kind": "research.deep", "status": "scheduled", "venture_id": vid,
           "workspace_id": wid, "revision_id": rid, "attempts": 0, "max_attempts": 5}
    held, calls = [], []
    active = False
    with sync_playwright() as runner:
        browser, context, _ = _context(runner, native=True, connected=True, founder_ui="classic")
        base_url = urlsplit(base)
        allowed_origins = {(base_url.scheme, base_url.netloc), ("https", "gateway-fixture.invalid"),
                           ("https", "identity-fixture.invalid")}

        def allow_origin(route):
            target = urlsplit(route.request.url)
            if (target.scheme, target.netloc) in allowed_origins:
                route.fallback()
            else:
                route.abort()

        context.route("**/*", allow_origin)

        def fixture(route):
            request = route.request
            path = urlsplit(request.url).path
            calls.append((request.method, path))
            if path.endswith("/rerun") and request.method == "POST":
                held.append(route)  # Delayed synthetic admission, no Core state is forged.
                return
            if path == f"/api/ventures/{vid}/dashboard":
                # Deliberately retain the old cached receipt after admission.
                # This test proves retirement does not wait for the 15s poll.
                data = {"venture": {"id": vid}, "next_step": step, "run_status": {"state": "idle"}}
            elif path == f"/api/ventures/{vid}":
                data = {"id": vid, "workspace_id": wid, "name": "Synthetic Research QA", "manager_name": "Charlie"}
            elif path == f"/api/ventures/{vid}/research/dossiers":
                data = {"items": [report]}
            elif path == f"/api/ventures/{vid}/research/dossiers/{report['id']}":
                data = report
            elif path == f"/api/ventures/{vid}/idea/sessions":
                data = {"items": [{"id": sid, "state": "confirmed", "project_intent": "startup"}]}
            elif path == f"/api/ventures/{vid}/idea/sessions/{sid}":
                data = idea
            elif path == f"/api/workspaces/{wid}/jobs":
                data = {"items": [job] if active else []}
            elif path == f"/api/jobs/{job['id']}":
                data = job
            else:
                route.fallback()
                return
            route.fulfill(json=data)

        context.route("https://gateway-fixture.invalid/**", fixture)
        page = context.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})
        try:
            page.goto(f"{base}/v/{vid}/research", wait_until="domcontentloaded")
            intro = page.get_by_role("dialog", name="Meet Charlie", exact=True)
            if intro.count() and intro.is_visible():
                intro.get_by_role("button", name="Skip", exact=True).click()
            pairs = page.locator('[aria-label="Landing page or customers"]')
            expect(pairs).to_have_count(1, timeout=30_000)
            build = pairs.locator('[data-usage-control="page_choice_build"]')
            skip = pairs.locator('[data-usage-control="page_choice_skip"]')
            expect(build).to_have_count(1)
            expect(skip).to_have_count(1)
            # Build stays the recommended primary action; skip is an always-available secondary step.
            assert build.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(37, 99, 235)"
            expect(build).to_have_class("primary")
            expect(skip).to_have_class("ghost sm")
            expect(skip).to_be_enabled()
            sidebar = page.get_by_role("complementary", name="Research sources and history")
            layout = sidebar.locator("..").bounding_box()
            main = sidebar.locator("..").locator(":scope > div").bounding_box()
            side = sidebar.bounding_box()
            # Chat-first keeps Charlie docked beside every venture page, so the page shares the width with chat.
            assert 400 < layout["width"] <= 1200
            assert side["x"] >= main["x"] + main["width"] + 16
            assert abs(side["y"] - main["y"]) <= 1
            directory = Path(os.getenv("C6_BROWSER_ARTIFACT_DIR", str(tmp_path)))
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "synthetic-research-wide.png"), full_page=True)
            page.set_viewport_size({"width": 680, "height": 900})
            main = sidebar.locator("..").locator(":scope > div").bounding_box()
            side = sidebar.bounding_box()
            assert abs(side["x"] - main["x"]) <= 1
            assert abs(side["width"] - main["width"]) <= 1
            assert side["y"] >= main["y"] + main["height"]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(directory / "synthetic-research-narrow.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 900})
            expect(page.locator('[data-usage-control="research_customer_interviews"]')).to_have_count(1)
            page.locator("details").filter(has=page.locator('[data-usage-control="research_rerun"]')).locator(":scope > summary").click()
            with page.expect_request(lambda request: request.url.endswith("/rerun") and request.method == "POST"):
                page.get_by_role("button", name="Run research again", exact=True).click()
            expect(pairs).to_have_count(0, timeout=3_000)
            assert len(held) == 1
            active = True
            held.pop().fulfill(json={"job_id": job["id"]})
            expect(page.get_by_role("heading", name="Researching…", exact=True)).to_be_visible(timeout=3_000)
            running = page.get_by_role("heading", name="Researching…", exact=True).locator("xpath=ancestor::section[1]")
            header = page.get_by_role("heading", name="Decide what is worth building").locator("..").bounding_box()
            box = running.bounding_box()
            assert 16 <= box["y"] - header["y"] - header["height"] <= 24
            assert box["height"] < 300
            expect(pairs).to_have_count(0)
            assert sum(method == "POST" and path.endswith("/rerun") for method, path in calls) == 1
            assert not any(method == "POST" and (path.endswith("/page-choice") or "/pagebuilder/" in path) for method, path in calls)
            directory = Path(os.getenv("C6_BROWSER_ARTIFACT_DIR", str(tmp_path)))
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "synthetic-c6-research-rerun.png"), full_page=True)
        finally:
            context.close()
            browser.close()
