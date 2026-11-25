"""Distiller — task scratch → 0..5 durable memories. Stingy by design (doc 72 §1)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.memory.model import MemoryKind, MemoryWrite


_MUNDANE = re.compile(
    r"\b(checked|status|ping|heartbeat|noop|idle|ack|ok)\b",
    re.IGNORECASE,
)


def distill_task(
    *,
    venture_id: str,
    task_summary: str,
    scratch: dict[str, Any] | None = None,
    founder_correction: str | None = None,
    author: str = "system",
) -> list[MemoryWrite]:
    """Return 0..5 MemoryWrite candidates. Zero is the common and correct answer.

    This is a deterministic first-pass (no LLM) so tests and offline mode work.
    A Flash LLM path can replace the heuristic later without changing callers.
    """
    scratch = scratch or {}
    out: list[MemoryWrite] = []

    if founder_correction and founder_correction.strip():
        statement = founder_correction.strip()
        # Prefer rule-level phrasing if they wrote an instance edit.
        if len(statement) < 200:
            rule = statement
        else:
            rule = statement[:180].rsplit(" ", 1)[0] + "…"
        out.append(
            MemoryWrite(
                venture_id=venture_id,
                kind=MemoryKind.STYLE_CORRECTION,
                subject="style.founder",
                statement=rule,
                detail=founder_correction.strip(),
                evidence=[f"correction:{hashlib.sha256(founder_correction.encode()).hexdigest()[:12]}"],
                confidence=1.0,
                confidence_source="founder",
                author=author,
                dedupe_key=_dedupe(venture_id, "style_correction", "style.founder", rule),
                half_life_days=None,
            )
        )

    summary = (task_summary or "").strip()
    if not summary or _MUNDANE.search(summary):
        return out

    # Only promote clearly marked outcomes / failures from scratch.
    if scratch.get("failed"):
        why = str(scratch.get("failure_reason") or summary)
        out.append(
            MemoryWrite(
                venture_id=venture_id,
                kind=MemoryKind.FAILURE,
                subject=str(scratch.get("subject") or "task.failure"),
                statement=why[:240],
                detail=summary,
                evidence=[str(scratch.get("evidence_id") or f"task:{hashlib.sha256(summary.encode()).hexdigest()[:12]}")],
                confidence=float(scratch.get("confidence") or 0.7),
                confidence_source="outcome",
                author=author,
                dedupe_key=_dedupe(venture_id, "failure", str(scratch.get("subject") or "task.failure"), why),
            )
        )
    elif scratch.get("durable_fact"):
        fact = str(scratch["durable_fact"]).strip()
        out.append(
            MemoryWrite(
                venture_id=venture_id,
                kind=MemoryKind.OUTCOME,
                subject=str(scratch.get("subject") or "task.outcome"),
                statement=fact[:240],
                detail=summary,
                evidence=[str(scratch.get("evidence_id") or f"task:{hashlib.sha256(fact.encode()).hexdigest()[:12]}")],
                confidence=float(scratch.get("confidence") or 0.6),
                confidence_source="outcome",
                author=author,
                dedupe_key=_dedupe(venture_id, "outcome", str(scratch.get("subject") or "task.outcome"), fact),
            )
        )

    return out[:5]


def _dedupe(venture_id: str, kind: str, subject: str, statement: str) -> str:
    norm = " ".join(statement.lower().split())
    raw = f"{venture_id}|{kind}|{subject}|{norm}"
    return hashlib.sha256(raw.encode()).hexdigest()
