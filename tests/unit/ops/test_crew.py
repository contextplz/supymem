"""AGENT_MAP covers all ten Polsia agent types."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ops_crew.db")
    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()


def test_all_ten_agents_run_under_mock(ops_env) -> None:
    from app.ops import VALID_AGENT_TYPES
    from app.ops.crew import AGENT_MAP, run_agent_for_task

    assert set(AGENT_MAP) == set(VALID_AGENT_TYPES)
    for agent_type in VALID_AGENT_TYPES:
        result = run_agent_for_task(
            agent_type, {"title": f"test {agent_type}"}, {"company": {"name": "Acme"}}
        )
        assert "summary" in result
        assert "duration_secs" in result


def test_unknown_agent_is_rejected(ops_env) -> None:
    from app.ops.crew import run_agent_for_task

    with pytest.raises(ValueError):
        run_agent_for_task("not_an_agent", {}, {})
