import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


def test_pricing_gate_requires_a_product_price(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.pricing import pricing_evidence_quote

    assert pricing_evidence_quote("Job seekers face a difficult market. Pricing is unclear for these tools.") is None
    assert pricing_evidence_quote("Average salary is $80,000. Employers continue hiring.") is None
    assert pricing_evidence_quote("Average salary is $80,000 per year. Employers continue hiring.") is None
    assert pricing_evidence_quote("JobWizard plans start at $12 per month. Features vary.") == "JobWizard plans start at $12 per month."
    assert pricing_evidence_quote("The free tier includes 20 searches.") == "The free tier includes 20 searches."


def test_saved_report_masks_old_generic_pricing_quote(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.service import _public

    now = datetime.now(timezone.utc)
    old_review = {"overview": "Pricing unknown", "service_checks": [
        {"source_id": "rsf_jobwizard", "pricing_quote": "JobWizard is a platform for job applications. This article discusses hiring trends but gives no plan price.",
         "limits_quote": None, "credentials_quote": None},
    ], "competitor_checks": [
        {"source_id": "rsf_jobwizard", "pricing_quote": "JobWizard is a platform for job applications. This article discusses hiring trends but gives no plan price.",
         "product_quote": "JobWizard is a platform for job applications."},
    ]}
    row = SimpleNamespace(id="dossier_1", venture_id="venture_1", revision_id="rev_1", kind="deep",
                          state="partial", workspace_id="workspace_1", summary=json.dumps({"review": old_review,
                          "_runtime": {"operations": {}}}), budget_calls=12, calls_used=2,
                          created_at=now, completed_at=now)
    report = _public(row, SimpleNamespace(get=lambda *_: None, scalar=lambda *_: None))
    assert report["summary"]["review"]["service_checks"][0]["pricing_quote"] is None
    assert report["summary"]["review"]["competitor_checks"][0]["pricing_quote"] is None
    assert old_review["service_checks"][0]["pricing_quote"] is not None


def test_saved_research_exposes_only_count_categories_from_private_receipts(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.service import _public

    now = datetime.now(timezone.utc)
    counts = {"provider_rows": 6, "parsed_rows": 5, "off_topic": 4, "admitted": 1,
              "secret_query": "clinic manager's private note", "unsafe_url": True}
    row = SimpleNamespace(id="dossier_2", venture_id="venture_1", revision_id="rev_2", kind="deep",
                          state="partial", workspace_id="workspace_1", summary=json.dumps({"_runtime": {
                              "operations": {"direct": {"state": "completed", "usage": {"provider": "firecrawl"},
                                                        "response": {"source_counts": counts,
                                                                     "items": [{"snippet": "private response"}]}}}}}),
                          budget_calls=12, calls_used=1, created_at=now, completed_at=now)
    public = _public(row, SimpleNamespace(get=lambda *_: None, scalar=lambda *_: None))["summary"]["operations"][0]
    assert public["source_counts"] == {"provider_rows": 6, "parsed_rows": 5, "off_topic": 4, "admitted": 1}
    assert "private" not in json.dumps(public) and "secret_query" not in json.dumps(public)


@pytest.mark.parametrize("text", [
    "This product generates $69K/month in revenue.",
    "My subscription startup has earnings of $7k/month.",
    "The subscription business makes $69K/month.",
    "Creators earn $69K/month from their subscription plans.",
    "Our subscription product generates $69K/month.",
    "Our subscription product brings in about USD 69,000/month.",
    "MRR: $69,000/month.",
    "Annual subscription revenue reached USD 690,000.",
    "We raised $2M for our monthly subscription business.",
    "The product valuation is $10M and pricing is discussed here.",
    "A salary of $80,000 per year.",
    "Marketing costs $3,000/month.",
    "Operating costs: $200 per month.",
    "The market size for annual subscriptions is $5B.",
    "We have 100 users and 5 sales.",
    "$7k/month.",
    "We plan to charge $9/month.",
    "Suggested price: $19 per seat.",
    "Pricing could cost $9 per month.",
    "Annual subscription pricing is not available.",
    "Price unavailable: $99/month.",
    "No free tier is available.",
    "The free tier is not available.",
    "Plans start at $12invalid/month.",
])
def test_financial_numbers_and_proposed_prices_do_not_become_verified_offer_prices(text):
    load_company_os_app()
    from app.research.pricing import pricing_evidence_quote
    from app.research.service import dossier_evidence_dimensions
    from app.research.verdict import research_verdict

    findings = {"source": [{"id": "rsf_context", "url": "https://context.example/story", "summary": text,
        "category": "source", "evidence_kind": "search_excerpt", "page_verified": True,
        "additional_excerpts": [{"text": text, "verified_page": True}]}]}
    assert pricing_evidence_quote(text) is None
    dimensions = dossier_evidence_dimensions(findings, None)
    assert dimensions["pricing"]["status"] == "unknown"
    assert dimensions["pricing"]["support"] == []
    verdict = research_verdict("partial", findings, dimensions, None)
    assert "pricing" not in " ".join(verdict["why"])
    price_verdict = research_verdict("partial", findings, {"pricing": dimensions["pricing"]}, None)
    assert "advertised offer pricing" in " ".join(price_verdict["why_not"])


@pytest.mark.parametrize("text", [
    "JobWizard plans start at $12 per month.",
    "Prices start at €9,99/month.",
    "One-time license: USD 49.",
    "An annual license is $10,000 per year.",
    "Setup fee: $49.",
    "The software costs £19 one time.",
    "$19 per seat per month.",
    "€9,50/user/month.",
    "Starter | $10/month | 5 projects",
    "Pro $29/month",
    "| Business | $49/month | 20 users |",
    "Basic plan: $99/year.",
    "The free tier includes 20 searches.",
    "The plan is free.",
    "The $12/month Pro plan generates reports.",
])
def test_offer_prices_and_whole_pricing_rows_remain_cited_leads_not_venture_demand(text):
    load_company_os_app()
    from app.research.pricing import pricing_evidence_quote
    from app.research.service import dossier_evidence_dimensions
    from app.research.verdict import research_verdict

    findings = {"source": [{"id": "rsf_offer", "url": "https://offer.example/pricing", "summary": text,
        "category": "source", "evidence_kind": "search_excerpt", "page_verified": True,
        "additional_excerpts": [{"text": text, "verified_page": True}]}]}
    assert pricing_evidence_quote(text) == text
    dimensions = dossier_evidence_dimensions(findings, None)
    assert dimensions["pricing"]["status"] == "supporting_leads"
    assert all(row["finding"] == text for row in dimensions["pricing"]["support"])
    assert dimensions["willingness_to_pay"]["status"] == "unknown"
    verdict = research_verdict("partial", findings, dimensions, None)
    assert "advertised offer pricing" in " ".join(verdict["why"])
    assert not any("willingness to pay" in line for line in verdict["why"])


def test_legacy_saved_report_projects_current_price_checks_without_rewriting_assessment():
    load_company_os_app()
    from app.research.service import _public, dossier_evidence_dimensions
    from app.research.verdict import research_verdict

    now = datetime.now(timezone.utc)
    revenue, price = "A subscription business has $69K/month in revenue.", "Plans start at $12 per month."
    document = {"review": {"overview": "Pricing and buying evidence were unknown when this assessment was saved.",
        "service_checks": [], "competitor_checks": [
            {"source_id": "rsf_story", "pricing_quote": revenue},
            {"source_id": "rsf_offer", "pricing_quote": price}]},
        "coverage_gaps": ["Historical model pricing gap."], "_runtime": {"operations": {}}}
    raw = json.dumps(document)
    row = SimpleNamespace(id="dossier_legacy", venture_id="venture", revision_id="rev", kind="deep",
        state="partial", workspace_id="workspace", summary=raw, budget_calls=12, calls_used=2,
        created_at=now, completed_at=now)
    projected = _public(row, SimpleNamespace(get=lambda *_: None, scalar=lambda *_: None))
    review = projected["summary"]["review"]
    assert review["competitor_checks"][0]["pricing_quote"] is None
    assert review["competitor_checks"][1]["pricing_quote"] == price
    assert review["service_checks"] == []
    findings = {"source": [{"id": source, "url": "https://sources.example/" + source,
        "summary": text, "category": "source", "evidence_kind": "search_excerpt", "page_verified": True,
        "additional_excerpts": [{"text": text, "verified_page": True}]}
        for source, text in [("rsf_story", revenue), ("rsf_offer", price)]]}
    dimensions = dossier_evidence_dimensions(findings, review)
    assert {lead["url"] for lead in dimensions["pricing"]["support"]} == {"https://sources.example/rsf_offer"}
    verdict = research_verdict("partial", findings, dimensions, review)
    assert "advertised offer pricing" in " ".join(verdict["why"])
    assert review["overview"] == document["review"]["overview"]
    assert projected["summary"]["coverage_gaps"] == ["Compare current prices and included features for the closest alternatives."]
    assert row.summary == raw  # read projection never overwrites immutable model prose/quotes


@pytest.mark.parametrize(("text", "expected"), [
    ("Revenue reached $69K/month. Plans start at $12/month.", "Plans start at $12/month."),
    ("Revenue reached $69K/month and plans start at $12/month.", "plans start at $12/month."),
    ("Revenue reached $69K/month; the Pro plan costs $12/month.", "the Pro plan costs $12/month."),
    ("Our subscription business makes $69K/month and plans start at $12/month.", "plans start at $12/month."),
    ("Creators earn $69K/month, but the advertised plan costs $12/month.", "but the advertised plan costs $12/month."),
    ("The Pro plan costs $12/month, while revenue reached $69K/month.", "The Pro plan costs $12/month"),
    ("The annual subscription revenue plan is $69K/month.", None),
])
def test_mixed_financial_passages_keep_only_a_separately_stated_offer_clause(text, expected):
    load_company_os_app()
    from app.research.pricing import pricing_evidence_quote
    from app.research.service import dossier_evidence_dimensions

    assert pricing_evidence_quote(text) == expected
    findings = {"source": [{"id": "rsf_mixed", "url": "https://mixed.example/story", "summary": text,
        "category": "source", "evidence_kind": "search_excerpt", "page_verified": True,
        "additional_excerpts": [{"text": text, "verified_page": True}]}]}
    pricing = dossier_evidence_dimensions(findings, None)["pricing"]
    assert pricing["status"] == ("supporting_leads" if expected else "unknown")
    assert {lead["finding"] for lead in pricing["support"]} == ({expected} if expected else set())
