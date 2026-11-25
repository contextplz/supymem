"""Scan a surface (agents / ai_proxy / chat) and return a verdict."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from guardrails.config import action_for, resolve_mode
from guardrails.injection import detect_injection
from guardrails.pii import BLOCK_ON_OUTPUT, detect_pii, redact_pii
from guardrails.secrets import detect_secrets, redact_secrets
from guardrails.toxicity import detect_toxicity

Severity = Literal["allow", "flag", "block"]


@dataclass
class GuardVerdict:
    action: Severity
    reasons: list[str] = field(default_factory=list)
    redacted: str | None = None
    hits: dict[str, Any] = field(default_factory=dict)
    mode: str = "deterministic_only"


def scan(
    text: str,
    *,
    surface: str = "agents",
    direction: str = "input",
) -> GuardVerdict:
    mode = resolve_mode()
    reasons: list[str] = []
    hits: dict[str, Any] = {}
    action: Severity = "allow"
    working = text or ""

    inj_action = action_for(surface, "injection")
    if inj_action != "off":
        inj = detect_injection(working)
        if inj:
            hits["injection"] = [h.pattern for h in inj]
            reasons.append("injection")
            action = _raise(action, inj_action)

    pii_action = action_for(surface, "pii")
    if pii_action != "off":
        pii = detect_pii(working)
        if pii:
            hits["pii"] = [h.kind for h in pii]
            reasons.append("pii")
            if direction == "output" and any(h.kind in BLOCK_ON_OUTPUT for h in pii):
                action = "block"
            else:
                action = _raise(action, pii_action)
            if pii_action == "redact":
                working = redact_pii(working)

    sec_action = action_for(surface, "secrets")
    if sec_action != "off":
        secrets = detect_secrets(working)
        if secrets:
            hits["secrets"] = [h.kind for h in secrets]
            reasons.append("secrets")
            action = _raise(action, sec_action)
            working = redact_secrets(working)

    tox_action = action_for(surface, "toxicity")
    if tox_action != "off":
        tox = detect_toxicity(working)
        hits["toxicity"] = {"score": tox.score, "skipped": tox.skipped}
        if not tox.skipped and tox.score >= 0.9:
            reasons.append("toxicity")
            action = _raise(action, tox_action)

    return GuardVerdict(
        action=action,
        reasons=reasons,
        redacted=working if working != (text or "") else None,
        hits=hits,
        mode=mode,
    )


def _raise(current: Severity, configured: str) -> Severity:
    if configured == "block":
        return "block"
    if configured in {"flag", "redact"} and current == "allow":
        return "flag"
    return current
