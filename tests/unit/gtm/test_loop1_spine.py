"""GTM Loop 1 unit tests — dossier gate, ICP, state machine, tone, no DM tools."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from datetime import datetime, timedelta, timezone

import app.actuation.tools  # noqa: F401
from app.actuation.registry import all_tools
from app.gtm.relationships.dossier import build_dossier_from_signal
from app.gtm.relationships.icp import load_icp, passes_icp, score_signal
from app.gtm.relationships.state_machine import can_transition
from app.gtm.relationships.tone import lint_draft
from app.signals.collectors.launch_hn import parse_launch_hits
from app.signals.collectors.yc_directory import parse_yc_companies


def test_icp_scores_yc_and_rejects_unknown():
    threshold = float(load_icp()["scoring"]["min_icp_score"])
    assert score_signal("yc_admit") >= threshold
    assert passes_icp("yc_admit")
    assert not passes_icp("random_noise")


def test_dossier_has_hook():
    d = build_dossier_from_signal(
        company_name="Acme",
        signal_kind="yc_admit",
        evidence_url="https://www.ycombinator.com/companies/acme",
        yc_batch="W26",
        person_name="Ada",
    )
    assert "YC W26" in d["hook"] or "W26" in d["hook"]
    assert d["hook"].strip()


def test_product_name_before_offer_is_refused():
    result = lint_draft(
        "Hey Ada,\n\nCongrats on YC W26. Try Supymem today!\n",
        hook="got into YC W26",
        step="congrats",
        product_name="Supymem",
    )
    assert result.refused
    assert "product_name_before_offer" in result.reasons


def test_congrats_without_hook_refused():
    result = lint_draft(
        "Hey there, congrats!",
        hook="",
        step="congrats",
        product_name=None,
    )
    assert result.refused
    assert "missing_dossier_hook" in result.reasons


def test_state_machine_researched_to_congratulated():
    ok = can_transition("researched", "congratulated")
    assert ok.ok
    bad = can_transition("new", "converted")
    assert not bad.ok


def test_no_dm_automation_tool_registered():
    ids = {t.tool_id for t in all_tools()}
    assert "email.sequence" in ids
    assert "email.read" in ids
    banned = {
        "linkedin.dm",
        "x.dm",
        "twitter.dm",
        "linkedin.message",
        "social.dm",
    }
    assert not (ids & banned)


def test_yc_parser_extracts_companies():
    since = datetime.now(timezone.utc) - timedelta(days=3650)
    rows = [
        {
            "name": "Acme AI",
            "slug": "acme-ai",
            "batch": "W26",
            "website": "https://acme.ai",
            "one_liner": "Widgets for founders",
        }
    ]
    parsed = parse_yc_companies(rows, since=since)
    assert parsed[0]["name"] == "Acme AI"
    assert parsed[0]["domain"] == "acme.ai"
    assert parsed[0]["yc_batch"] == "W26"


def test_launch_hn_parser():
    since = datetime.now(timezone.utc) - timedelta(days=30)
    hits = [
        {
            "title": "Launch HN: WidgetCo – does widgets",
            "objectID": "123",
            "created_at_i": int(datetime.now(timezone.utc).timestamp()),
            "url": "https://widgetco.example",
            "story_text": "Reach us at hello@widgetco.example",
        }
    ]
    parsed = parse_launch_hits(hits, since=since)
    assert parsed[0]["name"].startswith("WidgetCo")
    assert parsed[0]["email"] == "hello@widgetco.example"
