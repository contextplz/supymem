"""Data classification, seven classes (43 §3).

Every payload crossing the boundary is classified. A forbidden finding always
wins over whatever the caller declared: a prompt template cannot opt in to
sending a key. Where nothing can be established, the answer is `unknown`, which
is refused — an unclassifiable payload is the one we most want to keep at home.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.boundary import (
    FORBIDDEN,
    SENDABLE,
    DataClass,
    Finding,
    Segment,
    may_reach_model,
    threshold,
    worst,
)
from app.boundary.detectors import entropy as entropy_layer
from app.boundary.detectors import scan_segment

CODE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
        ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m",
        ".scala", ".sh", ".bash", ".zsh", ".sql", ".css", ".scss", ".html", ".vue",
        ".svelte", ".lua", ".ex", ".exs", ".erl", ".dart", ".j2",
    }
)
DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc", ".txt", ".org"})
META_SUFFIXES = frozenset(
    {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".lock", ".gitignore"}
)

# Beyond this length a prose payload is somebody's document rather than a note
# about their stack.
_PROSE_IS_BUSINESS = 400
_CODE_MARKERS = (
    "def ", "class ", "function ", "import ", "return ", "const ", "let ",
    "=> ", "public ", "#include", "package ", "fn ",
)
# Keep each token shorter than the generic assignment detector's common opaque
# secret shapes. This recognizes ordinary calls, not arbitrary call-like text.
_CODE_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]{0,19}"
_CODE_REFERENCE = rf"(?:{_CODE_IDENTIFIER}\.)*{_CODE_IDENTIFIER}"
_CODE_CALL_EXPRESSION = re.compile(
    rf"{_CODE_REFERENCE}\((?:{_CODE_REFERENCE}(?:,\s*{_CODE_REFERENCE})*)?\)?$"
)


@dataclass(frozen=True)
class SegmentClassification:
    segment: Segment
    data_class: DataClass
    findings: tuple[Finding, ...]
    reason: str

    @property
    def sendable(self) -> bool:
        return may_reach_model(self.data_class)


@dataclass(frozen=True)
class PayloadClassification:
    segments: tuple[SegmentClassification, ...]

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for s in self.segments for f in s.findings)

    @property
    def classes(self) -> tuple[str, ...]:
        seen = {s.data_class for s in self.segments}
        return tuple(str(c) for c in sorted(seen, key=lambda c: str(c)))

    @property
    def refused(self) -> tuple[SegmentClassification, ...]:
        return tuple(s for s in self.segments if not s.sendable)

    @property
    def sendable(self) -> bool:
        return not self.refused

    def worst_class(self) -> DataClass:
        return worst(s.data_class for s in self.segments)

    def redactable(self) -> tuple[Finding, ...]:
        """Findings we replace with a placeholder instead of refusing over."""
        limit = threshold("block_confidence", 0.8)
        return tuple(
            f for f in self.findings if f.value and (not f.forbidden or f.confidence < limit)
        )

    def summary(self) -> str:
        """Ledger-safe description: classes, rules and labels, never values."""
        parts = [
            f"{s.segment.display()}={s.data_class}({s.reason})" for s in self.refused
        ]
        return "; ".join(parts) or f"classes={','.join(self.classes)}"


def blocking_findings(findings: tuple[Finding, ...] | list[Finding]) -> list[Finding]:
    limit = threshold("block_confidence", 0.8)
    return [f for f in findings if f.forbidden and f.confidence >= limit]


def looks_binary(text: str) -> bool:
    """Undecidable input. A random blob is refused, never truncated and sent."""
    if not text:
        return False
    unreadable = sum(1 for c in text if not c.isprintable() and c not in "\t\n\r ")
    return unreadable / len(text) > threshold("binary_ratio", 0.02)


def infer_class(segment: Segment) -> tuple[DataClass, str]:
    """Positive classification for a segment with no forbidden findings."""
    text = segment.text
    if not text.strip():
        return DataClass.PUBLIC, "empty"
    if looks_binary(text):
        return DataClass.UNKNOWN, "undecodable"

    suffix = PurePosixPath(segment.path).suffix.lower() if segment.path else ""
    if suffix in CODE_SUFFIXES:
        return DataClass.WORKSPACE_CODE, f"suffix:{suffix}"
    if suffix in DOC_SUFFIXES:
        return DataClass.WORKSPACE_BUSINESS, f"suffix:{suffix}"
    if suffix in META_SUFFIXES:
        return DataClass.WORKSPACE_META, f"suffix:{suffix}"

    if any(marker in text for marker in _CODE_MARKERS):
        return DataClass.WORKSPACE_CODE, "code_markers"
    # A single opaque high-entropy token with no prose around it tells us
    # nothing, and guessing `public` for it is exactly the wrong instinct.
    if len(text) >= 64 and " " not in text.strip():
        if entropy_layer.shannon_bits(text) > threshold("entropy_bits", 4.0):
            return DataClass.UNKNOWN, "opaque_token"
    if len(text) > _PROSE_IS_BUSINESS:
        return DataClass.WORKSPACE_BUSINESS, "long_prose"
    return DataClass.WORKSPACE_META, "short_prose"


def classify_segment(
    segment: Segment,
    *,
    workspace_id: str | None = None,
    findings: list[Finding] | None = None,
    purpose: str | None = None,
) -> SegmentClassification:
    hits = findings if findings is not None else scan_segment(segment, workspace_id=workspace_id)
    if purpose == "gtm.draft_post" and segment.origin == "published_outreach_recipient":
        hits = [hit for hit in hits if not (
            hit.detector == "pii" and hit.rule in {"email", "email:bulk"}
            and hit.value == segment.published_contact_email
        )]
    blocking = blocking_findings(hits)
    # Reviewed build sandboxes begin from Supymem-owned source and return tool
    # output such as diffs, listings and test reports to the coding model. Those
    # outputs frequently look like tabular exports.  Preserve the broad table
    # refusal everywhere else, while allowing this one provenance marker to
    # classify a structural-only finding as code. Secret, PII, path, entropy
    # and vault findings still block before any provider call.
    reviewed_code_false_positive = (
        segment.origin == "reviewed_build_tool"
        and segment.declared_class == DataClass.WORKSPACE_CODE
        and blocking
        and all(
            (
                finding.detector == "structural"
                and finding.data_class is DataClass.UNKNOWN
                and finding.rule.startswith("record_table:")
            )
            or (
                finding.detector == "patterns"
                and finding.data_class is DataClass.SECRET
                and finding.rule == "assignment"
                and bool(_CODE_CALL_EXPRESSION.fullmatch(finding.value or ""))
            )
            for finding in blocking
        )
    )
    if blocking and not reviewed_code_false_positive:
        data_class = worst(f.data_class for f in blocking)
        rules = sorted({f.rule for f in blocking if f.data_class == data_class})
        return SegmentClassification(
            segment=segment,
            data_class=data_class,
            findings=tuple(hits),
            reason=",".join(rules[:3]),
        )

    declared = segment.declared_class
    if declared is not None:
        try:
            candidate = DataClass(declared)
        except ValueError:
            return SegmentClassification(
                segment=segment,
                data_class=DataClass.UNKNOWN,
                findings=tuple(hits),
                reason=f"undeclarable:{declared}",
            )
        # A caller may declare a narrower class than we infer, never a forbidden
        # one that magically becomes sendable.
        if candidate in FORBIDDEN or candidate in SENDABLE:
            return SegmentClassification(
                segment=segment,
                data_class=candidate,
                findings=tuple(hits),
                reason="declared",
            )

    data_class, reason = infer_class(segment)
    return SegmentClassification(
        segment=segment, data_class=data_class, findings=tuple(hits), reason=reason
    )


def classify(
    segments: list[Segment] | tuple[Segment, ...], *, workspace_id: str | None = None,
    purpose: str | None = None,
) -> PayloadClassification:
    return PayloadClassification(
        segments=tuple(
            classify_segment(s, workspace_id=workspace_id, purpose=purpose) for s in segments
        )
    )


__all__ = [
    "PayloadClassification",
    "SegmentClassification",
    "blocking_findings",
    "classify",
    "classify_segment",
    "infer_class",
    "looks_binary",
]
