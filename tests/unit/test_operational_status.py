"""Operational status prefers reality over stale stage labels."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.summary import operational_status, published_site_is_public


def test_local_preview_cannot_be_called_a_public_landing_page():
    assert not published_site_is_public(True, "http://127.0.0.1:3300/w/x/site/")
    assert not published_site_is_public(True, "file:///tmp/site.html")
    assert not published_site_is_public(True, "https://localhost/site")
    assert not published_site_is_public(False, "https://labhire.supymem.com/")
    assert published_site_is_public(True, "https://labhire.supymem.com/")


def test_paused_is_explicit():
    s = operational_status(stage="live", onboarding_status="paused", paused_reason="awaiting_key")
    assert s["code"] == "paused"
    assert s["label"] == "Paused"


def test_setup_without_artifacts():
    s = operational_status(stage="org_planning", agent_summary={"hired": 0, "working": 0, "watching": 0})
    assert s["label"] == "In setup"


def test_stuck_stage_with_site_and_team_is_still_setup():
    s = operational_status(
        stage="org_planning",
        agent_summary={"hired": 9, "working": 9, "watching": 0},
        signals={"has_site": True, "has_team": True},
    )
    assert s["code"] == "setup"
    assert s["label"] == "In setup"


def test_onboarding_done_is_live():
    s = operational_status(
        stage="org_planning",
        onboarding_status="done",
        agent_summary={"hired": 4, "working": 1, "watching": 0},
        signals={"has_site": True, "has_team": True},
    )
    assert s["code"] == "live"


def test_readiness_review_is_a_private_founder_gate():
    s = operational_status(
        stage="readiness_review",
        onboarding_status="done",
        agent_summary={"hired": 4, "working": 0, "watching": 0},
        signals={"has_site": True, "has_team": True},
    )
    assert s["code"] == "readiness_review"
    assert s["label"] == "Ready to launch"
    assert "private" in s["detail"]


def test_published_site_overrides_stale_readiness_stage():
    s = operational_status(
        stage="readiness_review",
        onboarding_status="done",
        agent_summary={"hired": 4, "working": 0, "watching": 0},
        signals={
            "has_site": True,
            "has_team": True,
            "website_published": True,
        },
    )
    assert s == {
        "code": "live",
        "label": "Landing page live",
        "detail": "Your early-access page is live. Next: get your first sign-ups.",
    }


def test_watching_shadow_team():
    s = operational_status(
        stage="live",
        agent_summary={"hired": 9, "working": 0, "watching": 8},
    )
    assert s["code"] == "watching"
    assert "Activate" in s["detail"]


def test_live_team_copy_matches_console_without_claiming_idle_work():
    for hired, working, detail in [
        (1, 0, "1 teammate"),
        (4, 0, "4 teammates"),
        (4, 2, "2 of 4 teammates working"),
    ]:
        status = operational_status(stage="live", agent_summary={"hired": hired, "working": working})
        assert status["detail"] == detail
