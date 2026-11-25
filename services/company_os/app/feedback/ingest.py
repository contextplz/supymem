"""Ingest feedback — permanently label untrusted content."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.feedback.classify import classify_text
from app.feedback.normalize import delimit_untrusted, sanitize
from core.clock import utcnow
from core.ids import new_id

_EVENTS: list[dict[str, Any]] = []
_SECURITY: list[dict[str, Any]] = []


@dataclass
class IngestResult:
    id: str
    trust: str
    action_taken: str = "none"
    security_logged: bool = False
    delimited: str = ""


def ingest(text: str, *, source: str = "public", venture_id: str = "") -> IngestResult:
    clf = classify_text(text, source=source)
    clean = sanitize(text)
    delimited = delimit_untrusted(clean)
    eid = new_id("fb")
    ev = {
        "id": eid,
        "venture_id": venture_id,
        "text": clean,
        "trust": clf.trust,
        "source": source,
        "injection_attempt": clf.injection_attempt,
        "created_at": utcnow().isoformat(),
    }
    _EVENTS.append(ev)
    security = False
    if clf.injection_attempt:
        _SECURITY.append(
            {
                "kind": "prompt_injection",
                "event_id": eid,
                "matched": clf.matched,
                "ts": utcnow().isoformat(),
            }
        )
        security = True
    # Capability floor: never take autonomous action on untrusted alone
    return IngestResult(
        id=eid,
        trust=clf.trust,
        action_taken="none",
        security_logged=security,
        delimited=delimited,
    )


def security_events() -> list[dict[str, Any]]:
    return list(_SECURITY)


def clear_feedback() -> None:
    _EVENTS.clear()
    _SECURITY.clear()
