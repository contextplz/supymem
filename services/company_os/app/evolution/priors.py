"""Installation-wide evolution priors containing outcome counts only."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt


@dataclass
class EvolutionPrior:
    key: str
    attempts: int = 0
    held: int = 0
    regressed: int = 0
    inconclusive: int = 0

    @property
    def prior_success(self) -> float:
        """Wilson lower bound: conservative ranking without tenant metrics."""
        if not self.attempts:
            return 0.0
        z = 1.96
        proportion = self.held / self.attempts
        denominator = 1 + z**2 / self.attempts
        centre = proportion + z**2 / (2 * self.attempts)
        margin = z * sqrt(
            proportion * (1 - proportion) / self.attempts + z**2 / (4 * self.attempts**2)
        )
        return (centre - margin) / denominator

    def record(self, outcome: str) -> None:
        if outcome not in {"held", "regressed", "inconclusive"}:
            raise ValueError(f"unknown evolution outcome: {outcome}")
        self.attempts += 1
        setattr(self, outcome, getattr(self, outcome) + 1)

    def counts(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "held": self.held,
            "regressed": self.regressed,
            "inconclusive": self.inconclusive,
        }


@dataclass
class EvolutionPriorStore:
    _priors: dict[str, EvolutionPrior] = field(default_factory=dict)

    def record(self, key: str, outcome: str) -> EvolutionPrior:
        prior = self._priors.setdefault(key, EvolutionPrior(key=key))
        prior.record(outcome)
        return prior

    def get(self, key: str) -> EvolutionPrior | None:
        return self._priors.get(key)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {key: prior.counts() for key, prior in self._priors.items()}


priors = EvolutionPriorStore()
