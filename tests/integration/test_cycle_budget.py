from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.cycle import run_cycle


@pytest.mark.asyncio
async def test_cycle_stops_at_token_ceiling():
    venture = {
        "id": "ven",
        "engagement_series": [10, 9, 8, 7, 2],
        "feedback_topics": {"tone": 6},
        "operator_id": "op1",
    }
    res = await run_cycle(venture, budget_tokens=100)  # too small for propose loop after diagnose
    assert res.reason == "token_budget" or res.tokens_used <= 100
