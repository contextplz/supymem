import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "company_os"))
from app.credits.estimate import actual_llm_micro, estimate_llm_micro, priority_multiplier


def test_priority_prices_at_the_multiplier(monkeypatch):
    monkeypatch.delenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", raising=False)
    base = actual_llm_micro("gpt-5.6-luna", prompt_tokens=100_000, completion_tokens=10_000)
    prio = actual_llm_micro("gpt-5.6-luna", prompt_tokens=100_000, completion_tokens=10_000, priority=True)
    assert base > 0 and prio == pytest.approx(2 * base, abs=2)
    q = estimate_llm_micro("gpt-5.6-luna", prompt_tokens=100_000, max_tokens=2500)
    assert estimate_llm_micro("gpt-5.6-luna", prompt_tokens=100_000, max_tokens=2500, priority=True) == pytest.approx(2 * q, abs=2)


def test_multiplier_is_env_tunable_and_bounded(monkeypatch):
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "1.5")
    assert float(priority_multiplier()) == 1.5
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "0.1")
    assert float(priority_multiplier()) == 1
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "junk")
    assert float(priority_multiplier()) == 2


def test_step_flag_is_env_driven(monkeypatch):
    from app.build import device_ai_step as step
    monkeypatch.delenv("SUPYMEM_CU_PRIORITY_TIER", raising=False)
    assert step.priority_tier() is False
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_TIER", "1")
    assert step.priority_tier() is True
