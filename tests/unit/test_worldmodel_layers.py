from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.worldmodel.actions import Action
from app.worldmodel.simulate import simulate


@pytest.mark.asyncio
async def test_rules_covered_never_reaches_llm():
    calls = {"n": 0}

    async def llm(*a, **k):
        calls["n"] += 1
        raise AssertionError("llm should not be called")

    sim = await simulate(
        "ven",
        Action(kind="noop"),
        llm_estimate=llm,
    )
    assert calls["n"] == 0
    assert sim.method_mix.get("rules", 0) >= 1
