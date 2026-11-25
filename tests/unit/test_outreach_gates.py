"""OT-14/OT-18: hard gates void the score; G2/G3/G5 cannot be overridden."""

from __future__ import annotations

import dataclasses
import inspect

from tests.company_os_harness import load_company_os_app

load_company_os_app()

import pytest
from app.gtm.relationships.gates import (
    NON_OVERRIDABLE_GATES,
    GateInput,
    HumanGateOverride,
    coerce_human_override,
    evaluate_gates,
)
from app.gtm.relationships.score import ScoreInput, score_contact


def _sendable(**kwargs: object) -> GateInput:
    base: dict[str, object] = {
        "role": "founder",
        "icp_roles": ("founder", "ceo"),
        "recipient_geo": "US",
        "channel": "email",
        "send_mode": "human_clipboard",
        "lawful_basis": "none",
        "sender_postal_address": "1 Main St, Oakland, CA 94612",
        "why_this_week": "They posted a hiring complaint this week",
        "why_this_week_confirmed": True,
        "source": "paste",
    }
    base.update(kwargs)
    return GateInput(**base)  # type: ignore[arg-type]


def test_all_gates_pass_is_not_void():
    result = evaluate_gates(_sendable())
    assert result.void is False
    assert all(result.passed.values())


def test_g3_dnc_voids():
    result = evaluate_gates(_sendable(do_not_contact=True))
    assert result.void is True
    assert result.passed["G3"] is False
    assert result.reasons["G3"] == "do_not_contact"
    scored = score_contact(
        ScoreInput(
            role="founder",
            icp_roles=("founder",),
            recipient_geo="US",
            sender_postal_address="1 Main St, Oakland, CA 94612",
            why_this_week="They posted a hiring complaint this week",
            why_this_week_confirmed=True,
            do_not_contact=True,
        )
    )
    assert scored.void is True
    assert scored.sum is None
    assert scored.band.value == "blocked"


def test_g3_override_cannot_send():
    result = evaluate_gates(
        _sendable(do_not_contact=True),
        {
            "g3_bypass": True,
            "override_g3": True,
            "bypass_g3": True,
            "g1_reason": "still want this person badly",
            "g4_reason": "founder confirmed this week",
        },
    )
    assert result.void is True
    assert result.passed["G3"] is False


def test_g2_override_cannot_send_de_email_without_consent():
    result = evaluate_gates(
        _sendable(recipient_geo="DE", lawful_basis="none", sender_postal_address=None),
        {"g2_bypass": True, "override_g2": True, "tenant_allow_de": True},
    )
    assert result.void is True
    assert result.passed["G2"] is False
    assert result.reasons["G2"] == "eprivacy_opt_in_required"


def test_g5_override_cannot_send_linkedin_automation():
    result = evaluate_gates(
        _sendable(channel="linkedin_connect", send_mode="gmail_composio"),
        {"g5_bypass": True, "send_anyway": True},
    )
    assert result.void is True
    assert result.passed["G5"] is False
    assert result.reasons["G5"] == "linkedin_human_clipboard_only"


def test_human_override_type_has_only_g1_and_g4():
    names = {f.name for f in dataclasses.fields(HumanGateOverride)}
    assert names == {"g1_reason", "g4_reason"}
    assert "G2" in NON_OVERRIDABLE_GATES
    assert "G3" in NON_OVERRIDABLE_GATES
    assert "G5" in NON_OVERRIDABLE_GATES
    with pytest.raises(TypeError):
        HumanGateOverride(g2_reason="nope")  # type: ignore[call-arg]


def test_evaluate_gates_signature_has_no_bypass_flags():
    params = inspect.signature(evaluate_gates).parameters
    for banned in (
        "override_g2",
        "override_g3",
        "override_g5",
        "bypass_g2",
        "bypass_g3",
        "bypass_g5",
        "force_send",
        "tenant_override",
    ):
        assert banned not in params
    assert set(params) == {"inp", "override"}


def test_g1_and_g4_may_be_overridden_and_recorded():
    result = evaluate_gates(
        _sendable(
            role="intern",
            why_this_week="",
            why_this_week_confirmed=False,
        ),
        HumanGateOverride(
            g1_reason="named economic buyer of the pain",
            g4_reason="spoke yesterday about the outage",
        ),
    )
    assert result.passed["G1"] is True
    assert result.passed["G4"] is True
    assert result.void is False
    assert [o.gate for o in result.recorded_overrides] == ["G1", "G4"]


def test_g1_override_reason_must_be_at_least_8_chars():
    result = evaluate_gates(
        _sendable(role="intern"),
        HumanGateOverride(g1_reason="nope"),
    )
    assert result.passed["G1"] is False
    assert result.void is True


def test_coerce_strips_non_overridable_keys():
    human = coerce_human_override({"g2_reason": "ignore", "g5_bypass": True, "g1_reason": "buyer yes"})
    assert human.g1_reason == "buyer yes"
    assert not hasattr(human, "g2_reason")
    assert not hasattr(human, "g5_bypass")


def test_harvested_source_voids_even_with_g4_override():
    result = evaluate_gates(
        _sendable(source="web_icp_search"),
        HumanGateOverride(g4_reason="I still want to write them"),
    )
    assert result.harvested is True
    assert result.void is True
    assert result.passed["G4"] is False
    assert result.reasons["G4"] == "harvested_layer_b"
