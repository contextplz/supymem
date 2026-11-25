"""Architecture Decision Records. A locked ADR is required before dispatch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

_ADRS: dict[str, "ADR"] = {}
_SEQ = 0


@dataclass
class ADR:
    id: str
    venture_id: str
    title: str
    stack: list[str] = field(default_factory=list)
    protected_routes: list[str] = field(default_factory=list)
    components_estimate: int = 0
    status: str = "draft"
    checksum: str = ""

    def _rehash(self) -> None:
        payload = {
            "venture_id": self.venture_id,
            "title": self.title,
            "stack": self.stack,
            "protected_routes": self.protected_routes,
            "components_estimate": self.components_estimate,
        }
        self.checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


def _next_id() -> str:
    global _SEQ
    _SEQ += 1
    return f"adr_{_SEQ}"


def clear_adrs() -> None:
    _ADRS.clear()


def create_adr(
    venture_id: str,
    title: str = "",
    stack: list[str] | None = None,
    protected_routes: list[str] | None = None,
    components_estimate: int = 0,
    **_: Any,
) -> ADR:
    adr = ADR(
        id=_next_id(),
        venture_id=venture_id,
        title=title or "ADR",
        stack=list(stack or []),
        protected_routes=list(protected_routes or []),
        components_estimate=int(components_estimate or 0),
        status="draft",
    )
    adr._rehash()
    _ADRS[adr.id] = adr
    return adr


def lock_adr(adr_id: str) -> ADR:
    adr = _ADRS[adr_id]
    adr.status = "locked"
    adr._rehash()
    return adr


def locked_adr_for_venture(venture_id: str) -> ADR | None:
    locked = [
        a
        for a in _ADRS.values()
        if a.venture_id == venture_id and a.status == "locked"
    ]
    return locked[-1] if locked else None
