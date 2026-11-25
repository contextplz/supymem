"""Layered local detectors, cheapest first (43 §5).

No layer may call a model: a model must never be asked whether something is safe
to send to a model. Every layer is a pure function of its input, so the same
payload always produces the same findings — a detector that drifts between runs
cannot be regression-tested, and detector regressions are the highest-risk
change in the repo.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from app.boundary import Finding, Segment
from app.boundary.detectors import entropy, paths, patterns, pii, structural, vault

LAYERS = ("paths", "patterns", "entropy", "pii", "structural", "vault")


def scan_segment(segment: Segment, *, workspace_id: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(paths.scan(segment))
    findings.extend(patterns.scan(segment))
    findings.extend(entropy.scan(segment))
    findings.extend(pii.scan(segment))
    findings.extend(structural.scan(segment))
    findings.extend(vault.scan(segment, workspace_id=workspace_id))
    return findings


def scan(
    segments: Sequence[Segment] | Iterable[Segment], *, workspace_id: str | None = None
) -> list[Finding]:
    out: list[Finding] = []
    for segment in segments:
        out.extend(scan_segment(segment, workspace_id=workspace_id))
    return out


__all__ = [
    "LAYERS",
    "entropy",
    "paths",
    "patterns",
    "pii",
    "scan",
    "scan_segment",
    "structural",
    "vault",
]
