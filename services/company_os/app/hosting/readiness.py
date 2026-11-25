"""Production readiness gate."""
from __future__ import annotations
from typing import Any

def readiness_report(ctx: dict[str, Any]) -> dict[str, Any]:
    blocking = list(ctx.get("blocking_issues") or [])
    score = float(ctx.get("readiness_score", 0.0 if blocking else 1.0))
    return {"ok": score >= 0.8 and not blocking, "score": score, "blocking": blocking}
