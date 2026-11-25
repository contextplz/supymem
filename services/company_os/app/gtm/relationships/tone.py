"""Tone / product-name linter for relationship drafts (doc 115 §6)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LintResult:
    ok: bool
    reasons: tuple[str, ...]

    @property
    def refused(self) -> bool:
        return not self.ok


_WORD = re.compile(r"\S+")


def lint_draft(
    body: str,
    *,
    hook: str | None,
    step: str,
    product_name: str | None = None,
    max_words: int = 90,
) -> LintResult:
    """Refuse drafts that violate relationship-first rules.

    - Steps 0–2 must not mention the product name.
    - Every draft must include a non-generic dossier hook.
    - Cap length at ``max_words`` for early steps.
    """
    reasons: list[str] = []
    text = (body or "").strip()
    if not text:
        reasons.append("empty_body")

    hook_s = (hook or "").strip()
    if not hook_s or hook_s.lower() in {"nice", "congrats", "great work", "hello"}:
        reasons.append("missing_dossier_hook")
    elif hook_s.lower() not in text.lower() and len(hook_s) > 8:
        # Soft: hook detail should appear; allow short paraphrases by checking tokens.
        tokens = [t for t in re.findall(r"[A-Za-z0-9]{4,}", hook_s.lower()) if t]
        if tokens and not any(t in text.lower() for t in tokens[:3]):
            reasons.append("hook_not_referenced")

    words = _WORD.findall(text)
    if step in {"congrats", "nudge", "value", "0", "1", "2"} and len(words) > max_words:
        reasons.append(f"too_long:{len(words)}")

    product = (product_name or "").strip()
    if product and step in {"congrats", "nudge", "value", "0", "1", "2"}:
        if re.search(re.escape(product), text, re.I):
            reasons.append("product_name_before_offer")

    # No links in step 0 / congrats.
    if step in {"congrats", "0"} and re.search(r"https?://", text, re.I):
        reasons.append("link_in_congrats")

    return LintResult(ok=not reasons, reasons=tuple(reasons))
