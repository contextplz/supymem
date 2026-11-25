"""Case know vs explore (docs/supymem/10 §2). Deterministic; LLM optional later."""

from __future__ import annotations

import re

_EXPLORE = re.compile(
    r"\b(not sure|help me|no idea|explore|brainstorm|what should i build|give me ideas)\b",
    re.I,
)
_CONSUMER = re.compile(
    r"\b(?:B2C|consumers?|individuals?|people|parents?|families|children|kids|enthusiasts?|households?|"
    r"hobbyists?|home users?|students?|residents?|homeowners?|renters?|travell?ers?|gamers?|patients?|"
    r"shoppers?|runners?|couples?|first[- ]time\s+(?!managers?\b|employers?\b)\w+|"
    r"(?:dogs?|cats?|pets?|pupp(?:y|ies)|horses?|homes?|houses?|cars?|vehicles?|e?bikes?|bicycles?|boats?|computers?|phones?)\s+owners?|"
    r"owners? of\s+(?:an?\s+)?(?:dogs?|cats?|pets?|pupp(?:y|ies)|horses?|homes?|houses?|cars?|vehicles?|e?bikes?|bicycles?|boats?|computers?|phones?))\b",
    re.I,
)


def is_consumer(audience: str | dict, context: dict | None = None) -> bool:
    context = audience if isinstance(audience, dict) else context or {}
    who = str(context.get("who") or context.get("customer") or context.get("audience") or "") if isinstance(audience, dict) else audience
    people_type = False
    for key in ("customer_type", "market_type", "audience_type", "business_model", "monetization"):
        value = context.get(key) or ""
        value = value.get("value") or "" if isinstance(value, dict) else value
        if not people_type and re.search(r"\b(?:B2B|business[- ]to[- ]business)\b", str(value), re.I):
            return False
        if re.search(r"\b(?:B2C|business[- ]to[- ]consumer|consumers?|individuals?)\b", str(value), re.I):
            return True
        if not people_type and re.search(r"\b(?:business(?:es)?|compan(?:y|ies)|teams?|firms?|clinics?|agencies)\b", str(value), re.I):
            return False
        if key in {"customer_type", "market_type", "audience_type"}:
            if re.search(r"\bpersonal\b", str(value), re.I):
                return True
            people_type = people_type or bool(re.search(r"\bpeople\b", str(value), re.I))
    idea = " ".join(str(context.get(key) or "") for key in ("summary", "idea", "raw_idea", "offer", "solution"))
    who = who or idea
    if not people_type and re.search(r"\b(?:business(?:es)?|compan(?:y|ies)|startups?|teams?|founders?|firms?|clinics?|agencies|agency|"
                 r"practices?|stores?|shops?|restaurants?|baker(?:y|ies)|freelanc\w*|(?:roles?|employees?) at|B2B)\b", who, re.I):
        return False
    # shortcut: English role titles and personal-use cues, use typed customer fields for ambiguous or localized audiences.
    personal_use = bool(re.search(r"\b(?:personal|households?|hobb(?:y|ies))\b", who + " " + idea, re.I))
    role_audience = re.sub(r"^\s*(?:people|individuals)\s+(?:(?:who|that)\s+)?(?:are|work(?:ing)? as)\s+", "", who, flags=re.I)
    role_audience = re.split(r"\b(?:who|that|which|whose|with|for|to|by|from|as|seeking|needing|awaiting|seeing|visiting|consulting)\b", role_audience, maxsplit=1, flags=re.I)[0]
    if (re.search(r"\b(?:directors?|managers?|officers?|executives?|administrators?|coordinators?|engineers?|analysts?|accountants?)\b", role_audience, re.I)
            and all(consumer_match.group(0).lower() in {"patient", "people"} for consumer_match in _CONSUMER.finditer(role_audience))
            and not personal_use):
        return False
    if people_type or _CONSUMER.search(who):
        return True
    pricing = " ".join(str(context.get(key) or "") for key in ("pricing", "pricing_hypothesis", "business_model", "monetization"))
    if re.search(r"\b(?:B2B|business(?:es)?|compan(?:y|ies)|teams?|firms?|clinics?|agencies)\b", idea, re.I):
        return False
    return bool(re.search(r"\bapp\b", idea, re.I) and (personal_use or re.search(r"\$\s*\d+(?:\.\d+)?\s*/\s*(?:mo(?:nth)?)(?:\b)|\bfree\b.*\bpremium\b", pricing + " " + idea, re.I)))


def classify_case(text: str) -> dict[str, object]:
    raw = (text or "").strip()
    lowered = raw.lower()
    explore_hit = bool(_EXPLORE.search(lowered))
    words = len(raw.split())
    if explore_hit or words < 6:
        case = "explore"
        confidence = 0.85 if explore_hit else 0.45
    else:
        case = "know"
        confidence = 0.7 if words >= 12 else 0.55
    sector = "general"
    if any(k in lowered for k in ("dental", "clinic", "health")):
        sector = "health"
    elif any(k in lowered for k in ("invoice", "freelancer", "saas")):
        sector = "b2b_saas"
    elif any(k in lowered for k in ("marketplace", "buyers", "sellers")):
        sector = "marketplace"
    return {
        "case": case,
        "confidence": confidence,
        "sector_guess": sector,
        "needs_chip": confidence < 0.6,
    }
