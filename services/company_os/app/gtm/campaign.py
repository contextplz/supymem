"""Campaign model — falsifiable hypothesis required."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.ids import new_id


@dataclass
class Campaign:
    id: str
    venture_id: str
    hypothesis: str
    metric: str
    threshold: float
    window_days: int
    channels: list[str]
    status: str = "planned"
    armed: bool = False


def create_campaign(
    venture_id: str,
    *,
    hypothesis: str,
    metric: str,
    threshold: float | None,
    window_days: int,
    channels: list[str],
) -> Campaign:
    if not hypothesis or not metric or threshold is None or window_days <= 0:
        raise ValueError("campaign requires falsifiable hypothesis, metric, threshold, window")
    # Must be falsifiable: metric + threshold is the decision rule
    return Campaign(
        id=new_id("camp"),
        venture_id=venture_id,
        hypothesis=hypothesis,
        metric=metric,
        threshold=float(threshold),
        window_days=window_days,
        channels=channels,
    )


def evaluate_campaign(camp: Campaign, observed: float) -> dict[str, Any]:
    if observed >= camp.threshold:
        return {"decision": "continue", "observed": observed}
    return {"decision": "pivot", "observed": observed, "learning": f"{camp.metric} missed {camp.threshold}"}
