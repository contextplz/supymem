"""The boundary filter: scan → refuse → redact → re-scan → record → send (43 §4).

Called by `llm_gateway.sole.transmit`, which is the only door to a provider. The
order is the whole design: recording before sending means a crash mid-call still
leaves evidence, and refusing before redacting means we never truncate a secret
and send the rest.

The re-scan after redaction is deliberate belt-and-braces. If a placeholder pass
somehow leaves a forbidden value in place, the call is refused rather than sent.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from llm_gateway.boundary import BoundaryViolation, Decision

from app.audit.writer import audit
from app.boundary import NARRATION_KEYS, PLACEHOLDER, DataClass, Segment
from app.boundary.classify import PayloadClassification, classify
from app.boundary.ledger import assert_debug_storage_allowed, record
from core.redact import redact

_RECORD_TABLE_KEY = "boundary.blocked_records"
_FALLBACK_LINES = {
    DataClass.SECRET: "Skipped {file}, it contains credentials.",
    DataClass.THIRD_PARTY_PII: "Skipped {file}, it holds other people's personal details.",
    DataClass.UNKNOWN: "Skipped {file}, I could not tell what is in it.",
}


def _line(key: str, tokens: dict[str, Any], fallback: str) -> str:
    """Founder-facing copy from the narration deck, with a safe fallback."""
    try:
        from app.onboarding.narrator import interpolate, resolve_variant

        template = resolve_variant(key, status="blocked") or resolve_variant(key)
        if template:
            return interpolate(template, tokens)
    except Exception:  # noqa: BLE001 — a missing deck must not hide the refusal
        pass
    return fallback.format(**tokens)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str, sort_keys=True)


def segments_from_messages(messages: Sequence[dict[str, Any]]) -> list[Segment]:
    return [
        Segment(
            text=_text_of(message.get("content")),
            label=f"the {message.get('role', 'user')} message",
        )
        for message in messages
    ]


def _segments_after_redaction(
    original: Sequence[dict[str, Any]],
    cleaned: Sequence[dict[str, Any]],
    classified: Sequence[Segment],
) -> list[Segment]:
    """Re-scan cleaned bytes without discarding verified provenance.

    Callers may attach a class and origin to each message.  The second scan
    used to rebuild anonymous segments, which made ANSI-heavy reviewed build
    output look like an unknown binary blob even though the first scan had
    correctly classified it as workspace code.  Preserve metadata only when
    the supplied segments are an exact one-to-one description of the original
    messages; otherwise fall back to anonymous segments so mismatched metadata
    can never weaken the belt-and-braces scan.
    """
    if len(original) != len(cleaned) or len(original) != len(classified):
        return segments_from_messages(cleaned)
    if any(segment.text != _text_of(message.get("content"))
           for message, segment in zip(original, classified, strict=True)):
        return segments_from_messages(cleaned)
    return [
        Segment(
            text=_text_of(message.get("content")),
            path=segment.path,
            origin=segment.origin,
            declared_class=segment.declared_class,
            label=segment.label,
            published_contact_email=segment.published_contact_email,
        )
        for message, segment in zip(cleaned, classified, strict=True)
    ]


def refusal_message(classification: PayloadClassification) -> tuple[str, str | None]:
    """A founder-facing refusal naming the class and the file, never the value."""
    lines: list[str] = []
    alternative: str | None = None
    for refused in classification.refused:
        tokens = {"file": refused.segment.display()}
        is_table = "record_table" in refused.reason
        key = _RECORD_TABLE_KEY if is_table else NARRATION_KEYS.get(refused.data_class, "")
        fallback = (
            "Skipped {file}, it is a table of records, not writing."
            if is_table
            else _FALLBACK_LINES.get(refused.data_class, "Skipped {file}.")
        )
        line = _line(key, tokens, fallback) if key else fallback.format(**tokens)
        if line not in lines:
            lines.append(line)
        if is_table or refused.data_class is DataClass.THIRD_PARTY_PII:
            alternative = _line(
                "boundary.offer_local",
                {},
                "I can count and group that here instead, without sending it.",
            )
    return " ".join(lines), alternative


def _redact_messages(
    messages: Sequence[dict[str, Any]], values: Sequence[str]
) -> tuple[list[dict[str, Any]], int]:
    before = sum(_text_of(m.get("content")).count(PLACEHOLDER) for m in messages)
    cleaned = [redact(dict(m), exact_values=list(values)) for m in messages]
    after = sum(_text_of(m.get("content")).count(PLACEHOLDER) for m in cleaned)
    return cleaned, max(0, after - before)


def _bytes_of(messages: Sequence[dict[str, Any]]) -> int:
    return len(json.dumps(messages, default=str).encode("utf-8"))


def inspect_payload(
    messages: list[dict[str, Any]],
    *,
    purpose: str,
    workspace_id: str | None,
    provider: str,
    segments: Sequence[Segment] | None = None,
    trace_id: str | None = None,
) -> Decision:
    assert_debug_storage_allowed()

    parts = list(segments) if segments else segments_from_messages(messages)
    if any(part.published_contact_email for part in parts):
        parts = _segments_after_redaction(messages, messages, parts)
    classification = classify(parts, workspace_id=workspace_id, purpose=purpose)

    if classification.refused:
        raise _refuse(
            classification,
            purpose=purpose,
            workspace_id=workspace_id,
            provider=provider,
            bytes_out=_bytes_of(messages),
            trace_id=trace_id,
            stage="scan",
        )

    cleaned, redactions = _redact_messages(
        messages, [f.value for f in classification.redactable()]
    )

    # Belt and braces: whatever we are actually about to send is classified again.
    after = classify(
        _segments_after_redaction(messages, cleaned, parts), workspace_id=workspace_id, purpose=purpose
    )
    if after.refused:
        raise _refuse(
            after,
            purpose=purpose,
            workspace_id=workspace_id,
            provider=provider,
            bytes_out=_bytes_of(cleaned),
            trace_id=trace_id,
            stage="post_redaction",
        )

    classes = tuple(sorted({*classification.classes, *after.classes}))
    bytes_out = _bytes_of(cleaned)
    ledger_id = record(
        workspace_id=workspace_id,
        provider=provider,
        purpose=purpose,
        classes=classes,
        bytes_out=bytes_out,
        redactions=redactions,
        blocked=False,
        trace_id=trace_id,
    )
    return Decision(
        messages=cleaned,
        classes=classes,
        redactions=redactions,
        bytes_out=bytes_out,
        provider=provider,
        ledger_id=ledger_id,
        trace_id=trace_id,
        findings=tuple(sorted({f.rule for f in classification.findings})),
    )


def _refuse(
    classification: PayloadClassification,
    *,
    purpose: str,
    workspace_id: str | None,
    provider: str,
    bytes_out: int,
    trace_id: str | None,
    stage: str,
) -> BoundaryViolation:
    message, alternative = refusal_message(classification)
    reason = f"{stage}:{classification.summary()}"
    classes = tuple(str(s.data_class) for s in classification.refused)
    ledger_id = record(
        workspace_id=workspace_id,
        provider=provider,
        purpose=purpose,
        classes=classes,
        bytes_out=bytes_out,
        blocked=True,
        block_reason=reason,
        trace_id=trace_id,
    )
    audit(
        "boundary.blocked",
        workspace_id=workspace_id,
        surface="internal",
        target_ref=ledger_id,
        outcome="blocked",
        denied_reason=reason,
        payload={"purpose": purpose, "provider": provider, "classes": list(classes)},
    )
    full = message if alternative is None else f"{message} {alternative}"
    return BoundaryViolation(
        full,
        classes=classes,
        reason=reason,
        ledger_id=ledger_id,
        alternative=alternative,
    )


__all__ = [
    "inspect_payload",
    "refusal_message",
    "segments_from_messages",
]
