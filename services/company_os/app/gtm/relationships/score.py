"""Additive 0–32 send-priority scorer (OT-14 / OT-15 / OT-18).

Output is a ``SendPriorityBand``, not a meeting probability. Missing evidence
scores 0, not 2. Network distance is non-monotonic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from app.gtm.relationships.gates import (
    GateInput,
    GateResult,
    HumanGateOverride,
    coerce_human_override,
    evaluate_gates,
)

PersonalizeMode = Literal["ok", "skip", "content_play"]

FACTOR_KEYS: tuple[str, ...] = (
    "problem",
    "trigger",
    "quote",
    "cred",
    "network",
    "status",
    "ask",
    "warmth",
)

_SENIORITY_RANK: dict[str, int] = {
    "ic": 0,
    "individual_contributor": 0,
    "manager": 1,
    "director": 2,
    "vp": 3,
    "svp": 3,
    "founder": 3,
    "cofounder": 3,
    "c_level": 4,
    "ciso": 4,
    "cxo": 4,
    "ceo": 4,
    "cto": 4,
    "cfo": 4,
}

_ASK_SCORE: dict[str, int] = {
    "none": 4,
    "connect": 4,
    "value_return": 4,
    "question": 2,
    "call_15": 0,
    "session": 0,
    "paid": 0,
    "demo": 0,
    "calendar": 0,
    "dropped": 0,
}

_WARMTH_SCORE: dict[str, int] = {
    "none": 0,
    "content": 2,
    "commented": 2,
    "reply": 4,
    "intro": 4,
    "inbound": 4,
}


class SendPriorityBand(str, Enum):
    """Ordinal send-priority band. Explicitly not a probability or P(meeting)."""

    BLOCKED = "blocked"
    DROP = "drop"
    WATCH = "watch"
    PURSUE = "pursue"
    PRIORITY = "priority"


class PersonalizeRefused(Exception):
    """Connect/question/value_return drafts must not be generated."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f"personalize_refused:{mode}")


@dataclass(frozen=True)
class ScoreInput:
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
    source: str | None = "paste"
    named_human: bool = True
    company_name: str | None = None
    workflow_evidenced: bool = False
    status_quo_evidenced: bool = False
    trigger_code: str | None = None
    trigger_confidence: str | None = None
    trigger_observed_at: datetime | None = None
    now: datetime | None = None
    quote_text: str | None = None
    quote_source: str | None = None
    sku_key: str | None = None
    sku_status: str | None = None
    i_actually_know_them: bool = False
    mutual_count: int | None = None
    sender_seniority: str | None = None
    recipient_seniority: str | None = None
    ask_rung: str = "connect"
    prior_warmth: str | None = None
    override: HumanGateOverride | None = None


@dataclass(frozen=True)
class ScoreResult:
    void: bool
    sum: int | None
    band: SendPriorityBand
    factors: dict[str, int]
    gates: dict[str, bool]
    gate_reasons: dict[str, str]
    explain: str
    why_now: str
    personalize: PersonalizeMode
    recorded_overrides: tuple[Any, ...] = ()
    harvested: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def network_score(mutual_count: int | None) -> int:
    """Non-monotonic weak-tie buckets. More mutuals is not better."""

    if mutual_count is None:
        return 1
    if mutual_count < 0:
        return 1
    if mutual_count == 0:
        return 1
    if mutual_count <= 5:
        return 4
    if mutual_count <= 15:
        return 3
    if mutual_count <= 49:
        return 2
    if mutual_count <= 199:
        return 1
    return 0


def trigger_score(
    *,
    trigger_code: str | None,
    confidence: str | None,
    observed_at: datetime | None,
    now: datetime,
) -> int:
    """Timing decays with age. Inferred triggers cap at 2."""

    if not (trigger_code or "").strip():
        return 0
    if observed_at is None:
        return 0
    cap = 2 if (confidence or "").strip() != "observed" else 4
    age_days = (now - observed_at).days
    age_days = max(age_days, 0)
    if age_days <= 14:
        return cap
    if age_days <= 45:
        return min(2, cap)
    if age_days <= 90:
        return min(1, cap)
    return 0


def problem_score(
    *,
    role: str | None,
    icp_roles: tuple[str, ...],
    economic_buyer_roles: tuple[str, ...],
    buyer_tag: str | None,
    workflow_evidenced: bool,
    status_quo_evidenced: bool,
) -> int:
    role_n = (role or "").strip().lower()
    icp = {r.strip().lower() for r in icp_roles if r}
    buyers = {r.strip().lower() for r in economic_buyer_roles if r}
    role_ok = bool(role_n) and (role_n in icp or role_n in buyers)
    if not role_ok and (buyer_tag or "").strip().lower() != "economic":
        return 0
    if workflow_evidenced and status_quo_evidenced:
        return 4
    if role_ok:
        return 2
    return 0


def quote_score(*, text: str | None, source: str | None) -> int:
    span = (text or "").strip()
    if not span:
        return 0
    src = (source or "").strip().lower()
    if src == "extracted" and len(span) >= 12:
        return 4
    return 2


def cred_score(
    *,
    sku_key: str | None,
    sku_status: str | None,
    i_actually_know_them: bool,
) -> int:
    if i_actually_know_them:
        return 4
    if (sku_key or "").strip() and (sku_status or "").strip() == "approved":
        return 4
    if (sku_key or "").strip():
        return 2
    return 0


def status_score(*, sender_seniority: str | None, recipient_seniority: str | None) -> int:
    left = _SENIORITY_RANK.get((sender_seniority or "").strip().lower())
    right = _SENIORITY_RANK.get((recipient_seniority or "").strip().lower())
    if left is None or right is None:
        return 0
    if (sender_seniority or "").strip().lower() in {"founder", "cofounder"} and (
        recipient_seniority or ""
    ).strip().lower() in {"founder", "cofounder"}:
        return 4
    if left == right:
        return 4
    delta = abs(left - right)
    if delta >= 3:
        return 0
    if delta == 1:
        return 2
    return 2


def ask_score(ask_rung: str) -> int:
    return _ASK_SCORE.get((ask_rung or "").strip().lower(), 0)


def warmth_score(prior_warmth: str | None) -> int:
    if not prior_warmth:
        return 0
    return _WARMTH_SCORE.get(prior_warmth.strip().lower(), 0)


def _has_extracted_quote(quote: str | None) -> bool:
    return len((quote or "").strip()) >= 12


def _has_observed_trigger(*, trigger_code: str | None, trigger_confidence: str | None) -> bool:
    return bool((trigger_code or "").strip()) and (trigger_confidence or "").strip() == "observed"


def personalize_decision(
    *,
    quote: str | None,
    trigger_code: str | None,
    trigger_confidence: str | None,
    named_human: bool = True,
) -> PersonalizeMode:
    """Refuse auto-personalize unless a verbatim quote or observed trigger exists."""

    if _has_extracted_quote(quote) or _has_observed_trigger(
        trigger_code=trigger_code, trigger_confidence=trigger_confidence
    ):
        return "ok"
    return "skip" if named_human else "content_play"


def draft_connect(
    *,
    quote: str | None,
    trigger_code: str | None,
    trigger_confidence: str | None,
    named_human: bool = True,
) -> dict[str, Any]:
    """Stub used until drafts.py exists. Never interpolates a fake hook."""

    mode = personalize_decision(
        quote=quote,
        trigger_code=trigger_code,
        trigger_confidence=trigger_confidence,
        named_human=named_human,
    )
    if mode != "ok":
        return {"blocked": True, "mode": mode, "body": None, "kind": "connect"}
    return {"blocked": False, "mode": mode, "body": None, "kind": "connect"}


def require_personalize_ok(
    *,
    quote: str | None,
    trigger_code: str | None,
    trigger_confidence: str | None,
    draft_kind: str,
    named_human: bool = True,
) -> PersonalizeMode:
    mode = personalize_decision(
        quote=quote,
        trigger_code=trigger_code,
        trigger_confidence=trigger_confidence,
        named_human=named_human,
    )
    if draft_kind in {"connect", "question", "value_return"} and mode != "ok":
        raise PersonalizeRefused(mode)
    return mode


def band_for_sum(score_sum: int) -> SendPriorityBand:
    if score_sum <= 7:
        return SendPriorityBand.DROP
    if score_sum <= 15:
        return SendPriorityBand.WATCH
    if score_sum <= 23:
        return SendPriorityBand.PURSUE
    return SendPriorityBand.PRIORITY


def _gate_token(passed: bool) -> str:
    return "pass" if passed else "fail"


def _format_explain(
    *,
    gates: GateResult,
    void: bool,
    score_sum: int | None,
    band: SendPriorityBand,
    factors: Mapping[str, int],
    inp: ScoreInput,
    personalize: PersonalizeMode,
    override_label: str,
) -> str:
    trigger_at = "none"
    if inp.trigger_observed_at is not None:
        trigger_at = inp.trigger_observed_at.date().isoformat()
    quote = (inp.quote_text or "").strip()
    quote_bit = quote[:120] if quote else "none"
    mutuals = "unk" if inp.mutual_count is None else str(inp.mutual_count)
    geo = (inp.recipient_geo or "unk").strip() or "unk"
    gaps = [k for k, v in factors.items() if v == 0]
    gap_s = ",".join(gaps) if gaps else "none"
    trig = (inp.trigger_code or "none").strip() or "none"
    sku = (inp.sku_key or "none").strip() or "none"
    role = (inp.role or "").strip() or "none"
    company = (inp.company_name or "").strip() or "none"
    sum_s = "NA" if score_sum is None else str(score_sum)
    return "\n".join(
        [
            (
                f"GATES G1={_gate_token(gates.passed['G1'])} "
                f"G2={_gate_token(gates.passed['G2'])} "
                f"G3={_gate_token(gates.passed['G3'])} "
                f"G4={_gate_token(gates.passed['G4'])} "
                f"G5={_gate_token(gates.passed['G5'])}"
            ),
            f"VOID={1 if void else 0} SUM={sum_s}/32 BAND={band.value} OVERRIDE={override_label}",
            (
                f"F problem={factors['problem']} trigger={factors['trigger']} "
                f"quote={factors['quote']} cred={factors['cred']} "
                f"network={factors['network']} status={factors['status']} "
                f"ask={factors['ask']} warmth={factors['warmth']}"
            ),
            (
                f"WHY_MACHINE role={role} company={company} trigger={trig}@{trigger_at} "
                f"quote={quote_bit} sku={sku} mutuals={mutuals} geo={geo} "
                f"channel={inp.channel}"
            ),
            f"GAPS {gap_s}",
            f"PERSONALIZE {personalize}",
        ]
    )


def _why_now_sentence(
    *,
    void: bool,
    gates: GateResult,
    factors: Mapping[str, int],
    inp: ScoreInput,
) -> str:
    if void:
        failed = [gid for gid, ok in gates.passed.items() if not ok]
        why = ", ".join(failed) if failed else "quarantine"
        return f"Do not send: void because {why}."
    cited = [k for k, v in factors.items() if v >= 3]
    if not cited:
        cited = [k for k, v in factors.items() if v >= 2]
    bits = ", ".join(f"{k}={factors[k]}" for k in cited) if cited else "no strong factors"
    trig = (inp.trigger_code or "").strip() or "no trigger"
    return f"Why this person, why now: {bits}; trigger={trig}."


def _to_gate_input(inp: ScoreInput) -> GateInput:
    return GateInput(
        role=inp.role,
        icp_roles=inp.icp_roles,
        economic_buyer_roles=inp.economic_buyer_roles,
        buyer_tag=inp.buyer_tag,
        recipient_geo=inp.recipient_geo,
        channel=inp.channel,
        send_mode=inp.send_mode,
        lawful_basis=inp.lawful_basis,
        sender_postal_address=inp.sender_postal_address,
        consent_state=inp.consent_state,
        legitimate_use_note=inp.legitimate_use_note,
        mined_third_party=inp.mined_third_party,
        do_not_contact=inp.do_not_contact,
        unsubscribed=inp.unsubscribed,
        burned=inp.burned,
        suppressed=inp.suppressed,
        why_this_week=inp.why_this_week,
        why_this_week_confirmed=inp.why_this_week_confirmed,
        source=inp.source,
        named_human=inp.named_human,
    )


def score_contact(inp: ScoreInput) -> ScoreResult:
    now = inp.now or datetime.now(timezone.utc)
    human = coerce_human_override(inp.override)
    gates = evaluate_gates(_to_gate_input(inp), human)
    factors = {
        "problem": problem_score(
            role=inp.role,
            icp_roles=inp.icp_roles,
            economic_buyer_roles=inp.economic_buyer_roles,
            buyer_tag=inp.buyer_tag,
            workflow_evidenced=inp.workflow_evidenced,
            status_quo_evidenced=inp.status_quo_evidenced,
        ),
        "trigger": trigger_score(
            trigger_code=inp.trigger_code,
            confidence=inp.trigger_confidence,
            observed_at=inp.trigger_observed_at,
            now=now,
        ),
        "quote": quote_score(text=inp.quote_text, source=inp.quote_source),
        "cred": cred_score(
            sku_key=inp.sku_key,
            sku_status=inp.sku_status,
            i_actually_know_them=inp.i_actually_know_them,
        ),
        "network": network_score(inp.mutual_count),
        "status": status_score(
            sender_seniority=inp.sender_seniority,
            recipient_seniority=inp.recipient_seniority,
        ),
        "ask": ask_score(inp.ask_rung),
        "warmth": warmth_score(inp.prior_warmth),
    }
    personalize = personalize_decision(
        quote=inp.quote_text if (inp.quote_source or "").strip().lower() == "extracted" else None,
        trigger_code=inp.trigger_code,
        trigger_confidence=inp.trigger_confidence,
        named_human=inp.named_human,
    )
    if gates.harvested:
        personalize = "skip" if inp.named_human else "content_play"

    void = gates.void
    score_sum: int | None = None if void else sum(factors[k] for k in FACTOR_KEYS)
    band = SendPriorityBand.BLOCKED if void else band_for_sum(score_sum or 0)
    override_label = "none"
    if gates.recorded_overrides:
        override_label = ",".join(o.gate.lower() for o in gates.recorded_overrides)

    explain = _format_explain(
        gates=gates,
        void=void,
        score_sum=score_sum,
        band=band,
        factors=factors,
        inp=inp,
        personalize=personalize,
        override_label=override_label,
    )
    return ScoreResult(
        void=void,
        sum=score_sum,
        band=band,
        factors=factors,
        gates=dict(gates.passed),
        gate_reasons=dict(gates.reasons),
        explain=explain,
        why_now=_why_now_sentence(void=void, gates=gates, factors=factors, inp=inp),
        personalize=personalize,
        recorded_overrides=gates.recorded_overrides,
        harvested=gates.harvested,
    )
