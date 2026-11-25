"""Fail closed on concrete availability claims in an explicitly unbuilt landing revision.

This is a narrow claim check, not semantic proof of all marketing copy. It never
rewrites an exact browser contract or infers that a product is working.
"""
from __future__ import annotations

import re

_CONCEPT = re.compile(
    r"\b(?:unbuilt|concept[- ]only|(?:early[- ]access\s+)?concept\s+(?:page|copy)|"
    r"no\s+(?:working\s+)?(?:app|product|booking)|product\s+is\s+not\s+launched)\b", re.I)
_FUNCTIONAL = re.compile(
    r"\b(?:sends|automates|lets\s+you|allows\s+you|enables\s+you|saves|"
    r"(?:clients?|customers?)\s+(?:can\s+)?(?:confirm|book))\b", re.I)
_EVIDENCE = re.compile(
    r"\b(?:(?:is|are)\s+(?:live|available\s+now)|trusted\s+by|used\s+by|"
    r"(?:[1-9][0-9]*|hundreds|thousands)\s+(?:active\s+)?customers|"
    r"validated\s+(?:outcomes|results)|proven\s+(?:savings|results))\b", re.I)
_FUTURE = re.compile(
    r"^(?:the\s+)?(?:planned|proposed|future|illustrative|concept)\s+"
    r"(?:workflow|capabilit(?:y|ies)|feature|example|preview|product)\b", re.I)
_NOW = re.compile(r"\b(?:currently|already|now|today)\b", re.I)


def validate_concept_copy(required_text: list[str], context: dict) -> None:
    # The latest explicit revision instruction controls present-tense copy.
    # Desired future functionality and an old published page are not proof.
    instruction = context.get("founder_instruction")
    if not isinstance(instruction, str) or not _CONCEPT.search(instruction):
        return
    for text in required_text:
        for sentence in re.split(r"[.!?;]\s*", text):
            for claim in (*_FUNCTIONAL.finditer(sentence), *_EVIDENCE.finditer(sentence)):
                prefix = sentence[:claim.start()]
                if re.search(r"\b(?:not|never|no)\s*$", prefix, re.I):
                    continue
                if (claim.re is _FUNCTIONAL and _FUTURE.match(sentence.strip())
                        and not _NOW.search(sentence)):
                    continue
                raise ValueError("landing_concept_current_claim")
