from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.cycle import run_cycle


@pytest.mark.asyncio
async def test_healthy_cycle_zero_pro():
    res = await run_cycle({"id": "ven", "engagement_series": [1, 1, 1], "feedback_topics": {}})
    assert res.pro_calls == 0
    assert res.cost_usd == 0.0
    assert "healthy" in res.note
