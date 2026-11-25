"""Persist only statistically-gated insights through MemoryPort."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Iterable

from app.memory.model import MemoryKind, MemoryWrite
from app.memory.port import MemoryPort
from app.signals.gate import CandidateClaim, GateResult, Signal, evaluate


async def write_insight(
    *,
    memory: MemoryPort,
    venture_id: str,
    claim: CandidateClaim,
    signals: Iterable[Signal],
    kind: str = "market_feedback",
) -> tuple[str | None, GateResult]:
    rows = list(signals)
    gate = evaluate(claim, rows)
    if not gate.passed:
        return None, gate
    evidence = [signal.id for signal in rows]
    digest = hashlib.sha256(f"{venture_id}:{claim.statement}:{evidence}".encode()).hexdigest()
    mem_id = await memory.remember(
        MemoryWrite(
            venture_id=venture_id,
            kind=MemoryKind.INSIGHT,
            subject=kind,
            statement=claim.statement,
            detail=json.dumps(asdict(gate), default=str, sort_keys=True),
            evidence=evidence,
            confidence=gate.confidence,
            dedupe_key=f"insight:{digest}",
            tags=["gated", kind],
        )
    )
    return mem_id, gate
