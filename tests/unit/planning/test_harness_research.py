"""Research tasks use the harness loop when an LLM is injected."""

from __future__ import annotations

import json
import sys

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/harness.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("HARNESS_DISABLE", "0")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def _venture(client: TestClient) -> str:
    created = client.post(
        "/ventures",
        json={"raw_idea": "neighborhood compost waitlist"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehousing.example", "camping.example"])
async def test_missing_optional_harness_allows_research_fallback_and_chat(monkeypatch, domain):
    load_company_os_app()
    monkeypatch.setitem(sys.modules, "harness", None)
    from app.planning.execute import _harness_research
    from app.runtime.live_turns import is_live, steer_live

    assert not is_live(domain) and not steer_live(domain, "Keep researching")
    assert await _harness_research(venture_id=domain, title="Research", detail=domain,
                                   speaker=None, workspace_id=None, llm=object()) is None


@pytest.mark.asyncio
async def test_harness_research_uses_explore_then_writes_note(client, monkeypatch):
    vid = _venture(client)
    from app.planning.execute import _harness_research
    from app.research import explore as explore_mod

    async def fake_explore(query: str, **kwargs):
        return {
            "query": query,
            "mode": "live",
            "hits": [{"url": "https://example.com/compost", "title": "Compost"}],
            "pages": [
                {
                    "url": "https://example.com/compost",
                    "title": "Compost",
                    "excerpt": "City pickup on Tuesdays.",
                    "chars": len("City pickup on Tuesdays."),
                }
            ],
            "summary": "One source.",
        }

    monkeypatch.setattr(explore_mod, "explore_web", fake_explore)

    n = {"i": 0}

    async def llm(messages, **kwargs):
        n["i"] += 1
        if n["i"] == 1:
            return {
                "role": "assistant",
                "content": None,
                "reasoning_content": "search",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "explore_web",
                            "arguments": json.dumps({"query": "compost waitlist"}),
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": "City pickup on Tuesdays. Gaps: pricing, who pays.",
        }

    from app.core.warden_client import CheckResponse, WardenClient

    class AllowWarden(WardenClient):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:9")

        async def check(self, **kwargs):
            return CheckResponse(decision="allow", reason="ok")

        async def kill_switch(self) -> bool:
            return False

    notes = await _harness_research(
        venture_id=vid,
        title="Compare compost waitlist alternatives",
        detail="Who already runs this in US cities?",
        speaker={"id": "ag_r", "name": "Maya", "title": "Researcher", "role": "researcher"},
        workspace_id=None,
        llm=llm,
        warden=AllowWarden(),
    )
    assert notes is not None
    assert notes.qualified is True
    assert "Tuesdays" in notes.text
    assert "Gaps" in notes.text
    assert n["i"] >= 2


@pytest.mark.asyncio
async def test_offline_research_falls_back_without_llm(client):
    from app.planning.execute import harness_enabled

    # Fixture sets LLM_OFFLINE=1
    assert harness_enabled() is False
