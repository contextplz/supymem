"""Prompt injection in third-party content (47 R13).

Content we observe — a README, an issue title, a webhook body — is data, never
instruction. Two things follow, and both are enforced here rather than asked for
in a prompt:

1. Observed content is wrapped in a delimited data block before it can reach a
   model. A prompt instruction telling the model to ignore instructions is not a
   guarantee; the wrapper plus the boundary filter are.
2. An injection attempt is recorded in `suspicious_content_events` so a silent
   ignore does not look like silent compliance.

Patterns come from `app.feedback.classify`, which already carries the taxonomy —
two drifting copies of an injection pattern list is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import SuspiciousContentEvent
from app.feedback.classify import INJECTION_PATTERNS
from app.feedback.normalize import delimit_untrusted, sanitize
from app.state import db
from core.clock import utcnow
from core.ids import new_id

# Enough to identify the attempt in the trust panel, short enough that a long
# injected paragraph cannot smuggle a payload into our own database.
EXCERPT_CHARS = 160

# Injection markers that specifically try to reach the data boundary. Kept here
# rather than in the shared feedback list because they are boundary-specific.
_BOUNDARY_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "print the contents of .env",
    "cat .env",
    "reveal the environment",
    "you are now",
    "new system prompt",
    "role-swap",
    "spoof a tool call",
    "<tool_call",
)


@dataclass(frozen=True)
class InjectionFinding:
    source_ref: str
    pattern: str
    excerpt: str


def detect(text: str, *, source_ref: str) -> list[InjectionFinding]:
    """Deterministic scan of one piece of observed content."""
    clean = sanitize(text or "")
    lowered = clean.lower()
    findings: list[InjectionFinding] = []
    seen: set[str] = set()

    for pattern in INJECTION_PATTERNS:
        match = pattern.search(clean)
        if match is None or pattern.pattern in seen:
            continue
        seen.add(pattern.pattern)
        findings.append(
            InjectionFinding(
                source_ref=source_ref,
                pattern=pattern.pattern,
                excerpt=_excerpt(clean, match.start()),
            )
        )
    for marker in _BOUNDARY_MARKERS:
        index = lowered.find(marker)
        if index < 0 or marker in seen:
            continue
        seen.add(marker)
        findings.append(
            InjectionFinding(
                source_ref=source_ref, pattern=marker, excerpt=_excerpt(clean, index)
            )
        )
    return findings


def _excerpt(text: str, start: int) -> str:
    begin = max(0, start - 20)
    return text[begin : begin + EXCERPT_CHARS].strip()


def wrap_third_party(text: str, *, source_ref: str) -> str:
    """Every piece of observed content reaches a prompt inside a data block."""
    return f"<data source={source_ref!r}>\n{delimit_untrusted(text)}\n</data>"


def record_events(
    findings: list[InjectionFinding], *, workspace_id: str | None = None
) -> list[str]:
    if not findings:
        return []
    ids: list[str] = []
    factory = db()
    now = utcnow()
    with factory() as session:
        for finding in findings:
            row_id = new_id("sce")
            session.add(
                SuspiciousContentEvent(
                    id=row_id,
                    workspace_id=workspace_id,
                    source_ref=finding.source_ref,
                    pattern=finding.pattern,
                    excerpt=finding.excerpt[:EXCERPT_CHARS],
                    at=now,
                )
            )
            ids.append(row_id)
        session.commit()
    return ids


def observe(
    text: str, *, source_ref: str, workspace_id: str | None = None
) -> tuple[str, list[str]]:
    """Scan, log, and return the text wrapped as data ready for a prompt."""
    findings = detect(text, source_ref=source_ref)
    event_ids = record_events(findings, workspace_id=workspace_id)
    return wrap_third_party(text, source_ref=source_ref), event_ids


def events(workspace_id: str | None = None) -> list[dict[str, str | None]]:
    from sqlalchemy import select

    factory = db()
    with factory() as session:
        stmt = select(SuspiciousContentEvent)
        if workspace_id is not None:
            stmt = stmt.where(SuspiciousContentEvent.workspace_id == workspace_id)
        rows = session.scalars(stmt.order_by(SuspiciousContentEvent.at.desc())).all()
        return [
            {
                "id": r.id,
                "source_ref": r.source_ref,
                "pattern": r.pattern,
                "excerpt": r.excerpt,
                "at": r.at.isoformat() if r.at else None,
            }
            for r in rows
        ]


__all__ = [
    "EXCERPT_CHARS",
    "InjectionFinding",
    "detect",
    "events",
    "observe",
    "record_events",
    "wrap_third_party",
]
