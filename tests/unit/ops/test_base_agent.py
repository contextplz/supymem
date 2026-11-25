"""BaseOpsAgent mock / no-subprocess (Polsia test_base_agent contract)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ops_base.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    load_company_os_app()


class _Concrete:
    pass


def test_call_claude_returns_mock_when_env_set(ops_env, monkeypatch) -> None:
    from app.ops.base_agent import BaseOpsAgent

    class ConcreteAgent(BaseOpsAgent):
        agent_type = "test"

        def run(self, task, context):
            return {"summary": "test"}

    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": "hello from mock"})
    )
    agent = ConcreteAgent()
    assert agent.call_claude("test prompt") == "hello from mock"


def test_call_claude_does_not_invoke_subprocess_in_mock_mode(ops_env, monkeypatch) -> None:
    from app.ops.base_agent import BaseOpsAgent

    class ConcreteAgent(BaseOpsAgent):
        agent_type = "test"

        def run(self, task, context):
            return {"summary": "test"}

    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    agent = ConcreteAgent()
    with patch("subprocess.run") as mock_run:
        agent.call_claude("test prompt")
        mock_run.assert_not_called()


def test_call_claude_json_parses_result(ops_env, monkeypatch) -> None:
    from app.ops.base_agent import BaseOpsAgent

    class ConcreteAgent(BaseOpsAgent):
        agent_type = "test"

        def run(self, task, context):
            return {"summary": "test"}

    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": '{"key": "value", "number": 42}'}),
    )
    agent = ConcreteAgent()
    result = agent.call_claude_json("test")
    assert result == {"key": "value", "number": 42}


def test_timed_run_adds_duration(ops_env) -> None:
    from app.ops.base_agent import BaseOpsAgent

    class ConcreteAgent(BaseOpsAgent):
        agent_type = "test"

        def run(self, task, context):
            return {"summary": "test"}

    agent = ConcreteAgent()
    result = agent.timed_run({"title": "test"}, {})
    assert "duration_secs" in result
    assert isinstance(result["duration_secs"], float)
