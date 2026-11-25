"""Data boundary taxonomy (43 §3).

This module holds only the class table, the finding record and the policy file
loader. Detectors and the classifier both import from here, so it must not
import them back.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from llm_gateway.boundary import BoundaryViolation

REPO_ROOT = Path(__file__).resolve().parents[4]
POLICY_PATH = REPO_ROOT / "config" / "boundary.yaml"


class DataClass(StrEnum):
    PUBLIC = "public"
    WORKSPACE_META = "workspace_meta"
    WORKSPACE_CODE = "workspace_code"
    WORKSPACE_BUSINESS = "workspace_business"
    THIRD_PARTY_PII = "third_party_pii"
    SECRET = "secret"
    UNKNOWN = "unknown"


ALL_CLASSES: tuple[DataClass, ...] = tuple(DataClass)

SENDABLE: frozenset[DataClass] = frozenset(
    {
        DataClass.PUBLIC,
        DataClass.WORKSPACE_META,
        DataClass.WORKSPACE_CODE,
        DataClass.WORKSPACE_BUSINESS,
    }
)

FORBIDDEN: frozenset[DataClass] = frozenset(
    {DataClass.THIRD_PARTY_PII, DataClass.SECRET, DataClass.UNKNOWN}
)

# Severity order for "the worst class in a payload decides". `unknown` outranks
# nothing except the sendable classes: a payload that is both unclassifiable and
# holds a key is reported as holding a key, which is the more useful line.
SEVERITY: dict[DataClass, int] = {
    DataClass.PUBLIC: 0,
    DataClass.WORKSPACE_META: 1,
    DataClass.WORKSPACE_CODE: 2,
    DataClass.WORKSPACE_BUSINESS: 3,
    DataClass.UNKNOWN: 4,
    DataClass.THIRD_PARTY_PII: 5,
    DataClass.SECRET: 6,
}

# Narration key per refused class (config/narration.d/boundary.yaml).
NARRATION_KEYS: dict[DataClass, str] = {
    DataClass.SECRET: "boundary.blocked_secret",
    DataClass.THIRD_PARTY_PII: "boundary.blocked_pii",
    DataClass.UNKNOWN: "boundary.blocked_unknown",
}

PLACEHOLDER = "[REDACTED]"


@dataclass(frozen=True)
class Finding:
    """One detector hit. Carries a label and a mask, never the raw value."""

    detector: str
    rule: str
    data_class: DataClass
    confidence: float
    # Exact matched text, kept only in-process so the redactor can replace it.
    # It is never written to the ledger, an audit row or a founder-facing line.
    value: str = field(default="", repr=False, compare=False)
    label: str = ""

    @property
    def masked(self) -> str:
        """A length-preserving hint that identifies the hit without leaking it."""
        n = len(self.value)
        if n <= 4:
            return "*" * n
        return f"{self.value[:2]}{'*' * (n - 4)}{self.value[-2:]}"

    @property
    def forbidden(self) -> bool:
        return self.data_class in FORBIDDEN


@dataclass(frozen=True)
class Segment:
    """A piece of a payload with whatever provenance the caller could supply."""

    text: str
    path: str | None = None
    # first_party: the founder or their granted systems. third_party: anything
    # we read from the outside world, which is data and never instruction.
    origin: str = "first_party"
    declared_class: str | None = None
    label: str = "input"
    published_contact_email: str | None = field(default=None, repr=False)

    def display(self) -> str:
        return self.path or self.label


@functools.lru_cache(maxsize=1)
def policy() -> dict[str, Any]:
    """Load and cache config/boundary.yaml."""
    import yaml

    with POLICY_PATH.open() as handle:
        return yaml.safe_load(handle) or {}


def threshold(name: str, default: float) -> float:
    value = (policy().get("thresholds") or {}).get(name)
    return float(value) if value is not None else default


def may_reach_model(data_class: DataClass | str) -> bool:
    """The class table in config/boundary.yaml is the readable source of truth."""
    entry = (policy().get("classes") or {}).get(str(data_class))
    if entry is None:
        return False
    return bool(entry.get("may_reach_model", False))


def worst(classes: Iterable[DataClass | str]) -> DataClass:
    """The most severe class present, or `unknown` when nothing is known."""
    present = [DataClass(c) for c in classes]
    if not present:
        return DataClass.UNKNOWN
    return max(present, key=lambda c: SEVERITY[c])


__all__ = [
    "ALL_CLASSES",
    "BoundaryViolation",
    "DataClass",
    "FORBIDDEN",
    "Finding",
    "NARRATION_KEYS",
    "PLACEHOLDER",
    "SENDABLE",
    "SEVERITY",
    "Segment",
    "may_reach_model",
    "policy",
    "threshold",
    "worst",
]
