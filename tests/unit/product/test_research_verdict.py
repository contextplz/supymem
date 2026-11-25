from tests.company_os_harness import load_company_os_app
import pytest


def research_verdict(*a, **kw):
    load_company_os_app()
    from app.research.verdict import research_verdict as fn
    return fn(*a, **kw)


def test_paying_customers_are_demand_evidence_not_missing_public_validation():
    for findings in (SRC, {}):
        result = research_verdict("partial", findings, dims(problem_validation="unknown", customer_demand="unknown", competition="unknown"), None, has_customers=True)
        assert result["title"] == "Grow what's already working"
        assert "paying customers" in result["why"][0]
        assert "acquisition channel" in result["why_not"][0]
        assert not any("No strong public evidence" in text or "only customers can" in text for text in result["why_not"])
        assert result["next_step"].startswith("Find customers")
    assert research_verdict("running", SRC, None, None, has_customers=True)["title"] == "Researching…"

SRC = {"source": [{"id": str(i), "category": "source", "evidence_kind": "search_excerpt"} for i in range(4)],
       "review_lead": [{"id": "l", "category": "review_lead", "evidence_kind": "search_lead_unverified"}]}


def dims(**kw):
    return {k: {"status": v} for k, v in kw.items()}


def test_all_unknown_is_test_first_with_both_sides():
    v = research_verdict("partial", SRC, dims(problem_validation="unknown", pricing="unknown"), None)
    assert v["verdict"] == "test_first" and v["source_count"] == 4 and v["lead_count"] == 1
    assert v["why"] and v["why_not"] and "the problem" in " ".join(v["why_not"])


def verified_fixture():
    findings = {"source": [
        {"id": str(i), "url": f"https://example.org/{i}", "category": "source",
         "evidence_kind": "search_excerpt", "page_verified": True,
         "additional_excerpts": [{"verified_page": True, "text": text}]}
        for i, text in enumerate(["I have this problem.", "I need a solution.", "Plans start at $10."])]}
    dimensions = {name: {"status": "supporting_leads", "support": [
        {"url": f"https://example.org/{i}", "finding": text, "verified": True}]}
        for name, i, text in [("problem_validation", 0, "I have this problem."),
                             ("customer_demand", 1, "I need a solution.")]}
    return findings, dimensions


def test_supported_is_proceed():
    findings, dimensions = verified_fixture()
    v = research_verdict("complete", findings, dimensions, None)
    assert v["verdict"] == "proceed" and v["title"] == "Recommended to proceed"


def test_challenged_finished_research_is_not_yet():
    findings, dimensions = verified_fixture()
    dimensions = {"risks": {"status": "challenging_leads", "challenge": [
        {"url": "https://example.org/0", "finding": "I have this problem.", "verified": True}]}}
    assert research_verdict("partial", findings, dimensions, None)["verdict"] == "not_yet"


def test_unfinished_and_outdated_research_have_status_without_a_verdict():
    for state, expected, title in [("running", "in_progress", "Researching…"),
                                   ("failed", "failed", "Research didn't finish"),
                                   ("outdated", "outdated", "Research is outdated")]:
        v = research_verdict(state, SRC, dims(risks="challenging_leads"), None, ["A clear customer"])
        assert v["verdict"] == expected and v["title"] == title
        assert v["why"] == [] and v["why_not"] == []
        assert v["source_count"] == 4 and v["lead_count"] == 1
        if state == "running":
            assert v["next_step"] == ""
        else:
            assert "Run research again" in v["next_step"]


def test_why_quotes_idea_review_strengths_before_generic_reasons():
    v = research_verdict("complete", SRC, dims(problem_validation="unknown"), None, ["Clear, reachable customer."])
    assert v["why"][0] == "Your idea review found: Clear, reachable customer."
    assert "Planning is cheap" not in " ".join(v["why"])


def test_zero_sources_is_not_presented_as_a_verdict():
    v = research_verdict("complete", {}, None, None)
    assert v["verdict"] == "no_sources" and v["source_count"] == 0 and v["why"] == []
    assert "no usable sources" in v["why_not"][0] and "Proceed" not in v["title"]


def test_dimension_status_and_competitor_category_never_establish_positive_evidence():
    v = research_verdict("complete", SRC, dims(pricing="supporting_leads", competition="supporting_leads"),
                         {"assessments": [{"category": "competitor_direct", "source_ids": ["0"]}]})
    assert v["verdict"] == "test_first"
    assert "pricing" in " ".join(v["why_not"])
    assert not any("real signals" in x or "buying" in x or "already exist" in x for x in v["why"])


def test_verified_dimension_entry_must_match_admitted_nonexcluded_page_passage():
    findings, dimensions = verified_fixture()
    dimensions["pricing"] = {"status": "supporting_leads", "support": [
        {"url": "https://example.org/2", "finding": "$99 monthly", "verified": True}]}
    findings["source"][0]["founder_flag"] = "exclude"
    v = research_verdict("complete", findings, dimensions, None)
    assert v["verdict"] == "test_first"
    assert "pricing" in " ".join(v["why_not"])
    assert "the problem" in " ".join(v["why_not"])
    assert "pricing" not in " ".join(v["why"])


def test_actual_offering_quote_does_not_establish_buyers_or_willingness_to_pay():
    findings, dimensions = verified_fixture()
    findings["source"][0]["additional_excerpts"][0]["text"] = "Scheduling and appointment reminders."
    review = {"competitor_checks": [{"source_id": "0", "product_quote": "Scheduling and appointment reminders."}]}
    v = research_verdict("complete", findings, {}, review)
    assert any("similar offerings" in x for x in v["why"])
    assert not any("people are buying" in x for x in v["why"])
    assert any("does not establish sales or willingness to pay" in x for x in v["why"])


def test_verified_page_mentioning_pricing_topic_without_a_price_stays_unknown():
    findings, dimensions = verified_fixture()
    text = "Annual subscription pricing is not available."
    findings["source"][2]["additional_excerpts"][0]["text"] = text
    dimensions = {"pricing": {"status": "supporting_leads", "support": [
        {"url": "https://example.org/2", "finding": text, "verified": True}]}}
    v = research_verdict("complete", findings, dimensions, None)
    assert v["verdict"] == "test_first"
    assert "pricing" not in " ".join(v["why"])
    assert "pricing" in " ".join(v["why_not"])


def test_shared_discovery_rule_is_in_both_actual_research_prompt_contexts():
    load_company_os_app()
    from app.idea.discovery_guidance import DISCOVERY_GUIDANCE
    from app.research import review, selection
    for system in [module.messages_for({"venture_name": "Discovery QA"}, [])[0]["content"]
                   for module in [review, selection]]:
        assert DISCOVERY_GUIDANCE in system
        assert system.index(DISCOVERY_GUIDANCE) < system.index("Schema:")
        assert "A missing exact feature combination" in system
    assert "anonymized" in DISCOVERY_GUIDANCE
    assert "consenting adult" in DISCOVERY_GUIDANCE
    assert "private WhatsApp/email threads" in DISCOVERY_GUIDANCE



@pytest.mark.parametrize("idea", ["Equipment rental workflow discovery", "Family recipe planning"])
def test_markdown_research_synthesis_includes_same_shared_discovery_rule(monkeypatch, idea):
    import asyncio
    from types import SimpleNamespace
    load_company_os_app()
    from app.idea.discovery_guidance import DISCOVERY_GUIDANCE
    from app.credits import metered
    from app.research import dossier
    sent = []
    async def transmit(messages, **kwargs):
        sent.extend(messages)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Synthetic report " * 40))])
    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.setattr(dossier.sole, "transmit", transmit)
    monkeypatch.setattr(metered, "llm", transmit)
    asyncio.run(dossier._synthesize_markdown(idea, {}, []))
    assert sent[0]["role"] == "system"
    assert DISCOVERY_GUIDANCE in sent[0]["content"]
