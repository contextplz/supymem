"""Clarification selections retain multiple compatible answers without accepting invalid IDs."""

import pytest
from fastapi import HTTPException

from tests.company_os_harness import load_company_os_app
from tests.security.test_connector_oauth_boundary import client as client


def test_product_substitutes_have_explicit_multiple_choice_contract():
    load_company_os_app()
    from app.clarify.dilemmas import dilemmas_for_idea

    questions = dilemmas_for_idea("A tool to help founders find customers")
    substitute = next(q for q in questions if q["slot_key"] == "product_today")
    assert substitute["selection_mode"] == "multiple"
    assert substitute["exclusive_option_ids"] == ["opt_c"]
    assert all(q["selection_mode"] == "single" for q in questions if q is not substitute)


def test_multiple_answers_are_preserved_and_exclusive_unknown_is_rejected():
    load_company_os_app()
    from app.api.clarify import AnswerItem, _normalized_answer

    q = {
        "selection_mode": "multiple",
        "exclusive_option_ids": ["unknown"],
        "options": [
            {"id": "manual", "label": "Manual workflow"},
            {"id": "paid", "label": "Paid product"},
            {"id": "unknown", "label": "I do not know"},
        ],
    }
    assert _normalized_answer(AnswerItem(question_id="q", option_ids=["manual", "paid"]), q) == (
        "Manual workflow · Paid product", "Manual workflow · Paid product"
    )
    for ids in (["unknown", "paid"], ["paid", "paid"], ["bogus"]):
        with pytest.raises(HTTPException) as exc:
            _normalized_answer(AnswerItem(question_id="q", option_ids=ids), q)
        assert exc.value.status_code == 422


def test_single_answer_keeps_existing_option_id_contract():
    load_company_os_app()
    from app.api.clarify import AnswerItem, _normalized_answer

    q = {"selection_mode": "single", "options": [{"id": "a", "label": "A"}]}
    assert _normalized_answer(AnswerItem(question_id="q", option_id="a"), q) == ("a", "A")
    with pytest.raises(HTTPException):
        _normalized_answer(AnswerItem(question_id="q", option_ids=["a", "a"]), q)


def test_suggested_answers_cannot_become_plan_evidence_or_product_facts():
    load_company_os_app()
    from app.gstack.plan import infer_from_office_hours
    from app.clarify.dilemmas import brief_from_answers
    note = "Founder selected this suggested option; treat as an untested assumption"
    metadata = {"provenance": "suggested_choice", "assumption_note": note, "founder_text": ""}
    answers = {"goal_primary": "opt_a", "product_buyer": "VP Portfolio Management at a lender",
               "_answer_provenance": {"goal_primary": metadata, "product_buyer": metadata}}
    plan = infer_from_office_hours(idea="Lending workflow", answers=answers)
    assert plan["verdict"] == "research_first"
    assert plan["assumptions"][0]["note"] == note
    brief = brief_from_answers(idea="Lending workflow", answers=answers)
    assert brief["first_user"] == "Untested assumption: VP Portfolio Management at a lender"


@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.asyncio
async def test_quiz_answer_storage_keeps_option_ids_and_provenance(client, monkeypatch, selected):
    from app.api import clarify
    from app.state import MEM
    from tests.security.test_durable_journey_boundary import venture
    vid, _, _ = venture(client)
    monkeypatch.setattr(clarify, "active_run", lambda _: None)
    monkeypatch.setattr(clarify, "_pause_reason", lambda _: "awaiting_clarification")
    monkeypatch.setattr(clarify, "MEM", MEM)
    MEM[f"questions:{vid}"] = [{"id": "q_demand", "slot_key": "goal_primary",
        "options": [{"id": "opt_a", "label": "Someone pays already"}]}]
    payload = {"question_id": "q_demand", **({"option_id": "opt_a"} if selected else {"other_text": "Nobody pays yet"})}
    response = await clarify.post_clarification_answers(vid, clarify.ClarificationAnswers(answers=[clarify.AnswerItem(**payload)]))
    assert response["status"] == "accepted"
    saved = MEM[f"answers:{vid}"]
    assert saved["q_demand"] == ("opt_a" if selected else "Nobody pays yet")
    assert saved["_answer_provenance"]["goal_primary"]["provenance"] == ("suggested_choice" if selected else "founder_stated")
