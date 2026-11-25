"""A concept page must not silently start a separately billed coding agent."""

import sys
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.mark.parametrize("mode", [None, "compose", "offline", "fallback"])
@pytest.mark.parametrize("idea", ["Inventory planning for distributors", "Weeknight meals for families"])
async def test_coding_agent_requires_explicit_engine_choice(monkeypatch, mode, idea):
    from app.core.local_mode import is_local
    from app.firstlook.compose import try_code_html
    from app.firstlook.service import FirstLookError

    def forbidden(*args, **kwargs):
        raise AssertionError("unqualified coding agent reached")

    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.delenv("FIRSTLOOK_ENGINE", raising=False)
    if mode is not None:
        monkeypatch.setenv("FIRSTLOOK_ENGINE", mode)
    monkeypatch.setitem(sys.modules, "coding_agents", SimpleNamespace(load_adapter=forbidden))
    monkeypatch.setitem(sys.modules, "coding_agents.detect", SimpleNamespace(detect=forbidden))
    monkeypatch.setitem(sys.modules, "coding_agents.protocol", SimpleNamespace(CodingRequest=forbidden))
    request = try_code_html(brand={}, copy={}, idea_text=idea, version=1,
        stage="first_look", interest_action="", variant_index=0, archetype=None,
        logo_url=None)
    if is_local():
        with pytest.raises(FirstLookError) as error:
            await request
        assert error.value.code == "page_venture_required"
        assert "Open Website for your venture" in error.value.message
        assert "connected coding tool" in error.value.message
    else:
        assert await request is None
