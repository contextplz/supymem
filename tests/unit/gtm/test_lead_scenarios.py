"""Lab lead scenario resolver + CSV export shape."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.gtm.lead_scenarios import build_query, list_presets, resolve_scenario
from app.gtm.openleads_bridge import leads_to_csv_bytes


def test_list_presets_nonempty():
    presets = list_presets()
    assert len(presets) >= 5
    assert all(p.get("id") and p.get("query") for p in presets)


def test_resolve_custom_builds_query():
    s = resolve_scenario(
        industry="Fintech",
        role="CMO",
        geo="London",
        keyword="payments",
    )
    assert s["id"] == "custom"
    assert "Fintech" in s["query"]
    assert "London" in s["query"]


def test_resolve_preset_by_id():
    s = resolve_scenario(scenario_id="sf_ai_founders")
    assert s["id"] == "sf_ai_founders"
    assert s["mode"] == "preset"
    assert "San Francisco" in s["query"] or s.get("geo") == "San Francisco"


def test_resolve_random_returns_fields():
    s = resolve_scenario(randomize=True)
    assert s.get("query")
    assert s.get("industry")
    assert s.get("mode") in {"preset_random", "fully_random"}


def test_build_query_prefers_explicit():
    assert build_query(query="  hello world  ") == "hello world"


def test_csv_export_has_header_and_row():
    raw = leads_to_csv_bytes(
        [
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.com",
                "organization": "Analytical",
                "source": "test",
            }
        ]
    )
    text = raw.decode("utf-8")
    assert "email" in text.splitlines()[0]
    assert "ada@example.com" in text
