"""GTM campaign planning — product-first vs market-first."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[4]
MANIFEST = REPO / "config" / "channel_manifests.yaml"


@lru_cache(maxsize=1)
def load_manifests() -> dict[str, Any]:
    with MANIFEST.open(encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("channels") or {}


@dataclass
class PlanResult:
    ok: bool
    projected_cost_usd: float
    channels: list[str]
    earliest_start_days: int
    human_tasks: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    line_items: list[dict[str, Any]] = field(default_factory=list)


def plan_campaign(
    *,
    channels: list[str],
    actions_per_channel: dict[str, int],
    budget_usd: float,
    gtm_order: str = "product_first",
    include_links_on_x: bool = False,
) -> PlanResult:
    manifests = load_manifests()
    total = 0.0
    lead = 0
    tasks: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for ch in channels:
        m = manifests.get(ch)
        if not m:
            return PlanResult(ok=False, projected_cost_usd=0, channels=channels, earliest_start_days=0, reason=f"unknown_channel:{ch}")
        n = int(actions_per_channel.get(ch, 1))
        unit = float(m.get("cost_per_action_usd") or 0)
        if ch == "x" and include_links_on_x:
            unit = float(m.get("cost_with_link_usd") or unit)
        cost = unit * n
        total += cost
        lines.append({"channel": ch, "n": n, "unit": unit, "cost": cost})
        lead = max(lead, int(m.get("approval_lead_days") or 0))
        for prereq in m.get("prerequisites") or []:
            tasks.append(
                {
                    "channel": ch,
                    "prereq": prereq,
                    "title": f"Complete {prereq} for {ch}",
                    "est_days": int(m.get("approval_lead_days") or 0),
                }
            )
        if ch == "instagram" and "app_review" in (m.get("prerequisites") or []):
            tasks.append(
                {
                    "channel": "instagram",
                    "prereq": "app_review",
                    "title": "Meta App Review for Instagram",
                    "est_days": 21,
                }
            )
    if total > budget_usd:
        return PlanResult(
            ok=False,
            projected_cost_usd=total,
            channels=channels,
            earliest_start_days=lead,
            human_tasks=tasks,
            reason="over_budget",
            line_items=lines,
        )
    return PlanResult(
        ok=True,
        projected_cost_usd=total,
        channels=channels,
        earliest_start_days=lead,
        human_tasks=tasks,
        line_items=lines,
    )
