"""Result / Ok / Err and Verdict types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    value: T

    @property
    def ok(self) -> bool:
        return True

    def unwrap(self) -> T:
        return self.value


@dataclass(frozen=True, slots=True)
class Err(Generic[E]):
    error: E

    @property
    def ok(self) -> bool:
        return False

    def unwrap(self) -> T:  # type: ignore[type-var]
        raise ValueError(f"called unwrap on Err: {self.error!r}")


Result = Ok[T] | Err[E]


@dataclass(frozen=True, slots=True)
class Verdict:
    """Allow / deny / escalate style decision (warden, capability floor, etc.)."""

    decision: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "proceed")
