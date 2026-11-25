from __future__ import annotations

from pathlib import Path
import asyncio
import json
import sys

import psutil
import pytest

from coding_agents import load_adapter
from coding_agents.detect import detect
from coding_agents.protocol import AgentEventKind, CodingRequest
from coding_agents.subprocess_runner import run_live


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
@pytest.mark.parametrize("provider", ["codex", "claude", "cursor", "opencode"])
async def test_local_tools_share_the_owned_supervisor_and_callbacks(tmp_path, monkeypatch, domain, provider):
    from coding_agents import subprocess_runner
    from coding_agents.protocol import AgentEvent, AgentEventKind, CodingResult
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(agent_status, "_status", lambda *args: {"available": False})
    received = []
    async def receive(event):
        received.append(event)
    stop = lambda: False
    transcript = tmp_path / "stream.ndjson"
    expected = CodingResult(ok=True, changed_files=["index.html"])
    async def owned(**options):
        assert options["adapter"].name == provider
        assert options["req"].prompt == domain
        assert options["transcript_path"] == transcript
        assert options["kill_switch"] is stop
        await options["on_event"](AgentEvent(kind=AgentEventKind.STARTED, text=domain))
        return expected
    monkeypatch.setattr(subprocess_runner, "_run_owned", owned)
    result = await subprocess_runner.run_live(adapter=load_adapter(provider),
        req=CodingRequest(prompt=domain, workdir=tmp_path), transcript_path=transcript,
        on_event=receive, kill_switch=stop)
    assert result is expected
    assert [event.text for event in received] == [domain]


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
@pytest.mark.parametrize("provider,events,passed", [
    ("cursor", [{"type": "result", "subtype": "success", "is_error": False}], True),
    ("cursor", [{"type": "error"}, {"type": "result", "subtype": "success", "is_error": False}], False),
    ("opencode", [{"type": "step_finish", "part": {"reason": "stop"}}], True),
    ("opencode", [{"type": "tool_use", "part": {"state": {"status": "error"}}},
                  {"type": "step_finish", "part": {"reason": "stop"}}], False),
])
def test_connected_tools_require_their_own_terminal_success(domain, provider, events, passed):
    from desktop_runtime.owned_coding import provider_outcome

    lines = [{"type": "text", "text": domain}, *events]
    output = b"\n".join(json.dumps(event).encode() for event in lines)
    assert provider_outcome(output, provider)["outcome"] == ("pass" if passed else "cli_error")


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
@pytest.mark.parametrize("event,outcome", [
    ({"type": "error", "error": {"name": "APIError", "data": {"statusCode": 429}}}, "quota_paused"),
    ({"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed"}}}, "sandbox_denied"),
])
def test_opencode_refuses_unexpected_tools_and_pauses_at_account_limit(domain, event, outcome):
    from desktop_runtime.owned_coding import provider_outcome

    lines = [event, {"type": "text", "text": domain}, {"type": "step_finish", "part": {"reason": "stop"}}]
    assert provider_outcome(b"\n".join(json.dumps(line).encode() for line in lines), "opencode")["outcome"] == outcome


@pytest.fixture
def owned_workspace(tmp_path, monkeypatch):
    from desktop_runtime import agent_status, owned_coding
    from types import SimpleNamespace

    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "Documents/Supymem/ventures/fixture"
    workspace.mkdir(parents=True)
    (workspace / ".supymem-venture").write_text("ven_fixture\n")
    owned_coding._trusted_git(workspace, "init", "--quiet", "--initial-branch=main")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(enabled=lambda name: True))
    monkeypatch.setattr(owned_coding.OwnedCoding, "preflight", lambda coding: agent_status.tool_binary(coding.provider))
    monkeypatch.setattr(owned_coding.OwnedCoding, "_provider_version", lambda *args: "fixture")
    monkeypatch.setattr(owned_coding.OwnedCoding, "_argv", lambda coding, binary, instruction, workspace:
        load_adapter(coding.provider).build_argv(CodingRequest(prompt=instruction, workdir=workspace)))
    return workspace


@pytest.mark.usefixtures("private_coding_install")
def test_detect_opencode(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    p = bin_dir / "opencode"
    p.write_text("#!/bin/sh\necho 1.0.0\n")
    p.chmod(0o755)
    found = detect(path_env=str(bin_dir))
    assert {row.name for row in found} == {"opencode"}


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
@pytest.mark.parametrize("signed_in", [True, False])
async def test_connected_tools_run_brief_in_workdir_and_stream_or_request_login(tmp_path, monkeypatch, owned_workspace, domain, signed_in):
    provider = "cursor"
    binary = tmp_path / "cursor-agent"
    workspace = owned_workspace
    receipt = tmp_path / "run.json"
    status = "Logged in as person@example.invalid" if signed_in else "Not logged in"
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": domain}]}, "session_id": "local-session"}
    binary.write_text(f"#!{sys.executable}\n" +
        "import json, os, sys\nfrom pathlib import Path\n" +
        "if sys.argv[1:] == ['--version']: print('2026.04.17-787b533')\n" +
        "elif sys.argv[1:] == ['--help']: print('--sandbox <mode> Explicitly enable or disable sandbox mode\\n--workspace <path>')\n" +
        "elif sys.argv[1:] == ['mcp', 'list']: print('No MCP servers configured.')\n" +
        f"elif sys.argv[1:] == ['status']: print({status!r})\n" +
        "else:\n" +
        f"    Path({str(receipt)!r}).write_text(json.dumps({{'cwd':os.getcwd(),'argv':sys.argv[1:]}}))\n" +
        f"    Path('result.txt').write_text({domain!r} + (' updated' if {domain + ' updated'!r} in sys.argv[-1] else ''))\n" +
        f"    print(json.dumps({event!r}), flush=True)\n" +
        "    print(json.dumps({'type':'file_changed','path':'result.txt'}), flush=True)\n" +
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}), flush=True)\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("CODING_AGENT_LIVE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    if signed_in:
        from desktop_runtime.agent_status import pin_provider
        from tests.coding_tool_fixtures import private_test_install
        private_test_install(monkeypatch, tmp_path)
        pin_provider(provider)
    events = []
    async def receive(event):
        events.append(event)
    result = await run_live(adapter=load_adapter(provider), req=CodingRequest(prompt=domain,
        workdir=workspace, timeout_s=10), on_event=receive)
    if not signed_in:
        assert result.ok is False
        assert "Settings > Keys and connections > Coding tools" in result.error
        assert "cursor-agent login" in result.error
        assert not receipt.exists()
        assert not (workspace / "result.txt").exists()
        return
    assert result.ok is True
    assert any(event.kind == AgentEventKind.FINISHED for event in events)
    assert any(event.session_id == "local-session" for event in events)
    assert any(event.text == domain for event in events)
    saved = json.loads(receipt.read_text())
    assert saved["cwd"] == str(workspace)
    assert domain in saved["argv"][-1]
    assert (workspace / "result.txt").read_text() == domain
    assert result.changed_files == ["result.txt"]
    direct = await load_adapter(provider).run(CodingRequest(prompt=domain + " updated", workdir=workspace, timeout_s=10))
    assert direct.ok is True
    assert any(event.text == domain for event in direct.events)
    assert (workspace / "result.txt").read_text() == domain + " updated"


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
async def test_connected_tool_failed_event_cannot_become_a_success(tmp_path, monkeypatch, owned_workspace, domain):
    from desktop_runtime import agent_status

    provider = "cursor"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(agent_status, "_status", lambda *args: {
        "installed": True, "signed_in": True, "subscription_login": True, "available": True})
    adapter = load_adapter(provider)
    event = {"type": "error", "message": domain}
    code = ("from pathlib import Path; import json; Path('result.txt').write_text('partial'); " +
        f"print(json.dumps({event!r}))")
    adapter.build_argv = lambda req: [sys.executable, "-c", code, adapter.write_flag]
    from desktop_runtime.owned_coding import OwnedCoding
    monkeypatch.setattr(OwnedCoding, "preflight", lambda coding: sys.executable)
    monkeypatch.setattr(OwnedCoding, "_disable_cursor_mcps", lambda *args: True)
    monkeypatch.setattr(OwnedCoding, "_argv", lambda *args: adapter.build_argv(None))
    result = await run_live(adapter=adapter, req=CodingRequest(prompt=domain, workdir=owned_workspace, timeout_s=5))
    assert result.ok is False
    assert (owned_workspace / "result.txt").read_text() == "partial"
    assert "Settings > Keys and connections > Coding tools" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
async def test_connected_tools_stop_only_their_owned_processes(tmp_path, monkeypatch, owned_workspace, domain):
    provider = "cursor"
    binary = tmp_path / "cursor-agent"
    binary.write_text(f"#!{sys.executable}\n" +
        "import json, os, sys, time\nfrom pathlib import Path\n" +
        "if sys.argv[1:] == ['--version']: print('2026.04.17-787b533')\n" +
        "elif sys.argv[1:] == ['--help']: print('--sandbox <mode> Explicitly enable or disable sandbox mode\\n--workspace <path>')\n" +
        "elif sys.argv[1:] == ['mcp', 'list']: print('No MCP servers configured.')\n" +
        "elif sys.argv[1:] == ['status']: print('Logged in as person@example.invalid')\n" +
        "else:\n" +
        "    Path('owned.pid').write_text(str(os.getpid()))\n" +
        "    print(json.dumps({'type':'started','session_id':'local-session'}), flush=True)\n" +
        "    time.sleep(30)\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    from desktop_runtime.agent_status import pin_provider
    from tests.coding_tool_fixtures import private_test_install
    private_test_install(monkeypatch, tmp_path)
    pin_provider(provider)
    stopped = asyncio.Event()
    async def receive(event):
        stopped.set()
    unrelated = psutil.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result = await run_live(adapter=load_adapter(provider), req=CodingRequest(prompt=domain,
            workdir=owned_workspace, timeout_s=10), on_event=receive, kill_switch=stopped.is_set)
        assert result.ok is False
        assert "Settings > Keys and connections > Coding tools" in result.error
        assert not psutil.pid_exists(int((owned_workspace / "owned.pid").read_text()))
        assert unrelated.is_running()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
