"""OT-13: per-jurisdiction G2 geo/legal gate."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.geo_policy import (
    evaluate_legal_send,
    gate_g2,
    policy_for,
)


def test_us_email_without_postal_fails():
    ok, reason = gate_g2(
        geo="US",
        channel="email",
        lawful_basis="none",
        sender_postal_address=None,
    )
    assert ok is False
    assert reason == "us_postal_address_required"


def test_us_email_with_postal_passes_without_consent():
    ok, reason = gate_g2(
        geo="US",
        channel="email",
        lawful_basis="none",
        sender_postal_address="1 Main St, Oakland, CA 94612",
    )
    assert ok is True
    assert reason == "can_spam_opt_out_ok"
    rule = policy_for("US", "email")
    assert rule["framework"] == "CAN-SPAM"
    assert rule["honor_business_days"] == 10
    assert rule["opt_out_keep_live_days"] == 30
    assert rule["physical_address_required"] is True
    assert rule["accurate_headers_required"] is True


def test_de_ca_it_es_email_without_consent_fail():
    for geo in ("DE", "CA", "IT", "ES"):
        ok, reason = gate_g2(
            geo=geo,
            channel="email",
            lawful_basis="none",
            sender_postal_address="1 Main St",
        )
        assert ok is False, geo
        assert reason in {
            "eprivacy_opt_in_required",
            "casl_consent_required",
        }, reason


def test_de_email_with_consent_passes():
    ok, reason = gate_g2(
        geo="DE",
        channel="email",
        lawful_basis="consent",
        sender_postal_address=None,
        consent_state="express",
    )
    assert ok is True
    assert reason == "eprivacy_consent_ok"


def test_us_linkedin_human_clipboard_passes_without_postal():
    ok, reason = gate_g2(
        geo="US",
        channel="linkedin_connect",
        lawful_basis="none",
        sender_postal_address=None,
        send_mode="human_clipboard",
    )
    assert ok is True
    assert reason == "linkedin_human_clipboard_ok"


def test_in_email_without_consent_or_note_fails():
    ok, reason = gate_g2(
        geo="IN",
        channel="email",
        lawful_basis="none",
        sender_postal_address=None,
    )
    assert ok is False
    assert reason == "dpdp_consent_or_legitimate_use_required"


def test_in_email_legitimate_use_note_passes():
    note = "Recipient emailed us first requesting a SOC2 workflow walkthrough."
    assert len(note) >= 40
    ok, reason = gate_g2(
        geo="IN",
        channel="email",
        lawful_basis="legitimate_interest",
        sender_postal_address=None,
        legitimate_use_note=note,
        consent_state="legitimate_use",
    )
    assert ok is True
    assert reason == "dpdp_legitimate_use_ok"


def test_in_public_carveout_is_not_a_send_licence():
    decision = evaluate_legal_send(
        country="IN",
        channel="email",
        consent_state="none",
        lawful_basis="none",
        legitimate_use_note="Made public on LinkedIn so DPDP section 3(c) lets us email.",
    )
    assert decision.allowed is False
    assert decision.reason == "dpdp_public_carveout_not_licence"


def test_casl_mined_directory_fails_implied_consent():
    decision = evaluate_legal_send(
        country="CA",
        channel="email",
        consent_state="implied",
        mined_third_party=True,
    )
    assert decision.allowed is False
    assert decision.reason == "casl_mined_directory_not_implied_consent"


def test_unknown_jurisdiction_email_blocked():
    ok, reason = gate_g2(
        geo="ZZ",
        channel="email",
        lawful_basis="consent",
        sender_postal_address="1 Main St",
    )
    assert ok is False
    assert reason == "unknown_jurisdiction"


def test_unknown_geo_none_email_blocked():
    ok, reason = gate_g2(
        geo=None,
        channel="email",
        lawful_basis="consent",
        sender_postal_address="1 Main St",
    )
    assert ok is False
    assert reason == "unknown_jurisdiction"


def test_fr_corporate_b2b_email_opt_out_ish_without_consent():
    """Member-state split: FR is not DE/IT/ES opt-in. 126 EU fallback is consent-true;
    130 Q2 only blocks CA/DE/IT/ES/IN. Implemented as opt-out-ish corporate B2B.
    """

    ok, reason = gate_g2(
        geo="FR",
        channel="email",
        lawful_basis="none",
        sender_postal_address=None,
    )
    assert ok is True
    assert reason == "eprivacy_corporate_b2b_opt_out_ish"
    rule = policy_for("FR", "email")
    assert rule["art_14_source_disclosure"] is True
    assert rule["art_21_objection"] is True


def test_linkedin_clipboard_allowed_in_opt_in_geo():
    ok, _reason = gate_g2(
        geo="DE",
        channel="linkedin_dm",
        lawful_basis="none",
        sender_postal_address=None,
        send_mode="human_clipboard",
    )
    assert ok is True
