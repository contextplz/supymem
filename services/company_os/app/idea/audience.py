"""Ground a model's customer extraction in the founder's own words."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_BUYER_CLAUSE = re.compile(r"\b(?:sold|sell(?:s|ing)?|market(?:ed|ing)?|offer(?:ed|ing)?)\b[^.!?;\n:]{0,100}?\bto\s+"
    r"([^.!?;\n,]+)", re.I)


class Audience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=300)
    kind: Literal["person", "organisation", "unknown"]
    quote: str = Field(max_length=2000)

    @model_validator(mode="after")
    def private_values(self):
        from app.secrets.detect import looks_like_secret

        if looks_like_secret(self.text) or looks_like_secret(self.quote):
            self.text, self.kind, self.quote = "", "unknown", ""
        return self


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .,:;!?")


def safe_audience(text: str, *, inferred: bool = True) -> str:
    from app.intake.smart import audience_phrase
    from app.secrets.detect import looks_like_secret

    if not isinstance(text, str) or looks_like_secret(text):
        return ""
    return audience_phrase(text, inferred=inferred)


def stated_buyer(source: str) -> str:
    from app.intake.smart import audience_phrase
    from app.secrets.detect import looks_like_secret

    for match in _BUYER_CLAUSE.finditer(source):
        phrase = re.split(r"\s+(?:to|that|which|so|because|who|and)\b", match[1], maxsplit=1, flags=re.I)[0]
        if (buyer := audience_phrase(phrase)) and not looks_like_secret(buyer):
            return buyer
    return ""


def validated_audience(value: dict | Audience | None, sources: list[str]) -> str:
    from app.secrets.detect import looks_like_secret

    if isinstance(value, BaseModel):
        value = value.model_dump()
    if not isinstance(value, dict) or value.get("kind") not in {"person", "organisation"}:
        return ""
    text, quote = value.get("text", ""), value.get("quote", "")
    if not isinstance(text, str) or looks_like_secret(text):
        return ""
    clauses = [_normalized(match[1]) for source in sources for match in _BUYER_CLAUSE.finditer(source)]
    buyer_match = any(re.match(rf"{re.escape(_normalized(text))}(?!\w)", clause) for clause in clauses) if text else False
    audience = safe_audience(text)
    if not audience and buyer_match:
        audience = text
    if not audience or _normalized(audience) != _normalized(text) or not isinstance(quote, str) or not quote or looks_like_secret(quote):
        return ""
    # shortcut: only literal English audience spans are trusted, upgrade with grounded entailment for paraphrases or other languages.
    if not re.search(rf"(?<!\w){re.escape(_normalized(text))}(?!\w)", _normalized(quote)) or not any(
            re.search(rf"(?<!\w){re.escape(_normalized(quote))}(?!\w)", _normalized(source)) for source in sources):
        return ""
    return "" if clauses and not buyer_match else audience


def local_audience(snapshot: dict, candidate: str = "", *, extracted: dict | None = None) -> str:
    from app.chat.idea_proposal import EDIT_QUESTION
    from app.gstack.office_hours import is_skip
    from app.idea.agent import is_unsure_answer
    from app.idea.runner import _founder_messages

    snapshot = {**snapshot, "original_idea": snapshot.get("original_idea", snapshot.get("idea", "")),
        "answered_fields": snapshot.get("answered_fields", snapshot.get("_answered_fields", {}))}
    sources = _founder_messages(snapshot)
    saved = snapshot["answered_fields"].get("customer") or snapshot["answered_fields"].get("audience")
    fact = (snapshot.get("founder_facts") or {}).get("audience", {})
    understanding = extracted or snapshot.get("understanding") or snapshot
    extraction = understanding.get("customer_extraction")
    turns = snapshot.get("conversation") or []
    if (len(turns) >= 2 and turns[-2].get("role") == "agent" and turns[-2].get("text") == EDIT_QUESTION
            and turns[-1].get("role") == "founder" and sources
            and (corrected := validated_audience(extraction, [turns[-1].get("text", "")]))):
        return corrected
    if (snapshot.get("founder_fact_updated_at", {}).get("audience") and not fact.get("value")
            or isinstance(saved, str) and saved and is_unsure_answer(saved) and not is_skip(saved)):
        return ""
    answers = [*snapshot.get("conversation", []), *snapshot.get("answered_questions", [])]
    explicit = any(pair.get("field") in {"customer", "audience"} and not pair.get("reused")
        and _normalized(pair.get("founder_text") or pair.get("text", "")) == _normalized(candidate)
        and pair.get("provenance") != "suggested_choice" for pair in answers)
    explicit = explicit or fact.get("source") in {"review", "clarification", "founder message"} and _normalized(str(fact.get("value") or "")) == _normalized(candidate)
    if explicit and safe_audience(candidate):
        return candidate
    if audience := validated_audience(extraction, sources):
        return audience
    for source in reversed(sources):
        if buyer := stated_buyer(source):
            return buyer
    return "" if extraction is not None else safe_audience(candidate, inferred=False)


def grounded_model_question(question: dict, snapshot: dict) -> dict | None:
    from app.idea.runner import _founder_messages

    understanding = snapshot.get("understanding") or snapshot
    sources = _founder_messages({**snapshot, "original_idea": snapshot.get("original_idea", snapshot.get("idea", ""))})
    audience = validated_audience(understanding.get("customer_extraction"), sources)
    if not audience or _normalized(local_audience(snapshot, understanding.get("customer", ""))) != _normalized(audience):
        return None
    text = question.get("text", "")
    if text.count("?") != 1 or re.search(r"\b(?:get help|first step|limits must|help (?:you|them|your audience))\b", text, re.I):
        return None
    words = set(re.findall(r"[\w'-]+", _normalized(text)))
    context = set(re.findall(r"[\w'-]+", _normalized(" ".join(sources))))
    neutral = set("a an the for to of with in on at by and or who what when where why how you your they their it this that would could should can do does first today try pay trial product service people".split())
    if not (words & context) - neutral:
        return None
    from app.ventures.founder_facts import contradicts

    facts = snapshot.get("founder_facts") or {}
    if contradicts(text, facts):
        return None
    from app.secrets.detect import looks_like_secret
    if any(looks_like_secret(value) for value in question.values() if isinstance(value, str)):
        return None
    from app.ventures.founder_facts import filter_options
    from app.gstack.office_hours import _unsupported_claim
    stated = {**snapshot, "idea": " ".join(sources)}
    if any(_unsupported_claim(question.get(field, ""), stated) for field in ("text", "why", "recommendation_reason")):
        return None
    options = [option for option in filter_options(question.get("options", []), facts)
        if not looks_like_secret(option) and not _unsupported_claim(option, stated)]
    if options != question.get("options", []):
        return {**question, "options": options, "requires_detail": [option for option in question.get("requires_detail", []) if option in options],
            "recommended_option": "", "recommendation_reason": ""}
    return question
