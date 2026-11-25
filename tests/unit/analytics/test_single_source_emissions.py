"""Exactly one emitter per hosted event: browser owns views/CTAs; server owns consented signup."""

from pathlib import Path

from tests.company_os_harness import load_company_os_app

load_company_os_app()

ROOT = Path(__file__).resolve().parents[3]
FIRSTLOOK = (ROOT / "services/company_os/app/api/firstlook_api.py").read_text(encoding="utf-8")
WAITLIST_API = (ROOT / "services/company_os/app/api/waitlist_api.py").read_text(encoding="utf-8")
WAITLIST_CAPTURE = (ROOT / "services/company_os/app/conversion/waitlist.py").read_text(encoding="utf-8")
FIRSTLOOK_WAITLIST = (ROOT / "services/company_os/app/firstlook/waitlist.py").read_text(encoding="utf-8")


def test_serve_site_html_does_not_emit_server_landing_view():
    assert "serve_site_html" in FIRSTLOOK
    # No server-side landing_view on the HTML GET path.
    assert 'event="landing_view"' not in FIRSTLOOK
    assert 'event="cta_click"' not in FIRSTLOOK
    assert "Browser beacon owns landing_view" in FIRSTLOOK


def test_capture_interest_does_not_emit_cta_or_signup():
    assert "async def capture_interest" in FIRSTLOOK
    assert "Do not emit signup here" in FIRSTLOOK
    assert "Browser beacon owns cta_click" in FIRSTLOOK
    # Interest path has no consent checkbox; must not mint signup.
    interest_block = FIRSTLOOK.split("async def capture_interest", 1)[1].split("\n@", 1)[0]
    assert "emit_browser_event" not in interest_block


def test_managed_waitlist_server_emits_only_signup_after_consent():
    assert 'event="signup"' not in WAITLIST_API
    assert 'event="cta_click"' not in WAITLIST_API
    assert 'event="landing_view"' not in WAITLIST_API
    assert "consent_required" in WAITLIST_CAPTURE or "waitlist_consent_required" in WAITLIST_CAPTURE
    # Consent gate before insert (quoted in orchestrator reply).
    assert (
        '(payload.get("consent") is not True and payload.get("consent") != "yes")'
        in WAITLIST_CAPTURE
    )
    # The public route delegates to capture; no second best-effort event write.
    submit = WAITLIST_API.split("async def submit_waitlist", 1)[1]
    assert "waitlist.capture(venture_id, payload)" in submit
    assert "_record_signup" not in submit
    # Capture adds the sole signup event after consent and test/duplicate gates,
    # then commits the waitlist request and event in one transaction.
    capture = WAITLIST_CAPTURE.split("def capture(", 1)[1].split("\ndef verified_receipt", 1)[0]
    assert capture.index('payload.get("consent")') < capture.index("session.add(entry)")
    assert capture.index("if entry is None:") < capture.index("session.add(AppEvent(")
    assert capture.index("if not is_test and not founder_import:") < capture.index("session.add(AppEvent(")
    assert capture.index("session.add(AppEvent(") < capture.index("session.commit()")
    assert capture.count("session.add(AppEvent(") == 1


def test_firstlook_rewrite_injects_beacon_not_server_events():
    assert "_inject_product_analytics" in FIRSTLOOK_WAITLIST
    assert "inject_into_html" in FIRSTLOOK_WAITLIST
    assert 'event="landing_view"' not in FIRSTLOOK_WAITLIST
    assert 'event="cta_click"' not in FIRSTLOOK_WAITLIST
