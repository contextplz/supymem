"""Clarification questions preserve old saved output while guiding new choices."""

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "company_os"))
from app.idea.agent import Question, SYSTEM, response_schema


def test_legacy_question_defaults_to_one_choice():
    question = Question.model_validate({
        "text": "Which path?", "field": "first_user_path", "why": "To plan acquisition",
        "options": ["Search", "Referrals"],
    })
    assert question.selection_mode == "single"


def test_multiple_choice_is_bounded_and_in_provider_schema():
    question = Question.model_validate({
        "text": "Which channels?", "field": "first_user_path", "why": "To plan acquisition",
        "options": ["Search", "Referrals"], "selection_mode": "multiple",
    })
    assert question.selection_mode == "multiple"
    provider_question = response_schema()["$defs"]["Question"]
    assert "selection_mode" in provider_question["required"]
    assert provider_question["additionalProperties"] is False
    assert response_schema()["additionalProperties"] is False
    with pytest.raises(ValidationError):
        Question.model_validate({**question.model_dump(), "selection_mode": "unknown"})


def test_recommendation_is_valid_and_old_questions_receive_a_default():
    question = Question.model_validate({
        "text": "Which path?", "field": "first_user_path", "why": "To plan acquisition",
        "options": ["Search", "Referrals"], "recommended_option": "Referrals",
        "recommendation_reason": "You can reach these people already.",
    })
    assert question.recommended_option == "Referrals"
    legacy = Question.model_validate({"text": "Which path?", "field": "first_user_path", "why": "To plan acquisition", "options": ["Search", "Referrals"]})
    assert legacy.recommended_option == "Search" and legacy.recommendation_reason
    for field in ("recommended_option", "recommendation_reason"):
        assert field in response_schema()["$defs"]["Question"]["required"]
    with pytest.raises(ValidationError):
        Question.model_validate({**question.model_dump(), "recommended_option": "Invented option"})


@pytest.mark.parametrize("text,options,expected", [
    ("Have you talked to shop owners / did anyone promise to pay?",
     ["I've had a couple of owners promise to pay verbally", "I haven't talked to any shop owners yet", "I'm not sure"],
     "I haven't talked to any shop owners yet"),
    ("Which best describes your progress?", ["I'm not sure", "I've interviewed shop owners"], "I'm not sure"),
    ("What evidence do you have?", ["No evidence yet", "I've seen owners abandon checkout"], "No evidence yet"),
    ("What evidence do you have?", ["I've talked to owners who haven't paid yet", "I'm not sure"], "I'm not sure"),
    ("Have you talked to shop owners?", ["I haven't spoken to everyone, but two owners promised to pay", "I don't know"], "I don't know"),
    ("What evidence do you have?", ["I haven't interviewed owners", "I have no direct evidence yet"], "I have no direct evidence yet"),
    ("Have you heard from any owners?", ["I've spoken to three owners", "They promised to pay"], ""),
    ("How much time do clinics lose to no-shows?",
     ["I know most systems send reminders, but I haven't quantified the time clinics spend chasing no-shows",
      "A front desk manager told me they spend an hour a day texting reminders",
      "I have an anonymised log from a clinic manager showing they lose about 3 billable hours a week"], ""),
    ("How much time do clinics lose to no-shows?",
     ["A front desk manager told me they spend an hour a day", "I haven't measured this yet"], "I haven't measured this yet"),
    ("Which channel should we test first?", ["Search", "Referrals", "I'm not sure"], "Referrals"),
    ("Which feature should we build today?", ["Checkout", "Calendar", "I'm not sure"], "Calendar"),
    ("What do we build first?", ["I'm not sure", "Checkout"], "Checkout"),
])
def test_past_evidence_recommendations_are_conservative(text, options, expected):
    question = Question.model_validate({
        "text": text, "field": "customer", "why": "Find the first test", "options": options,
        "recommended_option": options[1] if text.startswith(("Which", "What do we")) else options[0],
        "recommendation_reason": "Use this conservative assumption until you have evidence.",
    })
    assert question.recommended_option == expected
    if expected and text not in ("Which channel should we test first?", "Which feature should we build today?", "What do we build first?"):
        assert question.recommendation_reason == "Pick this if you have not checked yet."  # plain copy, 2026-10-24
    if not expected:
        assert question.recommendation_reason == ""


def test_prompt_requires_no_evidence_defaults_for_founder_history():
    assert "always include and recommend" in SYSTEM
    assert "Never recommend an option claiming past evidence" in SYSTEM


def test_prompt_keeps_assumed_picks_out_of_founder_facts():
    assert 'Answers starting with "Assumed: " are Supymem working assumptions, NOT founder evidence' in SYSTEM
    assert "Put the assumed option in understanding.assumptions" in SYSTEM
    assert "summary, problem, customer or any other understanding field" in SYSTEM
    assert "customers, payments or traction from an assumed option" in SYSTEM
    assert "only that extra text is the founder's own words" in SYSTEM


def test_evidence_recommendations_use_saved_conversations_but_not_negative_testimony():
    from app.idea.agent import safe_recommendation
    options = ["No one yet", "A few people"]
    facts = {"evidence": {"value": "Two clinic owners I spoke to said empty slots cost them revenue."}}
    assert safe_recommendation("Who have you talked to?", options, facts=facts)[0] == "A few people"
    facts["evidence"]["value"] = "I have not talked to anyone yet."
    assert safe_recommendation("Who have you talked to?", options, facts=facts)[0] == "No one yet"


@pytest.mark.parametrize("question,expected", [
    ("Who have you talked to? e.g. VP Portfolio Management at a lender", ["No one yet", "A few people"]),
    ("How many paying customers do you have?", ["No customers yet", "A few customers"]),
    ("What evidence do you have?", ["No evidence yet", "Some evidence to describe"]),
])
def test_evidence_options_never_offer_fabricated_people_or_customer_counts(question, expected):
    from app.idea.agent import fitting_options
    from app.gstack.office_hours import parse_picks
    from app.intake.smart import IntakeResult
    options = ["VP Portfolio Management at ExampleBank", "We have 10 paying customers"]
    assert fitting_options(question, options) == expected
    picks = parse_picks(json.dumps({"questions": [
        {"id": "Q1", "question": question, "options": options},
        {"id": "Q4", "question": "Which first workflow should we test?", "options": ["Order form", "Calendar"]},
    ]}))
    assert picks[0]["options"] == expected
    intake = IntakeResult(path="clarify", confidence=0.5, founder_line=question.split("?")[0] + "?",
                          button_label="Continue", choices=options)
    assert intake.choices == expected


def test_answer_provenance_preserves_typed_text_and_legacy_choice_assumptions():
    from app.idea.agent import answer_metadata, core_answer_status
    assert answer_metadata("Assumed: Lending VP\nI have not talked to anyone yet") == {
        "provenance": "suggested_choice", "suggested_text": "Lending VP",
        "founder_text": "I have not talked to anyone yet",
        "assumption_note": "Founder selected this suggested option; treat as an untested assumption",
    }
    assert answer_metadata("Lending VP")["provenance"] == "founder_stated"
    assert answer_metadata("Lending VP", ["Lending VP", "Bank CEO"])["provenance"] == "suggested_choice"
    intake = answer_metadata("A lending tool\nAudience: Assumed: Lending VP\nNobody interviewed yet")
    assert intake["provenance"] == "suggested_choice"
    assert intake["suggested_text"] == "Lending VP"
    assert intake["founder_text"] == "A lending tool\nNobody interviewed yet"
    assert core_answer_status("Assumed: Lending VP", 0, field="customer") == (True, True)


def test_review_prompts_keep_suggestions_out_of_founder_evidence():
    from app.idea.agent import SUGGESTED_CHOICE_NOTE
    from app.idea.validation_agent import SYSTEM as validation_system
    from app.research.review import SYSTEM as research_system
    from app.research.selection import SYSTEM as selection_system
    for prompt in (SYSTEM, validation_system, research_system, selection_system):
        assert SUGGESTED_CHOICE_NOTE in prompt
        assert "suggested_choice" in prompt


def test_saved_choice_corrections_keep_provenance_and_assumption():
    from app.idea.service import _record_answer_correction
    structured = {}
    _record_answer_correction(structured, "q_customer", "Assumed: Lending VP\nNo one interviewed yet")
    assert structured["_answer_corrections"]["q_customer"] == "Assumed: Lending VP\nNo one interviewed yet"
    assert structured["_answer_provenance"]["q_customer"]["provenance"] == "suggested_choice"
    assert "untested assumption: Lending VP" in structured["assumptions"][0]
    _record_answer_correction(structured, "q_customer", "An interviewed bakery owner")
    assert structured["_answer_provenance"]["q_customer"]["provenance"] == "founder_stated"


def test_core_field_never_fills_from_a_legacy_choice_or_mixed_assumption():
    from app.idea.agent import Clarification, Understanding, answer_metadata
    from app.idea.runner import _core_clarification
    for answer, metadata, expected in [
        ("Lending VP", answer_metadata("Lending VP", ["Lending VP", "Bank CEO"]), ""),
        ("Assumed: Lending VP\nIndependent tutors", answer_metadata("Assumed: Lending VP\nIndependent tutors"), "Independent tutors"),
    ]:
        result = Clarification(case="know", action="proposal", message="Saved", understanding=Understanding())
        snapshot = {"active_core_field": "customer", "question_fields_asked": ["customer", "offer"],
                    "conversation": [{"role": "founder", "text": answer, **metadata}]}
        updated = _core_clarification(snapshot, result)
        assert updated.understanding.customer == expected
