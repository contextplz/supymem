from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.actuation.contract import ActuationMode, ActuationResult  # noqa: E402
from app.actuation.execute import execute  # noqa: E402
from app.actuation.registry import ToolSpec, register  # noqa: E402


@pytest.mark.asyncio
async def test_dry_run_prevents_handler_and_returns_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def would_call_network(venture_id: str, payload: dict[str, object]) -> ActuationResult:
        raise AssertionError("handler must not run while CHANNELS_DRY_RUN is enabled")

    tool_id = "test.network.blocked"
    register(ToolSpec(tool_id=tool_id, handler=would_call_network))
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")

    result = await execute(tool_id, "ven_test", {"message": "hello"}, allow_live=True)

    assert result.mode is ActuationMode.SIMULATED
    assert result.reason == "dry_run_env"
    assert result.dry_run is True
