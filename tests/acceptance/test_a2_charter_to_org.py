"""A2 — Org derivation varies with Charter."""

from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.composer import compose


@pytest.mark.acceptance
def test_a2_agent_counts_differ():
    charters = [
        {"gtm_order": "product_first", "build_archetype": "crud_saas", "monetization": "free", "regulatory_surface": "none", "work_order_tracks": 1, "current_stage": "org_planning", "armed_channels": 0},
        {"gtm_order": "product_first", "build_archetype": "crud_saas", "monetization": "subscription", "regulatory_surface": "none", "work_order_tracks": 2, "current_stage": "org_planning", "armed_channels": 0, "integrations": ["wa"]},
        {"gtm_order": "product_first", "build_archetype": "marketplace", "monetization": "take_rate", "regulatory_surface": "health", "work_order_tracks": 3, "current_stage": "org_planning", "armed_channels": 0, "integrations": ["a", "b", "c", "d"]},
    ]
    briefs = [{"complexity_score": 0.25}, {"complexity_score": 0.5}, {"complexity_score": 0.85}]
    counts = []
    for ch, br in zip(charters, briefs):
        plan = compose(ch, br)
        counts.append(plan.agent_count)
        assert len([r for r in plan.roles if r.role == "orchestrator"]) == 1
        assert all(r.fitness_metric and r.retire_when for r in plan.roles)
    assert counts[0] < counts[1] <= counts[2]
    assert counts[0] <= 8
    assert counts[2] <= 12
