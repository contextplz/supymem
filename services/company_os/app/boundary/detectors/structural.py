"""Layer 5 — structure (43 §5.5). A table of records is data, not writing.

Classified `unknown` rather than `third_party_pii`: we genuinely do not know
whose rows these are, and `unknown` refuses just as hard without claiming to
have identified people. The PII layer escalates separately when it recognises
the contents.

A uniform table of harmless rows is refused too. That is a deliberate false
positive: whole-table egress is the shape of every export that has ever leaked.
"""

from __future__ import annotations

import json

from app.boundary import DataClass, Finding, Segment, threshold

DETECTOR = "structural"
_DELIMITERS = (",", "\t", ";", "|")
# Below this share of matching rows the payload is prose that happens to contain
# a table, not a table.
_UNIFORMITY = 0.9
# Source files put a delimiter on nearly every line too. Statement and block
# terminators tell the two shapes apart: a record row never ends in one.
_CODE_TERMINATORS = ("{", "}", ";", ")", ":", "*/")
_CODE_RATIO = 0.5


def _rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def looks_like_code(lines: list[str]) -> bool:
    ending = sum(1 for line in lines if line.rstrip().endswith(_CODE_TERMINATORS))
    return ending / len(lines) > _CODE_RATIO


def _uniform_delimited(lines: list[str]) -> tuple[str, int] | None:
    for delimiter in _DELIMITERS:
        counts = [line.count(delimiter) for line in lines]
        modal = max(set(counts), key=counts.count)
        if modal < 1:
            continue
        matching = sum(1 for c in counts if c == modal)
        if matching / len(lines) >= _UNIFORMITY:
            return delimiter, matching
    return None


def _jsonl_rows(lines: list[str]) -> int:
    matching = 0
    for line in lines:
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            json.loads(stripped)
        except ValueError:
            continue
        matching += 1
    return matching if matching / len(lines) >= _UNIFORMITY else 0


def record_rows(text: str) -> tuple[str, int] | None:
    """Return (shape, row count) when the text is mostly a record table."""
    lines = _rows(text)
    minimum = int(threshold("structural_min_rows", 20))
    if len(lines) <= minimum:
        return None
    jsonl = _jsonl_rows(lines)
    if jsonl > minimum:
        return "jsonl", jsonl
    if looks_like_code(lines):
        return None
    delimited = _uniform_delimited(lines)
    if delimited is not None and delimited[1] > minimum:
        return f"delimited:{delimited[0]!r}", delimited[1]
    return None


def scan(segment: Segment) -> list[Finding]:
    hit = record_rows(segment.text)
    if hit is None:
        return []
    shape, rows = hit
    return [
        Finding(
            detector=DETECTOR,
            rule=f"record_table:{shape}:{rows}",
            data_class=DataClass.UNKNOWN,
            confidence=1.0,
            label=segment.display(),
        )
    ]
