"""Founder-facing copy helpers. Never emit an em dash or en dash."""

from __future__ import annotations

import re


def no_dashes(text: str | None) -> str:
    """No em/en dashes in text a founder sees. Number ranges keep a hyphen, any other dash becomes a comma."""
    text = re.sub(r"(?<=\d)[ \t]*[–—][ \t]*(?=\d)", "-", text or "")
    text = re.sub(r"[ \t]*[–—]+[ \t]*", "\0", text)
    return re.sub(r"(?m)^\0|\0(?=[.,;:!?\n]|$)", "", text).replace("\0", ", ")


strip_emdash = no_dashes  # older name, same behaviour


def founder_copy(text: str | None) -> str:
    text = no_dashes(text)
    text = re.sub(r"(?<![\w-])([Tt]he founder|Founder)(['’]s)?\b",
                  lambda match: ("your" if match[2] else "you").capitalize()
                  if match[1][0].isupper() else "your" if match[2] else "you", text)
    verbs = {"has": "have", "is": "are", "was": "were", "does": "do", "wants": "want",
             "plans": "plan", "needs": "need", "intends": "intend", "hopes": "hope", "knows": "know",
             "expects": "expect", "believes": "believe", "aims": "aim", "builds": "build", "says": "say"}
    # ponytail: explicit English verbs and one listed adverb, extend the lists for observed misses.
    return re.sub(r"\b([Yy]ou)(\s+(?:(?:already|still|just|also|only|never)\s+)?)(" + "|".join(verbs) + r")(n['’]t)?\b",
                  lambda match: match[1] + match[2] + verbs[match[3]] + (match[4] or ""), text)
