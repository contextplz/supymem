"""Research categoriser, licence gate, pin/exclude (docs/supymem/11)."""

from __future__ import annotations

from tests.security.test_connector_oauth_boundary import client as client

from tests.company_os_harness import load_company_os_app

FIXTURES = [
    ("Stripe API", "payments api for cards", "api_service"),
    ("Y Combinator batch", "yc company from last winter", "competitor_accelerator"),
    ("Series B startup", "raised a series a last year", "competitor_funded"),
    ("GitHub next.js", "open source mit license on github.com", "oss_repo"),
    ("Show HN waitlist", "posted on hacker news", "market_signal"),
    ("Pricing page", "their plans start at $9", "pricing_signal"),
    ("Direct rival", "competitor to our scheduling tool", "competitor_direct"),
    ("Alternative to Calendly", "an alternative to calendly", "competitor_direct"),
]


def test_categorizer_fixture_accuracy() -> None:
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.research.categorize import categorize_finding

    hits = 0
    for title, text, expected in FIXTURES:
        cat, _tags = categorize_finding(title, text)
        if cat == expected:
            hits += 1
    assert hits / len(FIXTURES) >= 0.9


def test_license_gate() -> None:
    load_company_os_app()
    from app.research.license import evaluate_license

    assert evaluate_license("MIT")["ok"] is True
    assert evaluate_license("AGPL-3.0")["ok"] is False
    assert evaluate_license("GPL-3.0")["recommendation"] == "fork"


def test_pin_exclude_and_export(client, monkeypatch) -> None:
    from tests.security.test_research_boundary import confirmed, dossier
    from tests.security.test_durable_journey_boundary import request, run_queued
    from tests.research_fixtures import install_research
    vid, _, _, _ = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    report = dossier(client, vid)
    first = report["findings"]["source"][0]
    base = f"/ventures/{vid}/research"
    pinned = request(client, "PATCH", base + f"/findings/{first['id']}", json={"founder_flag": "pin"})
    assert pinned.json()["founder_flag"] == "pin"
    exported = request(client, "GET", base + f"/dossiers/{report['id']}/export")
    assert exported.status_code == 200 and "Research dossier" in exported.text
    rerun = request(client, "POST", base + f"/dossiers/{report['id']}/rerun", json={"phase": "services"})
    assert rerun.status_code == 200 and rerun.json()["job_id"]
    run_queued("research.deep")
    items = request(client, "GET", base + "/dossiers").json()["items"]
    assert len(items) == 2
    latest = request(client, "GET", base + f"/dossiers/{items[0]['id']}").json()
    assert any(f["founder_flag"] == "pin" for group in latest["findings"].values() for f in group)
    old = request(client, "GET", base + f"/dossiers/{report['id']}").json()
    assert old["findings"]["source"][0]["founder_flag"] == "pin"
