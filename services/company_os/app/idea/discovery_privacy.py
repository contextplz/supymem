"""Reject concrete unsafe discovery requests; this is not semantic privacy proof.

Only generated review prose is inspected. Founder answers remain attributed input.
A rejected answer follows the existing metered repair/failure path, never rewritten.
"""
from __future__ import annotations

import json
import re

_CHILDCARE = re.compile(r"\b(?:childcare|child[- ]care|children|child data)\b", re.I)
_VERBAL_ONLY = re.compile(r"\b(?:anonymi[sz]ed\s+verbal|verbal\s+(?:descriptions?|summar(?:y|ies))\s+only|no\s+child\s+data)\b", re.I)
_REQUEST = re.compile(r"\b(?:show|share|forward|send|collect|request|photograph|record|observe|watch|capture|hand\s+over|screen[- ]?share)\b", re.I)
_PRIVATE = re.compile(r"\b(?:private\s+(?:messages?|chats?|conversations?|threads?)|(?:actual|real|client|customer|parent)\s+(?:WhatsApp|emails?|texts?|chats?|messages?|contacts?)|contact\s+lists?|screenshots?|child(?:ren)?(?:'s)?\s+(?:records?|photos?|names?|details))\b", re.I)
_CHILD_SURFACE = re.compile(r"\b(?:child(?:ren)?|parents?|pickups?|pick[- ]ups?|family|attendance|meals?|naps?|incidents?|medical)\b", re.I)
_CHILD_REPLAY = re.compile(r"\b(?:walk\s+(?:me|you|her|him|them)?\s*through|describe\s+in\s+(?:painful\s+)?detail|what\s+(?:she|he|they|you)\s+(?:told\s+parents|wrote\s+down))\b", re.I)
_ANON = re.compile(r"\b(?:anonymi[sz]ed|synthetic|general\s+workflow|without\s+(?:names|identifiers|personal\s+details))\b", re.I)
_SYNTHETIC = re.compile(r"\b(?:synthetic|fictional|made[- ]up)\b", re.I)
_CONSENT = re.compile(
    r"\b(?:(?:affected\s+)?(?:customers?|clients?|people|parties)(?:['’]s|['’])?\s+(?:informed\s+)?(?:consent|permission)|"
    r"(?:consent|permission)\s+(?:from|of)\s+(?:the\s+)?(?:affected\s+)?(?:customers?|clients?|people|parties))\b", re.I)
_REDACT = re.compile(r"\b(?:redact(?:ed|ion)?|remove\s+(?:names|identifiers))\b", re.I)
_NEGATED = re.compile(r"\b(?:do\s+not|don't|never|must\s+not|cannot|avoid|without|no\s+(?:request\s+to|permission\s+to))\b", re.I)


def _prohibited(statement: str, match: re.Match) -> bool:
    # Scope negation to this clause, not another sentence or an earlier safe option.
    prefix = statement[max(0, match.start() - 75):match.start()]
    prefix = re.split(r"\b(?:and|but|then)\b", prefix, flags=re.I)[-1]
    return not _NEGATED.search(prefix)


def _positive_qualifier(pattern, statement):
    for match in pattern.finditer(statement):
        prefix = statement[:match.start()]
        suffix = statement[match.end():match.end() + 40]
        if (_prohibited(statement, match) and not re.search(r"\b(?:no|not)\s*$", prefix, re.I)
                and not re.match(r"\s+(?:is\s+)?(?:not\s+(?:required|given|provided)|denied|unavailable)", suffix, re.I)):
            return True
    return False


def unsafe_discovery_request(value: dict, revision_text: str) -> bool:
    revision = json.loads(revision_text)
    context = json.dumps(revision, ensure_ascii=False)
    childcare = bool(_CHILDCARE.search(context))
    verbal_only = bool(_VERBAL_ONLY.search(context))

    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, list):
            for child in node:
                yield from strings(child)
        elif isinstance(node, dict):
            for key, child in node.items():
                if key not in {"founder_answer", "source_ids", "question_id", "method", "verdict"}:
                    yield from strings(child)

    for text in strings(value):
        for statement in re.split(r"[.!?;\n]+", text):
            requests = [m for m in _REQUEST.finditer(statement) if _prohibited(statement, m)]
            synthetic = bool(_SYNTHETIC.search(statement)) and not re.search(r"\b(?:actual|real|authentic)\b", statement, re.I)
            if requests and _PRIVATE.search(statement):
                # Authentic customer examples require both affected-party consent
                # and redaction locally. Explicit verbal-only requirements forbid them.
                if not synthetic and (verbal_only or not (
                        _positive_qualifier(_CONSENT, statement) and _positive_qualifier(_REDACT, statement))):
                    return True
            if childcare and _CHILD_SURFACE.search(statement):
                child_requests = [m for m in requests if m.group().lower() not in {"send", "share", "forward"}]
                if child_requests and not synthetic and (
                        not _ANON.search(statement) or re.search(r"\b(?:observe|watch|photograph)\b", statement, re.I)):
                    return True
                if verbal_only and any(_prohibited(statement, m) for m in _CHILD_REPLAY.finditer(statement)):
                    if not _ANON.search(statement):
                        return True
    return False
