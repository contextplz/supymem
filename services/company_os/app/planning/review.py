"""Narrow, deterministic checks for contradictions with trusted planning inputs.

These checks do not establish truth or replace founder review. They catch specific
unsupported assertions observed in generated revisions while retaining the draft
for correction, rather than spending credits on an automatic regeneration.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

_PROOF = re.compile(r"\b(?:is|are|was|were|has been|have been)\s+(?:independently\s+)?(?:validated|proven|verified)\b|\b(?:a|the|this)\s+(?:validated|proven|verified)\s+(?:playbook|case|approach)\b", re.I)
_UNTESTED = re.compile(r"\bwaitlist\b[^.!?\n]{0,120}\b(?:has not been tested|has never been tested|is untested|is unverified|is not verified|remains untested)\b", re.I)
_SYNTHETIC_LEAD = re.compile(r"\b(?:waitlist|lead)\s+(?:dashboard|count)[^.!?\n]{0,70}\bshows?\b[^.!?\n]{0,50}\b(?:test|synthetic)\s+(?:submission|entry|lead)\b|\btest entry appears in\b[^.!?\n]{0,50}\bwaitlist dashboard\b", re.I)
_PRICE = re.compile(r"(?<!\w)(?:[$€£₹]\s*|(?:USD|EUR|GBP|INR)\s*)\d[\d,]*(?:\.\d{1,2})?\b", re.I)
_OUTREACH_SIGNAL = re.compile(r"\b(?:outreach|contact(?:ing)?|email(?:ing)?|messag(?:e|ing)|campaign|reach(?:ing)? out)\b", re.I)
_AUDIENCE_TARGET = re.compile(
    r"\b(?:at least\s+)?(\d+)\s*[- ]\s*(?:new\s+)?(?:independent\s+)?"
    r"(clinic|customer|prospect|lead|buyer|company|business|user)s?\b", re.I,
)
_NO_PUBLIC_APP = re.compile(
    r"\b(?:no|without|not\s+(?:a|an|the))\s+public(?:[- ]facing)?\s+(?:web\s+)?"
    r"(?:app|application|site|website)\b"
    r"|\b(?:will|do)\s+not\s+(?:deploy|publish|launch|build)\s+(?:a\s+)?"
    r"public(?:[- ]facing)?\s+(?:web\s+)?(?:app|application|site|website)\b", re.I,
)


def _text(value, path: str = "proposal") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _text(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _text(child, f"{path}[{index}]")


_PRICE_FIELDS = ("summary", "goal_first_paid_user", "assumptions", "exclusions", "milestones")
_TEST_PRICE = "a test price you choose"


def _norm(match) -> str:
    return match.group().replace(" ", "").replace(",", "").lower()


def _known_prices(context: dict) -> set[str]:
    """Amounts the founder confirmed, plus amounts in Supymem's own saved test proposals.

    The latter are test targets to try, not confirmed prices, so plans may repeat them.
    """
    idea = context.get("idea") if isinstance(context.get("idea"), dict) else {}
    brief = context.get("gstack_brief") if isinstance(context.get("gstack_brief"), dict) else {}
    review = context.get("research_review") if isinstance(context.get("research_review"), dict) else {}
    authorized = " ".join(text for _, text in _text({
        "idea": idea, "founder_instruction": context.get("founder_instruction") or "",
        "tests": [brief.get("proposed_tests"), brief.get("risks"), review.get("proposed_tests")],
    }))
    return {_norm(m) for m in _PRICE.finditer(authorized)}


def repair_prices(proposal: dict | None, context: dict) -> int:
    """Rewrite unconfirmed exact prices in place so founders are never blocked; returns count."""
    if not proposal:
        return 0
    known, count = _known_prices(context), 0

    def fix(match):
        nonlocal count
        if _norm(match) in known:
            return match.group()
        count += 1
        return _TEST_PRICE

    def walk(value):
        if isinstance(value, str):
            return _PRICE.sub(fix, value)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    for key in _PRICE_FIELDS:
        if key in proposal:
            proposal[key] = walk(proposal[key])
    return count


def findings(proposal: dict | None, context: dict) -> list[dict]:
    if not proposal:
        return []
    result: list[dict] = []
    def flag(code: str, path: str, message: str):
        result.append({"code": code, "path": path, "message": message})
    # Workbook confidence is not independent verification, including when a
    # previous generated plan carried the same unsupported wording.
    for path, text in _text(proposal.get("precedent_applications", []), "proposal.precedent_applications"):
        if _PROOF.search(text):
            flag("precedent_verification_claim", path,
                 "This case interpretation claims validation or proof that the supplied workbook does not establish. Correct the interpretation before approval.")
    observed = context.get("managed_conversion_observation") or {}
    # A confirmed idea and explicit founder instruction may establish a target;
    # model-authored research/assumptions may not. These checks preserve the
    # draft for human correction rather than silently rewriting strategy.
    idea = context.get("idea") if isinstance(context.get("idea"), dict) else {}
    authorized = " ".join(text for _, text in _text({
        "idea": idea, "founder_instruction": context.get("founder_instruction") or "",
    }))
    known_prices = _known_prices(context)
    known_outreach = {(match.group(1), match.group(2).lower())
                      for match in _AUDIENCE_TARGET.finditer(authorized)}
    landing_build = any(milestone.get("kind") == "landing" and milestone.get("included", True)
                        for milestone in proposal.get("milestones", []) if isinstance(milestone, dict))
    proposed_work = {key: proposal[key] for key in
                     ("summary", "goal_first_paid_user", "assumptions", "exclusions", "milestones") if key in proposal}
    for path, text in _text(proposed_work, "proposal"):
        if landing_build and _NO_PUBLIC_APP.search(text):
            flag("publication_scope_conflict", path,
                 "This draft rules out a public app or site while also proposing a public landing build. Clarify the product and publication scope before approval.")
        if any(_norm(match) not in known_prices
               for match in _PRICE.finditer(text)):
            flag("unconfirmed_price", path,
                 "This step names a price you have not chosen. Edit it to say you will pick a test price.")
        if any((target.group(1), target.group(2).lower()) not in known_outreach
               for sentence in re.split(r"[.!?\n]", text) if _OUTREACH_SIGNAL.search(sentence)
               for target in _AUDIENCE_TARGET.finditer(sentence)):
            flag("unconfirmed_outreach_target", path,
                 "This plan sets a numeric outreach target absent from the confirmed idea or founder instruction. Propose a scoped experiment for separate founder approval instead.")
    for path, text in _text(proposal):
        if observed.get("submission_verified") is True and observed.get("verified_at") and _UNTESTED.search(text):
            flag("conversion_snapshot_conflict", path,
                 "This draft says the waitlist was not tested, but its saved input contains a successful submission receipt. Preserve that dated evidence and require a current check before publication.")
        if context.get("managed_conversion", {}).get("kind") == "venture_waitlist" and _SYNTHETIC_LEAD.search(text):
            flag("synthetic_lead_claim", path,
                 "The built-in waitlist check records a verification receipt and excludes synthetic entries from real lead counts. Require the verified submission/storage result, not a test lead in the dashboard.")
    return result
