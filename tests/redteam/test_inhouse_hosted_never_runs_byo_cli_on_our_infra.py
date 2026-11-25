from __future__ import annotations

import pytest

from coding_agents import load_adapter
from coding_agents.consent import clear_consents, record_consent
from coding_agents.dispatcher import dispatch_request
from coding_agents.protocol import CodingRequest


@pytest.mark.asyncio
async def test_cloud_env_skips_local_byo(tmp_path, monkeypatch):
    from coding_agents import dispatcher
    from coding_agents.detect import DetectedAgent
    from coding_agents.inhouse import InhouseAdapter

    monkeypatch.setenv("COMPANY_OS_ENV", "cloud")
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    clear_consents()
    record_consent("inhouse")
    # Import guard for direct load
    with pytest.raises(RuntimeError):
        load_adapter("cursor")

    # Exercise selection independently of the canonical offline dispatch fence.
    # The only loadable test adapter is a source-owned deterministic marker;
    # PATH discovery, provider selection and subprocess execution are closed.
    loaded = []

    def closed_factory(name):
        loaded.append(name)
        assert name == "inhouse", "test attempted a real/provider adapter"
        return InhouseAdapter()

    def no_subprocess(*args, **kwargs):
        pytest.fail("hosted selection test must not launch a coding subprocess")

    monkeypatch.setattr(dispatcher, "load_adapter", closed_factory)
    monkeypatch.setattr(dispatcher, "detect", lambda: [DetectedAgent("cursor", "/synthetic/cursor", "test", "unknown")])
    monkeypatch.setattr(dispatcher, "_cloud_keys_present", lambda: {})
    monkeypatch.setattr(dispatcher.subprocess, "run", no_subprocess)
    monkeypatch.setattr(dispatcher.subprocess, "Popen", no_subprocess)
    monkeypatch.delenv("CODING_AGENT", raising=False)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    req = CodingRequest(prompt="x", workdir=tmp_path, allow_write=True)
    result = await dispatch_request(
        req,
        prefs=["cursor", "inhouse"],
        mode="auto",
        bridge_connected=True,
        allow_server_side_byo=False,
    )
    # Must land on inhouse, never pretend cursor ran on our infra.
    assert result.ok
    assert result.argv and result.argv[0] == "inhouse-coder"
    assert loaded == ["inhouse"]
    assert result.changed_files == ["inhouse_change.txt"]
    assert (tmp_path / "inhouse_change.txt").read_text() == "ok\n"


@pytest.mark.asyncio
async def test_offline_cloud_dispatch_never_reaches_adapter_or_detection(tmp_path, monkeypatch):
    from coding_agents import dispatcher

    def unavailable(*args, **kwargs):
        pytest.fail("offline dispatch crossed the effect admission fence")

    monkeypatch.setenv("COMPANY_OS_ENV", "cloud")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setattr(dispatcher, "detect", unavailable)
    monkeypatch.setattr(dispatcher, "load_adapter", unavailable)
    monkeypatch.setattr(dispatcher.subprocess, "run", unavailable)
    monkeypatch.setattr(dispatcher.subprocess, "Popen", unavailable)
    result = await dispatch_request(CodingRequest(prompt="x", workdir=tmp_path, allow_write=True),
                                    prefs=["cursor", "inhouse"])
    assert not result.ok and result.error == "llm_offline"
    assert result.argv == [] and result.changed_files == []
    assert not list(tmp_path.iterdir())
