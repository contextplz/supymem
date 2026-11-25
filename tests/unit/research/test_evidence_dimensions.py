"""Synthetic contrasting excerpts exercise research classification without web I/O."""

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.signals.research import _structure_evidence  # noqa: E402
from app.validation.verdict import ValidationVerdict, compute_verdict  # noqa: E402
from app.research.service import dossier_evidence_dimensions  # noqa: E402


def test_agent_security_competitor_copy_is_not_customer_demand_or_negative_viability():
    sources = [
        {"url": f"https://security-vendor-{i}.example/product", "title": f"Agent security vendor {i}",
         "snippet": "AI agent security for enterprise teams. Request a demo.", "provider": "fixture"}
        for i in range(7)
    ]
    scan = {"mode": "live", "sources": sources, **_structure_evidence("AI agent security for enterprises", sources)}
    decision = compute_verdict(scan)
    assert scan["demand_signals"] == []
    assert scan["differentiation_score"] is None
    assert decision.verdict == ValidationVerdict.PRELIMINARY
    assert decision.confidence is None
    assert decision.demand_signal_count == 0
    assert "Possible alternative" in scan["strongest_for"]
    assert "closest alternative" in decision.recommendation
    assert decision.evidence_dimensions["pricing"]["status"] == "unknown"
    assert decision.evidence_dimensions["willingness_to_pay"]["status"] == "unknown"
    assert "voice-kit" not in str(scan)


def test_explicit_pricing_and_customer_request_are_leads_not_paid_conversion():
    sources = [
        {"url": "https://vendor.example/pricing", "title": "Security product",
         "snippet": "Plans start at $99 per month for security teams.", "provider": "fixture"},
        {"url": "https://www.reddit.com/r/security/thread", "title": "Community discussion",
         "snippet": "We need a safer way to review AI agent security permissions for enterprises.", "provider": "fixture"},
    ]
    scan = {"mode": "live", "sources": sources, **_structure_evidence("AI agent security for enterprises", sources)}
    dimensions = scan["evidence_dimensions"]
    assert dimensions["competition"]["status"] == "supporting_leads"
    assert dimensions["pricing"]["status"] == "supporting_leads"
    assert dimensions["customer_demand"]["status"] == "supporting_leads"
    assert dimensions["willingness_to_pay"]["status"] == "unknown"
    assert all(lead["url"] for row in dimensions.values() for lead in row["support"])
    assert compute_verdict(scan).verdict == ValidationVerdict.PRELIMINARY
    assert "Customer-request lead" in scan["strongest_for"]
    assert "reddit.com" in scan["strongest_for"]
    assert "customer-request lead" in compute_verdict(scan).recommendation


def test_single_negative_forum_excerpt_is_challenge_not_market_wide_rejection():
    sources = [{"url": "https://www.reddit.com/r/security/skeptic", "title": "A discussion",
                "snippet": "We would not pay for this approach.", "provider": "fixture"}]
    scan = {"mode": "live", "sources": sources, **_structure_evidence("AI agent security", sources)}
    row = scan["evidence_dimensions"]["willingness_to_pay"]
    assert row["status"] == "challenging_leads"
    assert row["support"] == []
    assert compute_verdict(scan).verdict == ValidationVerdict.PRELIMINARY
    assert "bounded test" in compute_verdict(scan).recommendation


def test_vendor_page_without_model_review_cannot_be_customer_voice():
    sources = [{"url": "https://agent-security-vendor.example/", "title": "Agent security",
                "snippet": "Need a safer agent security tool? Try our product.", "provider": "fixture"}]
    scan = {"mode": "live", "sources": sources, **_structure_evidence("AI agent security", sources)}
    assert scan["demand_signals"] == []
    assert scan["evidence_dimensions"]["customer_demand"]["status"] == "unknown"
    result = compute_verdict(scan)
    assert result.verdict == ValidationVerdict.PRELIMINARY
    assert result.demand_signal_count == 0

    deep = dossier_evidence_dimensions(
        {"source": [{"id": "rsf_vendor", "url": sources[0]["url"], "summary": sources[0]["snippet"],
                     "founder_flag": None}]},
        review=None,
    )
    assert deep["customer_demand"]["status"] == "unknown"
    assert deep["willingness_to_pay"]["status"] == "unknown"


def test_review_categories_add_competition_pricing_and_community_leads():
    findings = {"source": [
        {"id": "vendor", "url": "https://vendor.example/product", "summary": "A related order board."},
        {"id": "api", "url": "https://api.example/plans", "summary": "An order API."},
        {"id": "community", "url": "https://community.example/thread", "summary": "Bakery owners discuss order intake."},
    ]}
    review = {"assessments": [
        {"category": "adjacent_solution", "source_ids": ["vendor"], "assessment": "Possible existing workflow."},
        {"category": "api_service", "source_ids": ["api"], "assessment": "An order API."},
        {"category": "community", "source_ids": ["community"], "assessment": "Owners discuss this problem."},
    ], "service_checks": [{"source_id": "api", "pricing_quote": "Plans start at $12 per month."}]}
    dimensions = dossier_evidence_dimensions(findings, review)
    for area in ("competition", "pricing"):
        assert dimensions[area]["status"] == "supporting_leads"
        assert dimensions[area]["source_strength"] == "search_excerpt_unverified"
    assert dimensions["pricing"]["support"][0]["finding"] == "Plans start at $12 per month."
    for area in ("problem_validation", "customer_demand"):
        assert dimensions[area]["status"] == "unknown"
    assert dimensions["willingness_to_pay"]["status"] == "unknown"


def test_review_cannot_upgrade_excluded_or_unqualified_leads():
    findings = {"source": [
        {"id": "excluded", "url": "https://vendor.example", "summary": "Orders", "founder_flag": "exclude"},
        {"id": "unqualified", "url": "https://blog.example", "summary": "Orders", "evidence_kind": "search_lead_unverified"},
        {"id": "legacy_lead", "url": "https://legacy.example", "summary": "Orders", "category": "review_lead"},
    ]}
    review = {"assessments": [{"category": "community", "source_ids": ["excluded", "unqualified", "legacy_lead"], "assessment": "Owners discuss this problem."}],
              "service_checks": [{"source_id": "excluded", "pricing_quote": "$12 per month"}]}
    assert all(row["status"] == "unknown" for row in dossier_evidence_dimensions(findings, review).values())


def test_customer_pain_requires_domain_audience_and_not_founder_promotion():
    from app.research.evidence_dimensions import customer_problem_evidence
    context = {"problem": "collision repair estimating", "customer": "collision repair shop owners"}
    source = {"url": "https://reddit.com/r/Autobody/comments/1", "title": "Collision repair estimating",
              "snippet": "I run a collision repair shop and struggle with manual collision repair estimating."}
    assert customer_problem_evidence(source, context)
    assert not customer_problem_evidence(source)
    assert not customer_problem_evidence({**source, "snippet": "We are the founders of Salem Robotics. Our inspection robots solve a manual process."}, context)
    assert not customer_problem_evidence({**source, "title": "Industrial robots", "snippet": "I struggle with manual industrial robot inspections."}, context)
    assert not customer_problem_evidence({**source, "url": "https://vendor.example/product"}, context)


def test_stated_payment_intent_is_a_source_lead_not_a_purchase_across_domains():
    from app.research.evidence_dimensions import evidence_dimensions

    for audience, problem in (("warehouse managers", "inventory reconciliation"),
                              ("home gardeners", "balcony irrigation")):
        for intent in ("I would pay", "We will pay", "I'd pay", "We’d pay"):
            quote = f"{intent} for help with {problem}."
            source = {"id": "customer", "url": "https://reddit.com/r/discussion/comments/payment",
                      "title": audience, "snippet": quote, "summary": quote}
            dimensions = evidence_dimensions([source], [], context={"customer": audience, "problem": problem})
            row = dimensions["willingness_to_pay"]
            assert row["status"] == "supporting_leads"
            assert row["source_strength"] == "search_excerpt_unverified"
            assert row["support"] == [{"url": source["url"], "finding": quote}]
            assert dimensions["customer_demand"]["status"] == "unknown"
            assert dossier_evidence_dimensions({"source": [source]}, None)["willingness_to_pay"]["support"] == row["support"]
            for rejected in ({**source, "url": "https://vendor.example/product"},
                             {**source, "summary": f"I would not pay for {problem}."},
                             {**source, "summary": f"I wouldn't pay for {problem}."},
                             {**source, "summary": f"I'd never pay for {problem}."},
                             {**source, "founder_flag": "exclude"},
                             {**source, "evidence_kind": "search_lead_unverified"}):
                assert dossier_evidence_dimensions({"source": [rejected]}, None)["willingness_to_pay"]["support"] == []
