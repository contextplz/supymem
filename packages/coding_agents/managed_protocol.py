"""Process boundary contracts for the first managed OpenHands profile.

Core remains the sole receipt, generation, credit and cursor authority. An
implementation of IntentClient may cross the private sandbox/controller link;
it must never serialize or hand CoreAuthority or its credential into a task.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Protocol


class ManagedInvocationError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class SyncResult:
    cursor: str | None
    consumed_invocations: frozenset[str]


class IntentClient(Protocol):
    """Sandbox-scoped capability, bound to ONE build/conversation at issue time."""

    def synchronize(self) -> SyncResult: ...

    def prepare(self, *, invocation_key: str, input_event_id: str | None,
                request: dict) -> dict: ...


class ModelClient(Protocol):
    def complete(self, invocation_id: str, request: dict) -> dict: ...


class CoreAuthority(Protocol):
    """Trusted controller-only adapter; run/generation/controller fence is bound.

    All methods must enforce the current fence, authorization and active pool.
    Mirror is idempotent by event ID + payload hash. Consuming an old receipt is
    idempotent without moving the cursor backwards. These guarantees live in
    Core's transactions, not in this Python object's lifetime.
    """

    def register(self, *, invocation_key: str, input_event_id: str | None,
                 request: dict) -> dict: ...

    def read_receipt(self, invocation_id: str) -> dict: ...

    def mirror(self, event: dict) -> int: ...

    def consume(self, invocation_id: str, *, mirrored_event_seq: int) -> None: ...

    def advance_cursor(self, *, event_id: str | None) -> bool:
        """False only while a receipt is outstanding; every other error raises."""
        ...
