from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EstimateLine:
    meter: str
    quantity: float
    micro: int
    confidence: float
    basis: str


@dataclass
class ResourceEstimate:
    lines: list[EstimateLine] = field(default_factory=list)
    offsets: list[dict] = field(default_factory=list)
    gross_upfront_micro: int = 0
    total_upfront_micro: int = 0
    first_month_run_micro: int = 0
    monthly_run_micro: int = 0

    @property
    def total_upfront_credits(self) -> float:
        return self.total_upfront_micro / 10_000
