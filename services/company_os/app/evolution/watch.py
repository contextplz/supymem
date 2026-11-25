"""Non-degradation watch — auto-rollback on regression."""

from __future__ import annotations

from typing import Any

from app.evolution.archive import archive
from app.evolution.rollback import rollback
from app.feedback.fitness import resolve_metric


def check_non_degradation(
    promotions: list[dict[str, Any]],
    observed: dict[str, float],
) -> list[dict[str, Any]]:
    actions = []
    for p in promotions:
        if p.get("status") != "observing":
            continue
        m = (p.get("baseline") or {}).get("metric")
        if not m or m not in observed:
            continue
        meta = resolve_metric(m)
        if observed.get("_samples", meta.get("min_sample", 1)) < meta.get("min_sample", 1):
            continue
        base = float((p.get("baseline") or {}).get("value") or 0)
        obs = float(observed[m])
        direction = meta.get("direction", "higher_is_better")
        thr = float(meta.get("regression_threshold") or 0.2)
        if direction == "higher_is_better":
            delta = (obs - base) / max(abs(base), 1e-6)
            regressed = delta < -thr
        else:
            delta = (base - obs) / max(abs(base), 1e-6)
            regressed = delta < -thr  # worse if higher when lower_is_better... simplify:
            regressed = obs > base * (1 + thr)
        if regressed:
            rollback(p, reason=f"regression:{m}")
            archive(
                {
                    "hypothesis": p.get("proposal_id"),
                    "predicted_metric": m,
                    "tier": p.get("target_kind"),
                    "outcome": "regressed",
                }
            )
            p["proposal_status"] = "regressed"
            actions.append({"promotion_id": p.get("id"), "action": "auto_rollback"})
    return actions
