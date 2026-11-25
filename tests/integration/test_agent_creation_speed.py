"""Agent creation under 1s wall clock."""

from __future__ import annotations

import time

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.registry import SpecRegistry


def test_create_agent_under_one_second():
    reg = SpecRegistry()
    t0 = time.perf_counter()
    for i in range(20):
        reg.create_agent_from_spec(
            venture_id="ven_speed",
            role="builder",
            name=f"Builder {i}",
            charge="ship WOs",
            system_prompt="build",
            allowed_tools=["propose_code"],
            model_tier="pro",
            fitness_metric="wo_throughput_per_day",
            retire_when="done",
        )
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, elapsed
