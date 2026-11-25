"""A6 — Autonomous evolution cycle offline."""

from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.archive import clear_archive, list_archive
from app.evolution.cycle import run_cycle
from app.evolution.promote import clear_promotions, promotions
from app.evolution.watch import check_non_degradation
from app.org.lifecycle import atomic_replace
from app.org.registry import registry


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_a6_evolution_and_auto_rollback():
    clear_archive(); clear_promotions()
    venture = {
        "id": "ven_a6",
        "engagement_series": [1.0, 0.9, 0.8, 0.7, 0.3],
        "feedback_topics": {"tone": 6},
        "operator_id": "op_ig",
    }
    res = await run_cycle(venture, budget_tokens=50_000)
    assert res.findings >= 1
    assert res.cost_usd <= 0.01
    # atomic org swap demo
    old = registry.create_agent_from_spec(
        venture_id="ven_a6", role="channel_operator", name="Old", charge="c",
        system_prompt="s", allowed_tools=["emit_signal"], model_tier="flash",
        fitness_metric="engagement_rate", retire_when="never", status="active",
    )
    atomic_replace(retire_id=old.id, new_spec_kwargs={
        "venture_id": "ven_a6", "role": "channel_operator", "name": "New", "charge": "c",
        "system_prompt": "s2", "allowed_tools": ["emit_signal"], "model_tier": "flash",
        "fitness_metric": "engagement_rate", "retire_when": "never",
    })
    # inject regression
    promos = [{
        "id": "p1", "status": "observing", "proposal_id": "prop", "target_kind": "org",
        "baseline": {"metric": "engagement_rate", "value": 0.5},
        "hypothesis": str({"kind": "org_replace"}),
    }]
    acts = check_non_degradation(promos, {"engagement_rate": 0.1, "_samples": 50})
    assert acts
    assert any(a.get("outcome") == "regressed" for a in list_archive())
