"""Source-linked model judgments are gated without pretending to prove semantics."""
import json

import pytest
from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def context(monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.content import generation_schema as schema
    snapshot = {"authoring_contract": schema.AUTHORING_CONTRACT,
        "brief": {"product": {"name": "Booking concept", "one_liner": "An unbuilt booking concept. Owners would manually review requests."},
                  "icp": {"segments": [{"name": "Studio owners", "pains": ["Avoid scheduling conflicts"]}]}, "claims": [], "voice": {}},
        "topic": {"angle": "Review requests", "icp_segment": "Studio owners"}, "platform": "linkedin"}
    draft = {"body": "The product automatically accepts requests and prevents double-booking.", "title": "", "visual": None}
    return schema, snapshot, draft


def review(checks):
    return {"score": .99, "verdict": "publish", "problems": [], "unsupported_claims": [],
            "reads_as_ai": False, "claims_reviewed": True, "claim_checks": checks}


def check(quote, status="unsupported", evidence=None):
    return {"draft_quote": quote, "status": status, "evidence": evidence or [],
            "reason": "The brief describes an unbuilt concept with manual review, not this automation or guarantee."}


@pytest.mark.parametrize("platform", ["linkedin", "x", "reddit", "bluesky", "mastodon", "devto"])
def test_writer_and_critic_share_exact_text_only_platform_contract(context, platform):
    schema, snapshot, draft = context
    snapshot["platform"] = platform
    writer = json.loads(schema.messages("writer", snapshot)[1]["content"])
    critic = json.loads(schema.messages("critic", snapshot, draft=draft)[1]["content"])
    assert writer["editorial_contract"] == critic["editorial_contract"]
    assert critic["editorial_contract"]["publication_mode"] == "text_only"
    assert critic["editorial_contract"]["title"].startswith("required_nonempty" if platform == "reddit" else "must_be_empty")
    assert critic["editorial_contract"]["visual"].startswith("must_be_null")
    assert "Do not penalize" in critic["task"]
    assert writer["grounding_rules"] == critic["grounding_rules"]
    assert "sources" in critic and "claim_checks" in critic["schema"]["required"]


def test_promotional_linkedin_contract_allows_owned_url_without_invented_anecdotes(context):
    schema, snapshot, draft = context
    snapshot["brief"]["product"]["url"] = "https://booking.example/"
    snapshot["topic"]["intent"] = "promo"
    writer = json.loads(schema.messages("writer", snapshot)[1]["content"])
    critic = json.loads(schema.messages("critic", snapshot, draft=draft)[1]["content"])
    assert writer["editorial_contract"] == critic["editorial_contract"]
    assert writer["hard_limits"]["max_links"] == 1
    assert writer["editorial_contract"]["approved_conversion_url"] == "https://booking.example/"
    assert any("Never invent conversations" in rule for rule in writer["grounding_rules"])


def test_model_reported_unsupported_or_uncertain_product_claims_cannot_pass_even_with_high_score(context):
    schema, snapshot, draft = context
    for status in ["unsupported", "uncertain"]:
        value = schema.parse("critic", json.dumps(review([check(draft["body"], status)])), snapshot, draft=draft)
        assert not schema.critic_passes(value, snapshot)
        assert "Remove or qualify" in schema.grounding_feedback(value, snapshot)[0]
    missing_review = review([])
    missing_review["claims_reviewed"] = False
    assert not schema.critic_passes(missing_review, snapshot)


def test_supported_claims_need_real_source_identifier_excerpt_and_exact_draft_span(context):
    schema, snapshot, draft = context
    cases = [
        (check(draft["body"], "supported"), "evidence_required"),
        (check(draft["body"], "supported", [{"source_id": "invented", "quote": "Unprovided proof"}]), "evidence_mismatch"),
        (check(draft["body"], "supported", [{"source_id": "product", "quote": "Automatically accepts requests"}]), "evidence_mismatch"),
        (check("Text not present in this draft"), "quote_mismatch"),
    ]
    for item, error in cases:
        with pytest.raises(ValueError, match=error):
            schema.parse("critic", json.dumps(review([item])), snapshot, draft=draft)


def test_invalid_critic_reference_can_only_feed_a_repair(context):
    schema, snapshot, draft = context
    cases = [
        check(draft["body"], "supported"),
        check(draft["body"], "supported", [{"source_id": "product", "quote": "Invented proof"}]),
        check("Paraphrase absent from the draft"),
    ]
    for item in cases:
        value = schema.parse_critic_for_repair(json.dumps(review([item])), snapshot, draft=draft)
        assert value["verdict"] == "revise"
        assert value["claims_reviewed"] is False
        assert value["unsupported_claims"]
        assert value["claim_checks"][0]["status"] == "uncertain"
        assert value["claim_checks"][0]["evidence"] == []
        assert not schema.critic_passes(value, snapshot)
        assert schema.grounding_feedback(value, snapshot)

    missing_contract = review([])
    missing_contract.pop("claim_checks")
    with pytest.raises(ValueError):
        schema.parse_critic_for_repair(json.dumps(missing_contract), snapshot, draft=draft)


def test_oversized_critic_explanations_are_clipped_without_loosening_evidence(context):
    schema, snapshot, draft = context
    checks = [check(draft["body"], "supported", [
        {"source_id": "product", "quote": "An unbuilt booking concept."}
    ]) for _ in range(11)]
    for index in (5, 7, 10):
        checks[index]["reason"] = "Verbose explanation. " * 30
    value = schema.parse_critic_for_repair(json.dumps(review(checks)), snapshot, draft=draft)
    assert len(value["claim_checks"]) == 11
    assert all(len(item["reason"]) <= 300 for item in value["claim_checks"])

    missing = review(checks)
    missing.pop("score")
    with pytest.raises(ValueError):
        schema.parse_critic_for_repair(json.dumps(missing), snapshot, draft=draft)


def test_honest_proposal_and_literal_supported_statement_preserve_stage_and_limitations(context):
    schema, snapshot, _ = context
    draft = {"body": "An unbuilt booking concept. Owners would manually review requests.", "title": "", "visual": None}
    item = check(draft["body"], "supported", [{"source_id": "product", "quote": draft["body"]}])
    value = schema.parse("critic", json.dumps(review([item])), snapshot, draft=draft)
    assert schema.critic_passes(value, snapshot)
    proposal = {**draft, "body": "Could a future version help owners review requests?"}
    value = schema.parse("critic", json.dumps(review([check(proposal["body"], "proposal")])), snapshot, draft=proposal)
    assert schema.critic_passes(value, snapshot)
    # This checks reported judgments and literal provenance, not semantic truth.
    assert "not prove semantic entailment" in schema.source_text.__doc__


def test_new_contract_rejects_missing_coverage_and_wrong_title_while_legacy_receipts_keep_schema(context):
    schema, snapshot, draft = context
    legacy = {key: value for key, value in review([]).items() if key not in {"claims_reviewed", "claim_checks"}}
    with pytest.raises(ValueError):
        schema.parse("critic", json.dumps(legacy), snapshot, draft=draft)
    old = {key: value for key, value in snapshot.items() if key != "authoring_contract"}
    assert schema.parse("critic", json.dumps(legacy), old, draft=draft) == legacy
    with pytest.raises(ValueError, match="title_contract"):
        schema.parse("writer", json.dumps({**draft, "title": "Not a LinkedIn field"}), snapshot)
    assert schema.parse("writer", json.dumps(draft), snapshot)["title"] == ""
    with pytest.raises(ValueError, match="title_contract"):
        schema.parse("writer", json.dumps(draft), {**snapshot, "platform": "reddit"})


def test_metered_critic_receives_full_contract_and_validates_actual_draft(context, monkeypatch):
    schema, snapshot, draft = context
    from app.content import generation
    from app.research import metered
    monkeypatch.setattr(generation, "mutate", lambda *args: None)
    observed = []
    def capture(run_id, payload, **kwargs):
        observed.append(kwargs)
        prompt = json.loads(kwargs["messages"][1]["content"])
        assert prompt["draft"] == draft
        assert prompt["editorial_contract"]["title"].startswith("must_be_empty")
        assert prompt["editorial_contract"]["visual"].startswith("must_be_null")
        assert prompt["schema"]["$defs"]["SourceExcerpt"]["properties"]["source_id"]["enum"] == ["product", "icp.segments.0"]
        assert kwargs["purpose"] == "content.critic" and kwargs["require_stop"] is True
        assert kwargs["cap_at_reservation"] is True and kwargs["provider_timeout_seconds"] == 240
        return kwargs["parse"](json.dumps(review([check(draft["body"])])))
    monkeypatch.setattr(metered, "synthesize", capture)
    value = generation._call("run-fixture", {}, "critic", "critic_1", snapshot, draft=draft)
    assert len(observed) == 1 and not schema.critic_passes(value, snapshot)


def test_unknown_contract_cannot_downgrade_to_legacy_grounding(context):
    schema, snapshot, draft = context
    with pytest.raises(ValueError, match="authoring_contract_unsupported"):
        schema.parse("critic", json.dumps(review([])), {**snapshot, "authoring_contract": "unknown"}, draft=draft)


def test_overlong_critic_fields_are_clipped_and_never_approve_a_missing_quote(context):
    schema, snapshot, draft = context
    item = check(draft["body"], "unsupported")
    item["reason"] = "Verbose explanation. " * 60
    value = schema.parse("critic", json.dumps({**review([item]), "problems": ["p " * 400] * 9}), snapshot, draft=draft)
    assert len(value["claim_checks"][0]["reason"]) < 300 and len(value["problems"]) == 8
    assert not schema.critic_passes(value, snapshot)
    # a clipped quote can no longer match the draft: fails closed into the repair loop
    item = check(draft["body"] + " " + "x" * 3000, "supported")
    draft = {**draft, "body": item["draft_quote"]}
    repaired = schema.parse_critic_for_repair(json.dumps(review([item])), snapshot, draft=draft)
    assert repaired["verdict"] == "revise" and not schema.critic_passes(repaired, snapshot)
