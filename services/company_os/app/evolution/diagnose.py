"""Deterministic detectors first — healthy ventures cost $0 Pro."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Finding:
    kind: str
    subject: str
    evidence: list[str]
    severity: str = "med"


def run_deterministic_detectors(venture: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    # engagement decay
    series = venture.get("engagement_series") or []
    if len(series) >= 5 and series[-1] < series[0] * 0.5:
        findings.append(
            Finding(
                kind="engagement_decay",
                subject=venture.get("operator_id") or "operator",
                evidence=[f"engagement {series[0]} -> {series[-1]}"],
            )
        )
    # feedback cluster
    feedback = venture.get("feedback_topics") or {}
    for topic, n in feedback.items():
        if n >= 5:
            findings.append(
                Finding(
                    kind="feedback_cluster",
                    subject=topic,
                    evidence=[f"{n} events on {topic}"],
                )
            )
    return findings


def synthesize_diagnosis(findings: list[Finding]) -> dict[str, Any]:
    return {
        "findings": [f.__dict__ for f in findings],
        "hypotheses": [
            {"kind": "config_change", "subject": f.subject, "from_finding": f.kind}
            for f in findings
        ]
        + [
            {"kind": "org_replace", "subject": f.subject, "from_finding": f.kind}
            for f in findings
            if f.kind == "engagement_decay"
        ],
        "pro_calls": 1 if findings else 0,
        "cost_usd": 0.01 if findings else 0.0,
    }
