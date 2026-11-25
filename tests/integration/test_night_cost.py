from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.cycle import run_cycle


@pytest.mark.asyncio
async def test_healthy_night_under_one_cent():
    total = 0.0
    for _ in range(8):
        res = await run_cycle({"id": "ven", "engagement_series": [1, 1], "feedback_topics": {}})
        total += res.cost_usd
    assert total < 0.01
