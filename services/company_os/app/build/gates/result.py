from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""

    def run(self, _ctx: dict[str, Any] | None = None) -> GateResult:
        return self
