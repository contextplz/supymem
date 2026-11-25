"""Offline dispatch never discovers or invokes external coding providers."""
import pytest

from coding_agents import dispatcher as d
from coding_agents.protocol import CodingRequest
from coding_agents.select import Selection


@pytest.fixture(autouse=True)
def hosted_dispatch(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["dispatch", "dispatch_request"])
async def test_offline_dispatch_refuses_before_selection_or_filesystem(tmp_path, monkeypatch, entry):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    def forbidden(*args, **kwargs):
        pytest.fail("offline dispatch admitted an external or filesystem boundary")
    for name in ("detect", "select", "create_worktree", "load_adapter"):
        monkeypatch.setattr(d, name, forbidden)
    if entry == "dispatch":
        result = await d.dispatch(prompt="synthetic task", repo=tmp_path, branch="wo/offline")
    else:
        result = await d.dispatch_request(CodingRequest(prompt="synthetic task", workdir=tmp_path))
    assert result.ok is False and result.error == "llm_offline"
    assert result.argv == [] and result.changed_files == []


@pytest.mark.asyncio
async def test_live_disabled_does_not_invoke_unrecorded_local_adapter(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    class Adapter:
        recorded = None
        async def run(self, req):
            pytest.fail("unrecorded local adapter can spawn live even through run()")
    monkeypatch.setattr(d, "load_adapter", lambda name: Adapter())
    result = await d._run_selection(Selection(adapter_name="opencode", mode="local", reason="synthetic"),
        CodingRequest(prompt="synthetic task", workdir=tmp_path), transcript_dir=None)
    assert result.ok is False and result.error == "live_not_enabled"
