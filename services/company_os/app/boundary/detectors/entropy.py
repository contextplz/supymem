"""Layer 3 — entropy (43 §5.3).

Fires below the blocking threshold on purpose. An unrecognised high-entropy
value in a value position is replaced with a placeholder rather than refusing
the whole call: the value still never leaves, and a checksum in a config file
does not cost the founder a prompt.

Pure hex can never exceed four bits per symbol, so hex digests and UUID lists
fall out of this layer arithmetically rather than by exception.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from app.boundary import DataClass, Finding, Segment, threshold

DETECTOR = "entropy"
CONFIDENCE = 0.6

_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_VALUE_POSITION = re.compile(r"[=:]\s*[\"'`]?$|\bBearer\s+$|\btoken\s+$")

# Prefixes of encoded binary we expect to see and must not mistake for keys.
_BLOB_MAGIC = ("iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR", "PHN2Zw", "JVBERi0", "AAAA")
_BLOB_CONTEXT = ("data:", "base64,", ";base64")


def shannon_bits(value: str) -> float:
    """Bits of entropy per symbol."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _is_blob(text: str, start: int, run: str) -> bool:
    if run.startswith(_BLOB_MAGIC):
        return True
    prefix = text[max(0, start - 32) : start].lower()
    return any(marker in prefix for marker in _BLOB_CONTEXT)


def _in_value_position(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    return bool(_VALUE_POSITION.search(text[line_start:start]))


def scan(segment: Segment) -> list[Finding]:
    text = segment.text
    if not text:
        return []
    min_run = int(threshold("entropy_min_run", 32))
    max_run = int(threshold("entropy_max_run", 200))
    bits = threshold("entropy_bits", 4.0)

    found: list[Finding] = []
    seen: set[str] = set()
    for match in _RUN.finditer(text):
        run = match.group(0)
        if len(run) < min_run or len(run) > max_run or run in seen:
            continue
        if _is_blob(text, match.start(), run):
            continue
        if not _in_value_position(text, match.start()):
            continue
        if shannon_bits(run) <= bits:
            continue
        seen.add(run)
        found.append(
            Finding(
                detector=DETECTOR,
                rule=f"entropy>{bits}",
                data_class=DataClass.SECRET,
                confidence=CONFIDENCE,
                value=run,
                label=segment.display(),
            )
        )
    return found
