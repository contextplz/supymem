"""Recipient-jurisdiction send policy (OT-13 / P0-02).

Rules live as data. Unknown geos default to BLOCKED. LinkedIn human-clipboard
is a software-gate pass across geos (platform ToS still bind the human).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

CHANNEL_EMAIL: Final = "email"
LINKEDIN_CHANNELS: Final[frozenset[str]] = frozenset(
    {"linkedin_connect", "linkedin_dm", "linkedin_comment"}
)

# ePrivacy Art. 13(5) member-state split: person-identified work email ≈ opt-in.
OPT_IN_EEA: Final[frozenset[str]] = frozenset({"DE", "IT", "ES"})

# ISO-3166-1 alpha-2 for EU/EEA (+ GB/UK PECR). Not a licence to email.
EEA_GEOS: Final[frozenset[str]] = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IS",
        "IE",
        "IT",
        "LV",
        "LI",
        "LT",
        "LU",
        "MT",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
        "GB",
        "UK",
    }
)

_PUBLIC_CARVEOUT_MARKERS: Final[tuple[str, ...]] = (
    "made public",
    "made_public",
    "public linkedin",
    "public profile",
    "scraped",
    "directory harvest",
    "section 3(c)",
    "s.3(c)",
    "§3(c)",
)


@dataclass(frozen=True)
class SendPolicyRow:
    """In-memory policy row. Persistence is owned by a parallel worker."""

    geo: str
    channel: str
    rule: Mapping[str, Any]
    workspace_id: str | None = None


@dataclass(frozen=True)
class GeoDecision:
    """Allowed/blocked for a recipient geo + channel. Not a risk score."""

    allowed: bool
    reason: str
    framework: str | None
    obligations: tuple[str, ...] = ()
    rule: Mapping[str, Any] | None = None


def _us_email_rule() -> dict[str, Any]:
    return {
        "framework": "CAN-SPAM",
        "consent_required": False,
        "opt_out_permitted": True,
        "opt_out_required": True,
        "accurate_headers_required": True,
        "physical_address_required": True,
        "honor_business_days": 10,
        "opt_out_keep_live_days": 30,
        "identify_as_advertisement": True,
    }


def _casl_email_rule() -> dict[str, Any]:
    return {
        "framework": "CASL",
        "consent_required": True,
        "implied_consent_ok": True,
        "mined_third_party_implied_fails": True,
        "opt_out_required": True,
    }


def _eprivacy_opt_in_email_rule() -> dict[str, Any]:
    return {
        "framework": "ePrivacy",
        "consent_required": True,
        "member_state_opt_in": True,
        "art_14_source_disclosure": True,
        "art_21_objection": True,
    }


def _eprivacy_b2b_opt_out_ish_email_rule() -> dict[str, Any]:
    return {
        "framework": "ePrivacy",
        "consent_required": False,
        "corporate_b2b_opt_out_ish": True,
        "opt_out_required": True,
        "art_14_source_disclosure": True,
        "art_21_objection": True,
    }


def _dpdp_email_rule() -> dict[str, Any]:
    return {
        "framework": "DPDP",
        "consent_required": True,
        "legitimate_use_note_required": True,
        "made_public_is_not_send_licence": True,
        "min_legitimate_use_note_chars": 40,
    }


def _linkedin_tos_rule() -> dict[str, Any]:
    return {
        "framework": "linkedin_tos",
        "automation": False,
        "human_send_only": True,
        "software_gate": "human_clipboard_ok",
    }


def _blocked_unknown_rule() -> dict[str, Any]:
    return {
        "framework": None,
        "consent_required": True,
        "default": "blocked",
        "reason": "unknown_jurisdiction",
    }


def default_send_policy_rows(*, workspace_id: str | None = None) -> tuple[SendPolicyRow, ...]:
    """Global rails. Tenants may only tighten these, never loosen G2."""

    rows: list[SendPolicyRow] = [
        SendPolicyRow("US", CHANNEL_EMAIL, _us_email_rule(), workspace_id),
        SendPolicyRow("CA", CHANNEL_EMAIL, _casl_email_rule(), workspace_id),
        SendPolicyRow("DE", CHANNEL_EMAIL, _eprivacy_opt_in_email_rule(), workspace_id),
        SendPolicyRow("IT", CHANNEL_EMAIL, _eprivacy_opt_in_email_rule(), workspace_id),
        SendPolicyRow("ES", CHANNEL_EMAIL, _eprivacy_opt_in_email_rule(), workspace_id),
        SendPolicyRow("EU", CHANNEL_EMAIL, _eprivacy_opt_in_email_rule(), workspace_id),
        SendPolicyRow("IN", CHANNEL_EMAIL, _dpdp_email_rule(), workspace_id),
    ]
    for ch in sorted(LINKEDIN_CHANNELS):
        rows.append(SendPolicyRow("*", ch, _linkedin_tos_rule(), workspace_id))
    for geo in sorted(EEA_GEOS - OPT_IN_EEA):
        rows.append(
            SendPolicyRow(geo, CHANNEL_EMAIL, _eprivacy_b2b_opt_out_ish_email_rule(), workspace_id)
        )
    return tuple(rows)


_POLICY_INDEX: dict[tuple[str, str], Mapping[str, Any]] = {
    (row.geo, row.channel): row.rule for row in default_send_policy_rows()
}


def seed_default_send_policies(
    sink: list[SendPolicyRow] | None = None,
    *,
    workspace_id: str | None = None,
) -> list[SendPolicyRow]:
    """Pure seed. Does not take a DB session (persistence is a parallel worker)."""

    rows = list(default_send_policy_rows(workspace_id=workspace_id))
    if sink is not None:
        sink.extend(rows)
    return rows


def normalize_geo(geo: str | None) -> str | None:
    if geo is None:
        return None
    code = geo.strip().upper()
    if not code:
        return None
    if code == "UK":
        return "GB"
    return code


def _linkedin_channel(channel: str) -> bool:
    return channel in LINKEDIN_CHANNELS or channel.startswith("linkedin")


def policy_for(geo: str | None, channel: str) -> dict[str, Any]:
    """Return the rule dict for (geo, channel). Unknown email geos are blocked."""

    g = normalize_geo(geo)
    if _linkedin_channel(channel):
        li = channel if channel in LINKEDIN_CHANNELS else "linkedin_connect"
        return dict(_POLICY_INDEX.get(("*", li), _linkedin_tos_rule()))
    if g is None:
        return dict(_blocked_unknown_rule())
    exact = _POLICY_INDEX.get((g, channel))
    if exact is not None:
        return dict(exact)
    if g in EEA_GEOS:
        if g in OPT_IN_EEA:
            return _eprivacy_opt_in_email_rule()
        return _eprivacy_b2b_opt_out_ish_email_rule()
    return dict(_blocked_unknown_rule())


def _note_is_public_carveout(note: str | None) -> bool:
    text = (note or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _PUBLIC_CARVEOUT_MARKERS)


def _has_express_consent(lawful_basis: str, consent_state: str) -> bool:
    if consent_state in {"express", "consent"}:
        return True
    return lawful_basis == "consent"


def evaluate_legal_send(
    *,
    country: str | None,
    channel: str,
    consent_state: str = "none",
    lawful_basis: str = "none",
    sender_postal_address: str | None = None,
    send_mode: str | None = None,
    legitimate_use_note: str | None = None,
    mined_third_party: bool = False,
) -> GeoDecision:
    """Given recipient country, channel, and consent state: allowed or blocked."""

    rule = policy_for(country, channel)
    framework = rule.get("framework")
    obligations: list[str] = []

    if _linkedin_channel(channel):
        mode = send_mode or "human_clipboard"
        if mode != "human_clipboard":
            return GeoDecision(
                False,
                "linkedin_human_clipboard_only",
                "linkedin_tos",
                ("human_send_only",),
                rule,
            )
        return GeoDecision(
            True,
            "linkedin_human_clipboard_ok",
            "linkedin_tos",
            ("human_send_only", "automation_forbidden"),
            rule,
        )

    if framework is None or rule.get("default") == "blocked":
        return GeoDecision(False, "unknown_jurisdiction", None, ("default_blocked",), rule)

    if framework == "CAN-SPAM":
        obligations.extend(
            [
                "accurate_headers",
                "physical_postal_address",
                "opt_out_honor_10_business_days",
                "opt_out_keep_live_30_days",
            ]
        )
        postal = (sender_postal_address or "").strip()
        if not postal:
            return GeoDecision(False, "us_postal_address_required", framework, tuple(obligations), rule)
        if consent_state == "opted_out":
            return GeoDecision(False, "opted_out", framework, tuple(obligations), rule)
        return GeoDecision(True, "can_spam_opt_out_ok", framework, tuple(obligations), rule)

    if framework == "CASL":
        obligations.append("consent_first")
        if mined_third_party and consent_state != "express" and lawful_basis != "consent":
            return GeoDecision(
                False,
                "casl_mined_directory_not_implied_consent",
                framework,
                tuple(obligations),
                rule,
            )
        if not _has_express_consent(lawful_basis, consent_state):
            if consent_state == "implied" and not mined_third_party:
                return GeoDecision(
                    True,
                    "casl_implied_consent_ok",
                    framework,
                    tuple(obligations),
                    rule,
                )
            return GeoDecision(False, "casl_consent_required", framework, tuple(obligations), rule)
        return GeoDecision(True, "casl_consent_ok", framework, tuple(obligations), rule)

    if framework == "ePrivacy":
        obligations.extend(["art_14_source_disclosure", "art_21_objection"])
        if rule.get("consent_required"):
            if not _has_express_consent(lawful_basis, consent_state):
                return GeoDecision(
                    False,
                    "eprivacy_opt_in_required",
                    framework,
                    tuple(obligations),
                    rule,
                )
            return GeoDecision(True, "eprivacy_consent_ok", framework, tuple(obligations), rule)
        if consent_state == "opted_out":
            return GeoDecision(False, "art_21_objected", framework, tuple(obligations), rule)
        return GeoDecision(
            True,
            "eprivacy_corporate_b2b_opt_out_ish",
            framework,
            tuple(obligations),
            rule,
        )

    if framework == "DPDP":
        obligations.extend(["consent_or_listed_legitimate_use", "made_public_not_send_licence"])
        if _note_is_public_carveout(legitimate_use_note):
            return GeoDecision(
                False,
                "dpdp_public_carveout_not_licence",
                framework,
                tuple(obligations),
                rule,
            )
        if _has_express_consent(lawful_basis, consent_state):
            return GeoDecision(True, "dpdp_consent_ok", framework, tuple(obligations), rule)
        note = (legitimate_use_note or "").strip()
        min_chars = int(rule.get("min_legitimate_use_note_chars") or 40)
        legitimate = consent_state == "legitimate_use" or lawful_basis == "legitimate_interest"
        if legitimate and len(note) >= min_chars:
            return GeoDecision(
                True,
                "dpdp_legitimate_use_ok",
                framework,
                tuple(obligations),
                rule,
            )
        if len(note) >= min_chars and lawful_basis != "none":
            return GeoDecision(True, "dpdp_legitimate_use_ok", framework, tuple(obligations), rule)
        return GeoDecision(
            False,
            "dpdp_consent_or_legitimate_use_required",
            framework,
            tuple(obligations),
            rule,
        )

    return GeoDecision(False, "unknown_jurisdiction", framework, (), rule)


def gate_g2(
    *,
    geo: str | None,
    channel: str,
    lawful_basis: str,
    sender_postal_address: str | None,
    send_mode: str | None = None,
    legitimate_use_note: str | None = None,
    consent_state: str = "none",
    mined_third_party: bool = False,
) -> tuple[bool, str]:
    """G2: recipient geography / legal send allowed for this channel. Not overridable."""

    decision = evaluate_legal_send(
        country=geo,
        channel=channel,
        consent_state=consent_state,
        lawful_basis=lawful_basis,
        sender_postal_address=sender_postal_address,
        send_mode=send_mode,
        legitimate_use_note=legitimate_use_note,
        mined_third_party=mined_third_party,
    )
    return decision.allowed, decision.reason
