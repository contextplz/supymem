"""Per-platform rule packs plus the anti-slop and claims gates.

Pure functions, no network and no model call, so the same check runs in CI as
part of the red-team suite and at publish time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_HASHTAG = re.compile(r"(?:^|\s)#\w+")
_NUMBER = re.compile(r"\d[\d,.]*\s*%?|\b\d+x\b", re.IGNORECASE)
_SHOUT = re.compile(r"\b[A-Z]{4,}\b")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]"
)

#: Phrases that make text read as machine-written. Cheap to check, high signal.
SLOP_PHRASES: tuple[str, ...] = (
    "in today's fast-paced",
    "in today's digital",
    "delve into",
    "game-changer",
    "game changer",
    "let that sink in",
    "the harsh truth",
    "here's the thing",
    "it's not just",
    "not only that",
    "buckle up",
    "look no further",
    "unlock the power",
    "unlock your",
    "revolutionize",
    "seamlessly integrate",
    "at the end of the day",
    "needle-moving",
    "supercharge",
    "elevate your",
    "in conclusion",
    "furthermore",
    "moreover",
)

#: Engagement bait. LinkedIn demotes it and readers discount it.
BAIT_PHRASES: tuple[str, ...] = (
    "comment yes",
    "comment below",
    "drop a ",
    "like if you",
    "who else",
    "am i the only one",
    "repost this",
    "share if you",
    "tag someone",
    "dm me",
    "link in comments",
    "link in bio",
    "link in my profile",
    "link in our profile",
)


@dataclass(frozen=True, slots=True)
class LintResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "stats": self.stats,
        }


@dataclass(frozen=True, slots=True)
class PlatformRules:
    max_chars: int
    max_links: int
    max_hashtags: int
    min_chars: int = 80
    #: LinkedIn truncates around here, X shows one line. The hook must land first.
    hook_chars: int = 140


RULES: dict[str, PlatformRules] = {
    # A link in an X post costs 13x, so the engine never puts one in the body.
    "x": PlatformRules(max_chars=280, max_links=0, max_hashtags=1, min_chars=40),
    # A preview card reclassifies a LinkedIn post and roughly halves reach.
    "linkedin": PlatformRules(max_chars=3000, max_links=0, max_hashtags=3),
    "reddit": PlatformRules(max_chars=40000, max_links=1, max_hashtags=0, min_chars=200),
    "bluesky": PlatformRules(max_chars=300, max_links=1, max_hashtags=2, min_chars=40),
    "mastodon": PlatformRules(max_chars=500, max_links=1, max_hashtags=2, min_chars=40),
    "devto": PlatformRules(max_chars=100000, max_links=5, max_hashtags=4, min_chars=400),
}

_DEFAULT_RULES = PlatformRules(max_chars=3000, max_links=1, max_hashtags=2)


def rules_for(platform: str, *, intent: str = "value") -> PlatformRules:
    if (platform or "").lower() == "linkedin" and intent == "promo":
        # One reviewed conversion link is more useful than a linkless promo.
        # The destination is checked against the founder's brief below.
        return PlatformRules(max_chars=3000, max_links=1, max_hashtags=3)
    return RULES.get((platform or "").lower(), _DEFAULT_RULES)


def _claim_corpus(brief: dict[str, Any] | None) -> str:
    if not brief:
        return ""
    parts: list[str] = []
    for claim in brief.get("claims") or []:
        if isinstance(claim, dict):
            parts.append(str(claim.get("text") or ""))
            parts.append(str(claim.get("evidence") or ""))
        else:
            parts.append(str(claim))
    product = brief.get("product") or {}
    if isinstance(product, dict):
        parts.append(str(product.get("one_liner") or ""))
        parts.extend(str(d) for d in product.get("differentiators") or [])
        parts.extend(str(d) for d in product.get("proof") or [])
    for comp in brief.get("competitors") or []:
        if isinstance(comp, dict):
            parts.append(str(comp.get("pricing") or ""))
            parts.append(str(comp.get("positioning") or ""))
    return " ".join(parts).lower()


def _normalize_number(token: str) -> str:
    return token.replace(",", "").replace(" ", "").lower().rstrip(".")


def unregistered_numbers(body: str, brief: dict[str, Any] | None) -> list[str]:
    """Numbers asserted in the body that appear nowhere in the brief.

    This is the anti-hallucination gate: the generator may reason freely but it
    may not invent a statistic.
    """
    corpus = _claim_corpus(brief)
    corpus_numbers = {_normalize_number(m) for m in _NUMBER.findall(corpus)}
    offenders: list[str] = []
    # Digits inside a URL are identifiers, not asserted statistics.
    for raw in _NUMBER.findall(_URL.sub(" ", body or "")):
        token = _normalize_number(raw)
        # Small integers are ordinary prose ("three reasons"), not statistics.
        bare = token.rstrip("%x")
        if bare.isdigit() and len(bare) <= 2 and not token.endswith("%"):
            continue
        if token not in corpus_numbers:
            offenders.append(raw.strip())
    return offenders


_RESEARCH_EXPERIENCE = re.compile(
    r"\b(?:I|we)(?:['’](?:m|re|ve)|\s+(?:am|are|have))?\s+"
    r"(?:(?:been|keep)\s+)?(?:talking|speaking|interviewing|hearing|heard|spoke|talked|interviewed)\b", re.I)


def unregistered_research_experience(body: str, brief: dict[str, Any] | None) -> bool:
    """Known first-person research claims need an exact registered statement.

    Audience hypotheses are not proof that the founder spoke with customers.
    This bounded English-pattern check is not general semantic claim validation.
    """
    evidence = []
    for claim in (brief or {}).get("claims") or []:
        evidence.extend([str(claim.get("text") or ""), str(claim.get("evidence") or "")]
                        if isinstance(claim, dict) else [str(claim)])
    product = (brief or {}).get("product") or {}
    if isinstance(product, dict):
        evidence.extend(str(item) for item in product.get("proof") or [])
    def normalized(value):
        return " ".join(value.replace("’", "'").casefold().split()).strip(" .!?;")
    registered = [normalized(item) for item in evidence]
    for sentence, terminal in re.findall(r"([^.!?;\n]+)([.!?;\n]|$)", body):
        research_claims = list(_RESEARCH_EXPERIENCE.finditer(sentence))
        if (len(research_claims) == 1 and terminal == "?" and re.match(
                r"^\s*(?:have|had|did|am|are|should|could|would|will)\s+(?:I|we)\b", sentence, re.I)):
            continue
        if (len(research_claims) == 1 and re.match(r"^\s*(?:after|once|when|if)\s+(?:I|we)\s+(?:have|had)\s+"
                     r"(?:spoken|talked|interviewed)\b", sentence, re.I)
                and re.search(r"\b(?:I|we)\s+(?:will|would)\b", sentence, re.I)):
            continue  # explicitly contingent future work, not a completed interview claim
        if research_claims:
            statement = normalized(sentence)
            if not statement or statement not in registered:
                return True
    return False


def lint(
    body: str,
    *,
    platform: str,
    intent: str = "value",
    brief: dict[str, Any] | None = None,
    hook: str | None = None,
    subreddit_rules: list[str] | None = None,
) -> LintResult:
    text = (body or "").strip()
    rules = rules_for(platform, intent=intent)
    reasons: list[str] = []
    warnings: list[str] = []

    if not text:
        return LintResult(ok=False, reasons=("empty_body",))

    if len(text) > rules.max_chars:
        reasons.append(f"too_long:{len(text)}>{rules.max_chars}")
    if len(text) < rules.min_chars:
        reasons.append(f"too_short:{len(text)}<{rules.min_chars}")

    links = _URL.findall(text)
    if len(links) > rules.max_links:
        reasons.append(f"too_many_links:{len(links)}>{rules.max_links}")
    if platform == "linkedin" and intent == "promo" and links:
        product = (brief or {}).get("product") or {}
        approved = str(product.get("url") or "").strip() if isinstance(product, dict) else ""
        if (not approved.startswith("https://") or
                any(link.rstrip(".,;:!?)").rstrip("/") != approved.rstrip("/") for link in links)):
            reasons.append("unapproved_conversion_link")

    hashtags = _HASHTAG.findall(text)
    if len(hashtags) > rules.max_hashtags:
        reasons.append(f"too_many_hashtags:{len(hashtags)}>{rules.max_hashtags}")

    lowered = text.lower()
    banned = list(SLOP_PHRASES)
    voice = (brief or {}).get("voice") or {}
    if isinstance(voice, dict):
        banned.extend(str(p).lower() for p in voice.get("banned_phrases") or [])
    hits = sorted({phrase for phrase in banned if phrase and phrase in lowered})
    if hits:
        reasons.append("slop_phrase:" + ",".join(hits[:4]))

    bait = sorted({phrase for phrase in BAIT_PHRASES if phrase in lowered})
    if bait:
        reasons.append("engagement_bait:" + ",".join(bait[:3]))

    if unregistered_research_experience(text, brief):
        reasons.append("unregistered_founder_research")

    offenders = unregistered_numbers(text, brief)
    if offenders:
        reasons.append("unregistered_claim:" + ",".join(offenders[:4]))

    emojis = _EMOJI.findall(text)
    if len(emojis) > 2:
        reasons.append(f"emoji_heavy:{len(emojis)}")

    shouts = [w for w in _SHOUT.findall(text) if w not in {"SaaS", "API", "ICP"}]
    if len(shouts) > 2:
        warnings.append(f"shouting:{len(shouts)}")

    opener = text[: rules.hook_chars]
    if hook and hook.strip() and hook.strip().lower() not in opener.lower():
        warnings.append("hook_not_in_opener")
    if opener.strip().endswith("?") and len(text) < rules.min_chars * 2:
        warnings.append("question_only_opener")

    if intent == "promo" and platform == "reddit":
        reasons.append("promo_on_reddit")

    for rule in subreddit_rules or []:
        needle = str(rule).strip().lower()
        if needle and needle in lowered:
            reasons.append(f"subreddit_rule:{needle[:40]}")

    return LintResult(
        ok=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        stats={
            "chars": len(text),
            "links": len(links),
            "hashtags": len(hashtags),
            "emojis": len(emojis),
        },
    )
