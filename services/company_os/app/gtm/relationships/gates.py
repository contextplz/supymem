"""Hard gates G1–G5 (doc 130). Any failure voids the score.

G2, G3, and G5 are not tenant-overridable: ``HumanGateOverride`` has no fields
for them, ``evaluate_gates`` accepts no bypass flags, and mapping extras are
ignored rather than honored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Final

from app.gtm.relationships.geo_policy import gate_g2

G1: Final = "G1"
G2: Final = "G2"
G3: Final = "G3"
G4: Final = "G4"
G5: Final = "G5"
GATE_IDS: Final[tuple[str, ...]] = (G1, G2, G3, G4, G5)

NON_OVERRIDABLE_GATES: Final[frozenset[str]] = frozenset({G2, G3, G5})
OVERRIDABLE_GATES: Final[frozenset[str]] = frozenset({G1, G4})

ALLOWED_SEND_MODES: Final[frozenset[str]] = frozenset(
    {"human_clipboard", "gmail_composio", "simulated"}
)
HARVESTED_SOURCES: Final[frozenset[str]] = frozenset(
    {"web_icp_search", "hunter", "openleads", "scrapy", "brightdata", "opengtm"}
)

_OVERRIDE_FIELD_NAMES: Final[frozenset[str]] = frozenset({"g1_reason", "g4_reason"})


@dataclass(frozen=True, slots=True)
class HumanGateOverride:
    """Recorded human override. Only G1 and G4 exist as fields.

    There is no ``g2``, ``g3``, ``g5``, or ``bypass`` attribute. Adding one is a
    type error; ``evaluate_gates`` never reads extra mapping keys for G2/G3/G5.
    """

    g1_reason: str | None = None
    g4_reason: str | None = None


def coerce_human_override(value: HumanGateOverride | Mapping[str, Any] | None) -> HumanGateOverride:
    """Strip any attempted G2/G3/G5 bypass keys from a mapping."""

    if value is None:
        return HumanGateOverride()
    if isinstance(value, HumanGateOverride):
        return value
    g1 = value.get("g1_reason")
    g4 = value.get("g4_reason")
    return HumanGateOverride(
        g1_reason=str(g1) if g1 is not None else None,
        g4_reason=str(g4) if g4 is not None else None,
    )


assert {f.name for f in fields(HumanGateOverride)} == set(_OVERRIDE_FIELD_NAMES)


@dataclass(frozen=True)
class GateInput:
    role: str | None = None
    icp_roles: tuple[str, ...] = ()
    economic_buyer_roles: tuple[str, ...] = ()
    buyer_tag: str | None = None
    recipient_geo: str | None = None
    channel: str = "email"
    send_mode: str = "human_clipboard"
    lawful_basis: str = "none"
    sender_postal_address: str | None = None
    consent_state: str = "none"
    legitimate_use_note: str | None = None
    mined_third_party: bool = False
    do_not_contact: bool = False
    unsubscribed: bool = False
    burned: bool = False
    suppressed: bool = False
    why_this_week: str | None = None
    why_this_week_confirmed: bool = False
    source: str | None = None
    named_human: bool = True


@dataclass(frozen=True)
class RecordedOverride:
    gate: str
    reason: str


@dataclass(frozen=True)
class GateResult:
    """Void means un-sendable. A low score is not this type."""

    void: bool
    passed: dict[str, bool]
    reasons: dict[str, str]
    recorded_overrides: tuple[RecordedOverride, ...]
    harvested: bool


def _norm_role(value: str | None) -> str:
    return (value or "").strip().lower()


def gate_g1(inp: GateInput) -> tuple[bool, str]:
    role = _norm_role(inp.role)
    icp = {_norm_role(r) for r in inp.icp_roles if r}
    buyers = {_norm_role(r) for r in inp.economic_buyer_roles if r}
    if role and role in icp:
        return True, "in_icp_role"
    if role and role in buyers:
        return True, "named_economic_buyer"
    if (inp.buyer_tag or "").strip().lower() == "economic":
        return True, "buyer_tag_economic"
    return False, "not_in_icp_role"


def gate_g3(inp: GateInput) -> tuple[bool, str]:
    if inp.do_not_contact:
        return False, "do_not_contact"
    if inp.unsubscribed:
        return False, "unsubscribed"
    if inp.burned:
        return False, "burned"
    if inp.suppressed:
        return False, "suppressed"
    if inp.consent_state == "opted_out":
        return False, "opted_out"
    return True, "not_suppressed"


def gate_g4(inp: GateInput) -> tuple[bool, str]:
    text = (inp.why_this_week or "").strip()
    if len(text) >= 12 and inp.why_this_week_confirmed:
        return True, "why_this_week_confirmed"
    if len(text) < 12:
        return False, "why_this_week_too_short"
    return False, "why_this_week_unconfirmed"


def gate_g5(*, channel: str, send_mode: str) -> tuple[bool, str]:
    mode = (send_mode or "").strip()
    if mode not in ALLOWED_SEND_MODES:
        return False, "send_mode_not_permitted"
    ch = (channel or "").strip()
    if ch.startswith("linkedin") and mode != "human_clipboard":
        return False, "linkedin_human_clipboard_only"
    return True, "channel_permitted"


def _override_reason_ok(reason: str | None) -> bool:
    return len((reason or "").strip()) >= 8


def evaluate_gates(
    inp: GateInput,
    override: HumanGateOverride | Mapping[str, Any] | None = None,
) -> GateResult:
    """Any failure ⇒ void. ``override`` cannot contain a G2/G3/G5 bypass."""

    recorded: list[RecordedOverride] = []
    human = coerce_human_override(override)
    harvested = (inp.source or "").strip() in HARVESTED_SOURCES

    g1_ok, g1_reason = gate_g1(inp)
    if not g1_ok and _override_reason_ok(human.g1_reason):
        g1_ok = True
        g1_reason = "human_override"
        recorded.append(RecordedOverride(G1, (human.g1_reason or "").strip()))

    g2_ok, g2_reason = gate_g2(
        geo=inp.recipient_geo,
        channel=inp.channel,
        lawful_basis=inp.lawful_basis,
        sender_postal_address=inp.sender_postal_address,
        send_mode=inp.send_mode,
        legitimate_use_note=inp.legitimate_use_note,
        consent_state=inp.consent_state,
        mined_third_party=inp.mined_third_party,
    )

    g3_ok, g3_reason = gate_g3(inp)

    g4_ok, g4_reason = gate_g4(inp)
    if harvested:
        g4_ok = False
        g4_reason = "harvested_layer_b"
    elif not g4_ok and _override_reason_ok(human.g4_reason):
        g4_ok = True
        g4_reason = "human_override"
        recorded.append(RecordedOverride(G4, (human.g4_reason or "").strip()))

    g5_ok, g5_reason = gate_g5(channel=inp.channel, send_mode=inp.send_mode)

    passed = {G1: g1_ok, G2: g2_ok, G3: g3_ok, G4: g4_ok, G5: g5_ok}
    reasons = {G1: g1_reason, G2: g2_reason, G3: g3_reason, G4: g4_reason, G5: g5_reason}
    void = (not all(passed.values())) or harvested
    return GateResult(
        void=void,
        passed=passed,
        reasons=reasons,
        recorded_overrides=tuple(recorded),
        harvested=harvested,
    )
