"""Rejected clarification receipts disclose structure, never provider content."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def agent():
    load_company_os_app(reset=False)
    from app.idea import agent as module
    return module


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"private":"founder secret",', "json_syntax"),
        (json.dumps({"private":"founder secret", "action":"execute_shell"}), "schema"),
    ],
)
def test_rejected_output_diagnostic_redacts_values_and_unknown_keys(agent, content, expected):
    with pytest.raises(ValueError) as failure:
        agent.parse_result(content, force_propose=False)
    result = agent.rejected_output_diagnostics(content, SimpleNamespace(finish_reason="stop"), failure.value)
    assert result["validation_kind"] == expected
    assert result["finish_reason"] == "stop"
    assert result["content_chars"] == len(content)
    assert result["schema_issue_count"] >= 1
    assert "founder secret" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    assert "execute_shell" not in json.dumps(result)


def test_rejected_output_diagnostic_uses_closed_rules_and_finish_reasons(agent):
    result = agent.rejected_output_diagnostics("private", SimpleNamespace(finish_reason="private finish"),
                                         ValueError("private failure"))
    assert result == {"content_chars": 7, "finish_reason": "unknown", "validation_kind": "contract"}
    limit = agent.rejected_output_diagnostics("x" * 50001, SimpleNamespace(finish_reason="length"),
                                        ValueError("provider output exceeds limit"))
    assert limit == {"content_chars": 50001, "finish_reason": "length", "validation_kind": "contract",
                     "validation_rule": "output_limit"}


def _turn(labels):
    return json.dumps({
        "case": "know", "action": "question", "message": "ok", "understanding": {}, "confidence": 0.1,
        "question": {"text": "Who pays first?", "field": "customer", "why": "w",
                     "options": ["Independent clinics that book by phone", "Gyms with drop-in classes"],
                     "option_labels": labels},
    })


def test_option_labels_kept_when_short_and_aligned(agent):
    q = agent.parse_result(_turn(["Phone clinics", "Drop-in gyms"]), force_propose=False).question
    assert q.option_labels == ["Phone clinics", "Drop-in gyms"]
    assert q.options[0].startswith("Independent clinics")


@pytest.mark.parametrize("labels", [["Phone clinics"], ["A very long label indeed yes", "Gyms"], ["Same", "same"], []])
def test_bad_option_labels_dropped_not_rejected(agent, labels):
    assert agent.parse_result(_turn(labels), force_propose=False).question.option_labels == []


@pytest.mark.parametrize("audience,options", [
    ("Repair shop operators", ["Record overdue inspections", "Assign maintenance visits"]),
    ("Astronomy hobbyists", ["Plan a viewing route", "Record observation notes"]),
])
def test_provider_recommendation_mismatch_does_not_lose_a_valid_turn(agent, audience, options):
    data = json.loads(_turn([]))
    data["understanding"] = {"customer": audience}
    data["question"].update(text=f"Which task would you try first for {audience}?", field="workflow",
        options=options, recommended_option="An option not in the list", recommendation_reason="Unsupported recommendation")
    parsed = agent.parse_result(json.dumps(data), force_propose=False)
    assert parsed.understanding.customer == audience and parsed.question.options == options
    assert parsed.question.recommended_option in options
    assert parsed.question.recommendation_reason != "Unsupported recommendation"
    data["question"]["field"] = "send_email"
    with pytest.raises(ValueError):
        agent.parse_result(json.dumps(data), force_propose=False)
