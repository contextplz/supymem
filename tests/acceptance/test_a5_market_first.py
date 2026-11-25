"""A5 — Market-first demand test planning."""

from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.campaign import create_campaign, evaluate_campaign
from app.gtm.planner import plan_campaign
from app.gtm.scheduler import schedule_post


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_a5_market_first_plan():
    over = plan_campaign(channels=["landing", "reddit"], actions_per_channel={"landing": 1, "reddit": 1}, budget_usd=-0.01)
    # landing+reddit cost 0 so use x with links for over budget
    over = plan_campaign(channels=["x"], actions_per_channel={"x": 100}, budget_usd=0.5, include_links_on_x=True)
    assert over.ok is False and over.reason == "over_budget"
    xplan = plan_campaign(channels=["x"], actions_per_channel={"x": 1}, budget_usd=10, include_links_on_x=True)
    assert xplan.line_items[0]["unit"] == 0.20
    ig = plan_campaign(channels=["instagram"], actions_per_channel={"instagram": 1}, budget_usd=10)
    assert ig.earliest_start_days >= 21
    res = await schedule_post("reddit", {"text": "hi"}, armed=False)
    assert res["ok"] is False
    camp = create_campaign("ven", hypothesis="cvr>=2%", metric="cvr", threshold=0.02, window_days=14, channels=["landing", "reddit"])
    decision = evaluate_campaign(camp, 0.005)
    assert decision["decision"] == "pivot"
