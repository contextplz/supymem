"""Review findings preserve drafts and distinguish observations from model claims."""
import pytest
from tests.unit.planning.test_author import author as author

@pytest.fixture
def review(author):
    from app.planning.review import findings
    return findings

@pytest.mark.parametrize("text", ["The playbook is validated for B2B.", "This is a proven playbook.", "This case has been independently verified."])
def test_unverified_workbook_cannot_become_proof(review, text):
    findings = review({"precedent_applications": [{"record_id": "case", "important_difference": text}]}, {})
    assert findings[0]["code"] == "precedent_verification_claim"

@pytest.mark.parametrize("text", ["This playbook has not been validated.", "The case is not independently verified.", "The workbook reports a result, but the context differs."])
def test_qualified_analogy_is_retained(review, text):
    assert review({"precedent_applications": [{"important_difference": text}]}, {}) == []

def test_successful_receipt_conflicts_with_never_tested_claim_only_when_present(review):
    proposal = {"evidence_gaps": ["Managed waitlist destination has not been tested to receive submissions."]}
    assert review(proposal, {}) == []
    context = {"managed_conversion_observation": {"submission_verified": True, "verified_at": "2026-09-19T10:49:02Z"}}
    findings = review(proposal, context)
    assert findings[0]["code"] == "conversion_snapshot_conflict"
    assert proposal["evidence_gaps"]  # Do not silently rewrite the founder's draft.
    assert review({"evidence_gaps": ["Recheck current waitlist verification before publication."]}, context) == []

@pytest.mark.parametrize("text", ["The waitlist dashboard shows one test submission.", "The waitlist dashboard shows the synthetic test entry.", "Confirm the test entry appears in the gateway waitlist dashboard."])
def test_synthetic_probe_must_not_be_presented_as_lead(review, text):
    result = review({"milestones": [{"acceptance": [text]}]}, {"managed_conversion": {"kind": "venture_waitlist"}})
    assert result[0]["code"] == "synthetic_lead_claim"

def test_receipt_check_and_unknown_provider_health_are_valid(review):
    assert review({"evidence_gaps": ["Managed publication provider health is unverified."],
                   "milestones": [{"acceptance": ["Website shows a dated submission/storage verification receipt; synthetic checks are excluded from leads."]}]},
                  {"managed_conversion": {"kind": "venture_waitlist"}, "managed_publication": {"configured": True}}) == []


def test_shift_handover_plan_flags_invented_price_and_outreach_target(review):
    context = {"idea": {"offer": "AI assistant turning deidentified clinic shift notes into a handover checklist.",
                        "desired_outcome": "Action items and critical follow-ups, one line per patient.",
                        "pricing_hypothesis": ""},
               "founder_instruction": "Research and plan only; do not build, deploy or contact people."}
    proposal = {"summary": "Build a handover checklist assistant.",
                "milestones": [{"tasks": [{"description": "Start a Stripe $999/year subscription."},
                                         {"description": "Run 10-clinic multi-thread outreach."}]}]}
    result = review(proposal, context)
    assert {item["code"] for item in result} == {"unconfirmed_price", "unconfirmed_outreach_target"}
    assert {item["path"] for item in result} == {
        "proposal.milestones[0].tasks[0].description", "proposal.milestones[0].tasks[1].description"}


def test_confirmed_targets_and_tentative_experiments_are_reviewable(review):
    context = {"idea": {"pricing_hypothesis": "Founder wants to test $99 per year.",
                        "first_user_path": "Founder proposed contacting 3 clinic managers."}}
    proposal = {"summary": "Test $99 per year with founder review.",
                "milestones": [{"description": "Contact 3 clinic managers after separate approval."}]}
    assert review(proposal, context) == []
    assert review({"milestones": [{"description": "Propose a small opt-in clinic interview, then ask the founder."}]},
                  {"idea": {"pricing_hypothesis": ""}}) == []


def test_research_price_does_not_authorize_founder_plan_price(review):
    context = {"idea": {"pricing_hypothesis": ""},
               "sources": [{"summary": "A rival advertises $999 annually."}]}
    result = review({"goal_first_paid_user": "Charge $999 annually."}, context)
    assert [item["code"] for item in result] == ["unconfirmed_price"]


def test_publication_scope_conflict_is_flagged_without_banning_private_previews(review):
    proposal = {"summary": "No public web application is planned.",
                "milestones": [{"kind": "landing", "tasks": [{"capability_id": "build.run"}]}]}
    assert [item["code"] for item in review(proposal, {})] == ["publication_scope_conflict"]
    proposal["summary"] = "Keep the core application private while testing a public landing page."
    assert review(proposal, {}) == []


def test_saved_clinic_plan_wording_is_review_blocked_before_approval(review):
    context = {"idea": {"offer": "AI assistant turning deidentified clinic shift notes into a handover checklist.",
                        "desired_outcome": "Action items and critical follow-ups, one line per patient.",
                        "pricing_hypothesis": "", "first_user_path": "A clinic manager may review a prototype."},
               "founder_instruction": "Research and planning only; no build, deployment or contact."}
    proposal = {"summary": "Handover checklist assistant",
                "exclusions": ["We will not deploy a public-facing web application"],
                "milestones": [
                    {"kind": "landing", "included": True, "tasks": [{"capability_id": "build.run"}]},
                    {"kind": "monetisation", "tasks": [{"description": "Test a Stripe subscription, e.g., $999/year"}]},
                    {"kind": "acquisition", "description": "Execute multichannel outreach to 10 new independent clinics",
                     "acceptance": ["Outreach is attempted to at least 10 clinics"]},
                ]}
    result = review(proposal, context)
    assert {(item["code"], item["path"]) for item in result} == {
        ("publication_scope_conflict", "proposal.exclusions[0]"),
        ("unconfirmed_price", "proposal.milestones[1].tasks[0].description"),
        ("unconfirmed_outreach_target", "proposal.milestones[2].description"),
        ("unconfirmed_outreach_target", "proposal.milestones[2].acceptance[0]"),
    }


def test_saved_test_prices_allowed_and_others_repaired():
    from app.planning.review import findings, repair_prices

    ctx = {"idea": {}, "gstack_brief": {"proposed_tests": [{"hypothesis": "Try $50/month with 5 clinics"}]}}
    plan = {"summary": "Test $50/month, then $99/month.", "milestones": [{"description": "Offer at $20/month"}]}
    assert {f["path"] for f in findings(plan, ctx)} == {"proposal.summary", "proposal.milestones[0].description"}
    assert repair_prices(plan, ctx) == 2
    assert plan["summary"] == "Test $50/month, then a test price you choose/month."
    assert plan["milestones"][0]["description"] == "Offer at a test price you choose/month"
    assert findings(plan, ctx) == []
