"""Real Console + signed Core reads/writes with synthetic saved research evidence.

Provider evidence, identity and native transport are fixtures. The dossier API
rebuilds companies/citations from real database rows, and exclusion uses its real
signed PATCH route. No provider, profile, source, delivery or production calls.
Run only with the separately started matched loopback Console in GTM_CONSOLE_URL.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

import pytest

from tests.browser.test_gtm_journey_browser import (
    _install_forwarder,
    _screenshot,
    _skip_intro,
    journey_db as journey_db,
    rendered_core as rendered_core,
)
from tests.security.test_core_signed_boundary import headers


pytestmark = pytest.mark.skipif(not os.getenv("GTM_CONSOLE_URL"), reason="isolated Console URL not supplied")


def _seed_saved_prior_art(d):
    """Synthetic provider receipts, never synthetic dossier API responses."""
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
    from core.clock import utcnow

    sid, rid, did = "idea_prior_browser", "revision_prior_browser", "dossier_prior_browser"
    alpha, beta, names = "source_prior_alpha", "source_prior_beta", "source_prior_names"
    alpha_quote = "Alpha manages bakery orders. Plans cost $29 per month."
    beta_quote = "Beta managed bakery orders. The company shut down because support costs became unsustainable."
    beta_lesson = "Check the support cost of manual order workflows."
    pattern = "Compare existing bakery order workflows before building."
    now = utcnow()
    rows = [
        (alpha, "Synthetic Alpha homepage", "https://alpha.example/", alpha_quote,
         {"evidence_kind": "web", "page_verified": True, "prior_art": {
             "company_name": "Alpha", "company_url": "https://alpha.example/", "source_kind": "web",
             "date": "2025-06-01", "author": "alpha-builder", "author_url": "https://github.com/alpha-builder"}}),
        (beta, "Synthetic Beta post-mortem", "https://www.ycombinator.com/companies/beta", beta_quote,
         {"evidence_kind": "yc_company", "prior_art": {
             "company_name": "Beta", "company_url": "https://beta.example/", "source_kind": "yc_company",
             "status": "inactive", "date": "2021-04-01", "author": "beta-builder", "author_url": "https://github.com/beta-builder"}}),
        (names, "Synthetic roundup names only", "https://roundup.example/alternatives", "Ghost is an unconfirmed candidate.",
         {"evidence_kind": "name_source", "prior_art": {
             "company_name": "Ghost", "company_url": "https://ghost.example/", "source_kind": "name_source"}}),
    ]
    review = {"overview": "Synthetic prior-attempt fixture for rendered integration.",
        "observations": [{"source_id": alpha, "quote": alpha_quote}, {"source_id": beta, "quote": beta_quote}],
        "competitor_checks": [{"source_id": alpha, "product_quote": "Alpha manages bakery orders.", "pricing_quote": "Plans cost $29 per month."}],
        "service_checks": [], "proposed_tests": [], "assessments": [
            {"kind": "inference", "category": "competitor_alternative", "source_ids": [beta], "assessment": beta_lesson},
            {"kind": "inference", "category": "competitor_alternative", "source_ids": [alpha, beta], "assessment": pattern},
        ]}
    with d["factory"]() as session:
        session.add(IdeaSession(id=sid, workspace_id=d["wid"], venture_id=d["vid"], case="know", state="confirmed",
            current_revision_id=rid, confirmed_revision_id=rid, confirmed_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id=rid, session_id=sid, workspace_id=d["wid"], venture_id=d["vid"], rev=1,
            source="founder", summary="Synthetic bakery order fixture", created_at=now,
            structured=json.dumps({"project_intent": "startup", "problem": "Manual bakery orders", "customer": "Independent bakeries", "offer": "Order management"})))
        session.add(ResearchDossier(id=did, workspace_id=d["wid"], venture_id=d["vid"], revision_id=rid,
            kind="deep", state="complete", created_at=now, completed_at=now,
            summary=json.dumps({"_runtime": {"operations": {}}, "review": review, "coverage_gaps": ["Synthetic source evidence only."],
                # The API must rebuild this marker; fake company API payloads cannot satisfy the test.
                "prior_art": {"companies": [], "coverage_gaps": ["Synthetic source evidence only."], "cost": {}}})))
        session.flush()
        for fid, title, url, quote, evidence in rows:
            session.add(ResearchFinding(id=fid, dossier_id=did, workspace_id=d["wid"], venture_id=d["vid"], category="source",
                title=title, url=url, source="synthetic_browser_fixture", summary=quote, created_at=now,
                evidence=json.dumps({**evidence, "observed_at": now.isoformat()})))
        session.commit()
    return {"dossier": did, "beta_finding": beta, "beta_lesson": beta_lesson, "pattern": pattern}


def test_saved_prior_art_modes_timeline_and_exclusion_use_signed_core(rendered_core, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    parsed = urlsplit(base)
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}, "isolated loopback Console required"
    d, client, _workers, sends = rendered_core
    fixture = _seed_saved_prior_art(d)
    dossier_path = f"/ventures/{d['vid']}/research/dossiers/{fixture['dossier']}"

    def saved():
        response = client.get(dossier_path, headers=headers(dossier_path, subject=d["actor"]))
        assert response.status_code == 200, response.text
        return response.json()["summary"]["prior_art"]

    assert {company["name"] for company in saved()["companies"]} == {"Alpha", "Beta"}
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_navigation_timeout(90_000)
        external_requests = []

        def isolate(route):
            target = urlsplit(route.request.url)
            if (target.scheme, target.netloc) == (parsed.scheme, parsed.netloc):
                route.fallback()
            else:
                external_requests.append(route.request.url)
                route.abort()

        page.route("**/*", isolate)
        requests = _install_forwarder(page, base, rendered_core)
        try:
            page.goto(f"{base}/v/{d['vid']}/research", wait_until="domcontentloaded")
            _skip_intro(page)
            prior = page.get_by_role("region", name="Who tried this before", exact=True)
            playwright.expect(prior).to_be_visible(timeout=30_000)
            # The export button is intentionally hidden while Simple mode is
            # collapsed. Locate by its always-visible summary, then assert it.
            full_report = page.locator("details").filter(has=page.locator("summary", has_text="Read the full research report"))
            playwright.expect(full_report).to_have_count(1)
            assert not full_report.evaluate("el => el.open"), "Simple mode must keep the full report collapsed"
            playwright.expect(prior.get_by_text("Public page available", exact=True)).to_be_visible()
            playwright.expect(prior.get_by_text("Inactive", exact=True)).to_be_visible()
            playwright.expect(prior.get_by_role("heading", name=re.compile(r"^Beta\b"))).to_be_visible()
            playwright.expect(prior.get_by_text("Plans cost $29 per month.", exact=True)).to_be_visible()
            playwright.expect(prior.get_by_text("Why it struggled", exact=True)).to_be_visible()
            playwright.expect(prior.get_by_text(fixture["beta_lesson"], exact=True)).to_be_visible()
            playwright.expect(prior.get_by_text("Pattern · Inference", exact=True)).to_be_visible()
            playwright.expect(prior.get_by_text("Ghost", exact=True)).to_have_count(0)
            profile = prior.locator('[data-usage-control="research_prior_profile"]').filter(has_text="beta-builder")
            playwright.expect(profile).to_have_attribute("href", "https://github.com/beta-builder")
            playwright.expect(profile).to_have_attribute("rel", "noopener noreferrer")
            timeline = prior.locator("details").filter(has=page.locator('[data-usage-control="research_prior_timeline"]')).first
            timeline.locator("summary").click()
            playwright.expect(timeline.locator('time[datetime="2025-06-01"]')).to_be_visible()
            playwright.expect(prior.locator('[data-usage-control="research_prior_source"]').first).to_have_attribute("target", "_blank")
            _screenshot(page, tmp_path, "synthetic-prior-art-simple.png")

            detail = page.get_by_role("group", name="Detail level", exact=True)
            detail.get_by_role("button", name="Detailed", exact=True).click()
            playwright.expect(detail.get_by_role("button", name="Detailed", exact=True)).to_have_attribute("aria-pressed", "true")
            playwright.expect(prior).to_be_visible()
            assert full_report.evaluate("el => el.open")
            detail.get_by_role("button", name="Simple", exact=True).click()
            playwright.expect(detail.get_by_role("button", name="Simple", exact=True)).to_have_attribute("aria-pressed", "true")
            playwright.expect(prior).to_be_visible()

            feedback = page.get_by_role("form", name="Research feedback", exact=True)
            playwright.expect(feedback).to_be_visible()
            feedback.get_by_label("Was this research useful?").select_option("4")
            feedback.get_by_label("Did you learn something new?").select_option("3")
            feedback.get_by_label("Would you act on these findings?").select_option("5")
            feedback.get_by_label("What was missing? (optional)").fill("Pricing history missing")
            feedback_path = f"/ventures/{d['vid']}/feedback"
            expected_feedback = {"kind": "research_prior_art_usefulness", "dossier_id": fixture["dossier"],
                "ratings": {"useful": 4, "new": 3, "actionable": 5}, "note": "Pricing history missing"}
            with page.expect_response(lambda response: response.url.endswith(feedback_path) and response.request.method == "POST") as submitted:
                feedback.get_by_role("button", name="Share feedback", exact=True).click()
            assert submitted.value.status == 200
            posted = submitted.value.request.post_data_json
            assert posted["source"] == "founder" and json.loads(posted["text"]) == expected_feedback
            feedback_id = submitted.value.json()["id"]
            assert isinstance(feedback_id, str) and feedback_id
            playwright.expect(feedback.get_by_role("status")).to_have_text("Thanks. Your feedback was saved.")
            from app.db.models import FeedbackEvent
            with d["factory"]() as session:
                receipt = session.get(FeedbackEvent, feedback_id)
                assert receipt is not None and receipt.venture_id == d["vid"]
                assert receipt.source == receipt.trust == "founder"
                assert json.loads(receipt.raw_text) == expected_feedback
            assert sum(method == "POST" and path == feedback_path for method, path in requests) == 1

            # Returning to Simple does not discard the user's open source details.
            source = page.get_by_role("complementary", name="Research sources and history").locator("li").filter(has=page.locator("summary > strong").filter(has_text="Synthetic Beta post-mortem"))
            playwright.expect(source).to_have_count(1)
            source.locator("summary").click()
            finding_path = f"/ventures/{d['vid']}/research/findings/{fixture['beta_finding']}"
            with page.expect_response(lambda response: response.url.endswith(finding_path) and response.request.method == "PATCH") as patch:
                source.get_by_role("button", name="Exclude finding", exact=True).click()
            assert patch.value.status == 200
            assert patch.value.request.post_data_json == {"founder_flag": "exclude"}
            playwright.expect(prior.get_by_role("heading", name=re.compile(r"^Beta\b"))).to_have_count(0, timeout=30_000)
            playwright.expect(prior.get_by_text(fixture["beta_lesson"], exact=True)).to_have_count(0)
            playwright.expect(prior.get_by_text("Pattern · Inference", exact=True)).to_have_count(0)
            playwright.expect(prior.get_by_role("link", name="beta-builder", exact=True)).to_have_count(0)
            from app.db.supymem_models import ResearchFinding
            with d["factory"]() as session:
                assert json.loads(session.get(ResearchFinding, fixture["beta_finding"]).evidence)["founder_flag"] == "exclude"
            refreshed = saved()
            assert [company["name"] for company in refreshed["companies"]] == ["Alpha"]
            assert not any(lesson["text"] == fixture["beta_lesson"] for lesson in refreshed["lessons"])
            assert all(lesson["kind"] == "anecdote" for lesson in refreshed["lessons"])
            assert not any(person["name"] == "beta-builder" for person in refreshed["people"])
            assert ("PATCH", finding_path) in requests
            assert not sends
            assert not any(method == "POST" and path.endswith(("/rerun", "/regenerate", "/send", "/publish")) for method, path in requests)
            # Existing font CSS/favicon attempts remain aborted. Only these
            # exact static asset endpoints are expected; source/profile/API
            # navigation would still fail this assertion without network I/O.
            expected_assets = {("https", "www.google.com", "/s2/favicons"), ("https", "fonts.googleapis.com", "/css2")}
            assert all((urlsplit(url).scheme, urlsplit(url).hostname, urlsplit(url).path) in expected_assets for url in external_requests), [
                (urlsplit(url).scheme, urlsplit(url).hostname, urlsplit(url).path) for url in external_requests
            ]
            _screenshot(page, tmp_path, "synthetic-prior-art-excluded.png")
        finally:
            browser.close()
