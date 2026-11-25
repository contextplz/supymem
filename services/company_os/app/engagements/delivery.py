"""The delivery contract and the four-part hand-over message (36 §7).

All four conditions are checked in code, never by a model: a deliverable that
hashes to what we recorded, a done-definition restated with its evidence, gates
that either ran or are plainly said not to have, and cost against cap. If any
declared outcome is unmet the engagement cannot reach ``delivered`` — it lands
``blocked`` with the gap named (47 R18).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.engagements.triage import NOTHING_LEFT_OUT

DELIVERABLE_KINDS = ("answer", "file", "config", "link", "note")

# The gates that mean anything for a bounded change. The other seven from doc 24
# need a venture build context and are honestly reported as not run.
ENGAGEMENT_GATES: tuple[str, ...] = (
    "secret_scan",
    "static_scan",
    "unit_tests",
    "error_handling",
    "migration_check",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Deliverable:
    kind: str
    label: str
    path_or_url: str | None = None
    sha256: str = ""
    content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "path_or_url": self.path_or_url,
            "sha256": self.sha256,
        }


def make_deliverable(
    kind: str,
    label: str,
    *,
    path: Path | str | None = None,
    url: str | None = None,
    content: str | None = None,
) -> Deliverable:
    """Content-address whatever we are handing over.

    Files hash their bytes, answers hash their text, links hash the reference we
    recorded — so "you already gave me this" is detectable either way (36 §3).
    """
    if path is not None:
        p = Path(path)
        return Deliverable(kind, label, str(p), sha256_file(p) if p.is_file() else "")
    if url is not None:
        return Deliverable(kind, label, url, sha256_text(url))
    body = content or ""
    return Deliverable(kind, label, None, sha256_text(body), content=body)


def load_deliverables(raw: str | None) -> list[Deliverable]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out: list[Deliverable] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        out.append(
            Deliverable(
                kind=str(item.get("kind") or "note"),
                label=str(item.get("label") or ""),
                path_or_url=item.get("path_or_url"),
                sha256=str(item.get("sha256") or ""),
                content=item.get("content"),
            )
        )
    return out


def dump_deliverables(items: Iterable[Deliverable]) -> str:
    return json.dumps([d.to_dict() for d in items])


# ---------------------------------------------------------------------------
# the four conditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateReport:
    ran: bool
    passed: int = 0
    total: int = 0
    failed: list[str] = field(default_factory=list)
    not_run_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def clean(self) -> bool:
        return not self.failed


def run_gates(context: dict[str, Any] | None) -> GateReport:
    """Run the engagement-shaped gates, or say plainly why they did not run."""
    if context is None:
        return GateReport(
            ran=False,
            not_run_reason="nothing was changed, so there was nothing to check",
        )
    from app.build.gates import (
        error_handling,
        migration_check,
        secret_scan,
        static_scan,
        unit_tests,
    )

    runners = {
        "secret_scan": secret_scan.run,
        "static_scan": static_scan.run,
        "unit_tests": unit_tests.run,
        "error_handling": error_handling.run,
        "migration_check": migration_check.run,
    }
    results = [runners[name](context) for name in ENGAGEMENT_GATES]
    failed = [r.name for r in results if not r.passed]
    return GateReport(
        ran=True,
        passed=len(results) - len(failed),
        total=len(results),
        failed=failed,
    )


def load_gate_report(raw: str | None) -> GateReport:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return GateReport(
        ran=bool(data.get("ran")),
        passed=int(data.get("passed") or 0),
        total=int(data.get("total") or 0),
        failed=[str(f) for f in (data.get("failed") or [])],
        not_run_reason=str(data.get("not_run_reason") or ""),
    )


@dataclass(frozen=True)
class DeliveryCheck:
    ok: bool
    failures: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.failures)


def verify_deliverables(items: Iterable[Deliverable]) -> DeliveryCheck:
    failures: list[str] = []
    evidence: list[str] = []
    for item in items:
        if not item.sha256:
            failures.append(f"{item.label} was never hashed")
            continue
        if item.path_or_url and not str(item.path_or_url).startswith(("http://", "https://")):
            path = Path(item.path_or_url)
            if not path.is_file():
                failures.append(f"{item.label} is missing from {path}")
                continue
            if sha256_file(path) != item.sha256:
                failures.append(f"{item.label} changed after it was recorded")
                continue
        elif item.content is not None and sha256_text(item.content) != item.sha256:
            failures.append(f"{item.label} changed after it was recorded")
            continue
        evidence.append(item.path_or_url or item.label)
    return DeliveryCheck(ok=not failures, failures=failures, evidence=evidence)


def check_delivery(
    *,
    deliverables: Iterable[Deliverable],
    definition_of_done: str,
    outcomes_met: bool,
    gates: GateReport,
    cost_usd: float,
    cost_cap_usd: float,
) -> DeliveryCheck:
    """All four conditions of 36 §7, plus the R18 subset rule."""
    items = list(deliverables)
    failures: list[str] = []

    if not items:
        failures.append("there is nothing to hand over yet")
    hashes = verify_deliverables(items)
    failures.extend(hashes.failures)

    if not definition_of_done.strip():
        failures.append("the done-definition was never written down")
    elif not hashes.evidence and items:
        failures.append("the done-definition has no evidence behind it")

    if not outcomes_met:
        failures.append("part of what you asked for is still unfinished")
    if gates.ran and gates.failed:
        failures.append(f"these checks failed: {', '.join(gates.failed)}")
    if not gates.ran and not gates.not_run_reason:
        failures.append("the checks did not run and no reason was recorded")
    if cost_usd > cost_cap_usd:
        failures.append("this cost more than its cap")

    return DeliveryCheck(ok=not failures, failures=failures, evidence=hashes.evidence)


# ---------------------------------------------------------------------------
# the four-part message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryMessage:
    asked_for: str
    changed: list[str]
    how_to_check: str
    not_doing: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self, labels: dict[str, str] | None = None) -> str:
        label = {
            "asked_for": "What you asked for",
            "changed": "What changed",
            "how_to_check": "How to check",
            "not_doing": "What I did not do",
            **(labels or {}),
        }
        width = max(len(v) for v in label.values())
        lines = [f"{label['asked_for']:<{width}} : {self.asked_for}"]
        lines.append(f"{label['changed']:<{width}} : {self.changed[0]}")
        pad = " " * (width + 3)
        lines.extend(f"{pad}{bullet}" for bullet in self.changed[1:])
        lines.append(f"{label['how_to_check']:<{width}} : {self.how_to_check}")
        lines.append(f"{label['not_doing']:<{width}} : {self.not_doing[0]}")
        lines.extend(f"{pad}{bullet}" for bullet in self.not_doing[1:])
        return "\n".join(lines)


def build_message(
    *,
    request_verbatim: str,
    deliverables: Iterable[Deliverable],
    how_to_check: str,
    not_doing: Iterable[str],
    gates: GateReport,
    cost_usd: float,
    cost_cap_usd: float,
) -> DeliveryMessage:
    """Two to four bullets, each naming a file or URL, plus the exclusions."""
    items = list(deliverables)
    changed = [
        f"{d.label}{d.path_or_url}" if d.path_or_url else d.label for d in items[:4]
    ]
    if gates.ran:
        changed.append(f"checks: {gates.passed} of {gates.total} passed")
    else:
        changed.append(f"checks: not run{gates.not_run_reason}")
    changed.append(f"cost: ${cost_usd:.4f} of a ${cost_cap_usd:.2f} cap")
    if not changed:
        changed = ["nothing was produced"]

    exclusions = [n for n in not_doing if n and n.strip()] or [NOTHING_LEFT_OUT]
    return DeliveryMessage(
        asked_for=_trim(request_verbatim),
        changed=changed,
        how_to_check=how_to_check or "open the file listed above",
        not_doing=exclusions,
    )


def _trim(text: str, limit: int = 200) -> str:
    body = " ".join(text.split())
    return body if len(body) <= limit else body[: limit - 1].rstrip() + "…"
