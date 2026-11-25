from __future__ import annotations

from pathlib import Path

import json
import sys
import time

import pytest

from coding_agents.cursor import CursorAdapter
from coding_agents.protocol import CodingRequest


pytestmark = pytest.mark.usefixtures("private_coding_install")


DOMAINS = ["supplier invoice tracking", "family hiking journal"]
CURSOR_HELP = """Usage: cursor-agent [options] [prompt...]
  -p, --print                       Print non-interactively
  --output-format <format>          text|json|stream-json (with --print)
  --mode <mode>                     plan|ask
  -f, --force                       Allow commands unless explicitly denied
  --sandbox <mode>             Explicitly enable or disable sandbox mode
  --workspace <path>           Workspace directory to use (defaults to current
  --trust                          Trust workspace without prompting (headless only)
  -w, --worktree                    Use a worktree
  --model <model>                   Model to use
"""
SANDBOX_PROMISE = "Explicitly enable or disable sandbox mode"
UNSAFE_SANDBOX_HELP = [CURSOR_HELP.replace(SANDBOX_PROMISE, description) for description in (
    "Sandbox mode (choices: disabled)", "Select sandbox mode", "Enabled mode is not supported (choices: disabled, enabled)")]


def fake_cursor(tmp_path, monkeypatch, help_text=CURSOR_HELP, signed_in=True, outside=False,
                mcps=(), disable_fails=False, idle=False, check_env=False):
    home = tmp_path / "home"
    config = home / ".cursor/cli-config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"sandbox":"disabled","approvalMode":"ask"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "fixture-owner")
    monkeypatch.setenv("LOGNAME", "fixture-owner")
    binary = tmp_path / "cursor-agent"
    protected = tmp_path / "protected"
    protected.mkdir()
    protected.chmod(0o500)
    listing = ("Configured MCP servers:\n" + "".join(f"  {name}\n    Status: Enabled\n" for name in mcps)
               if mcps else "No MCP servers configured.")
    binary.write_text(f"#!{sys.executable}\n" +
        "import json, os, sys, time\nfrom pathlib import Path\n" +
        f"with Path({str(tmp_path / 'calls.jsonl')!r}).open('a') as log: log.write(json.dumps([os.getcwd(), sys.argv[1:2] + (sys.argv[2:] if sys.argv[1:2] == ['mcp'] else [])]) + '\\n')\n" +
        f"if {check_env!r}: assert os.environ.get('SHELL') == '/bin/sh'\n" +
        f"assert os.environ['HOME'] == {str(home)!r}\n" +
        "assert not ({'BASH_ENV', 'AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN', 'AZURE_OPENAI_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CURSOR_API_KEY', 'CURSOR_AUTH_TOKEN', 'OTHER_SECRET'} & os.environ.keys())\n" +
        f"if sys.argv[1:] == ['--help']: print({help_text!r}); sys.exit(0)\n" +
        "if sys.argv[1:] == ['--version']: print('2026.04.17-787b533'); sys.exit(0)\n" +
        f"if sys.argv[1:] == ['status']: print({('Logged in as person@example.invalid' if signed_in else 'Not logged in')!r}); sys.exit(0)\n" +
        "if sys.argv[1:2] == ['mcp']:\n" +
        " tmp = Path(os.environ['TMPDIR'])\n" +
        " assert tmp.is_dir() and tmp.stat().st_mode & 0o777 == 0o700 and tmp != Path.cwd() / '.tmp'\n" +
        " if sys.argv[2:] == ['list']:\n" +
        f"  print({listing!r}); sys.exit(0)\n" +
        f" assert sys.argv[2] == 'disable' and sys.argv[3] in {mcps!r}\n" +
        f" sys.exit({int(disable_fails)})\n" +
        "assert sys.argv[1:8] == ['-p', '--output-format', 'stream-json', '--sandbox', 'enabled', '--workspace', os.getcwd()]\n" +
        "assert sys.argv[8] == '--trust' and len(sys.argv) == 10\n" +
        "assert sys.stdin.read() == ''\n" +
        "assert os.environ['TMPDIR'] == str(Path.cwd() / '.tmp')\n" +
        "assert os.environ['USER'] == os.environ['LOGNAME'] == 'fixture-owner'\n" +
        f"if {idle!r}:\n" +
        " for _ in range(600):\n" +
        "  print('Not a stream event', flush=True)\n" +
        "  print('Not progress either', file=sys.stderr, flush=True)\n" +
        "  time.sleep(.1)\n" +
        "print(json.dumps({'type':'system','session_id':'fixture'}), flush=True)\n" +
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Building your requested files'}]}}), flush=True)\n" +
        "print(json.dumps({'type':'tool_call','subtype':'started','tool_call':{'shellToolCall':{'args':{'command':'python3 --version'}}}}), flush=True)\n" +
        f"if {outside!r}:\n" +
        f" try: Path({str(protected / 'outside.txt')!r}).write_text('outside')\n" +
        " except PermissionError:\n" +
        "  print(json.dumps({'type':'tool_call','subtype':'completed','tool_call':{'shellToolCall':{'args':{'command':'write outside workspace'},'result':{'error':{'code':'sandbox_denied','message':'Your tool was denied access outside your build folder.'}}}}}), flush=True)\n" +
        " else: sys.exit(98)\n" +
        "else: Path('index.html').write_text('<main>Your requested result</main>')\n" +
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'Your files are ready'}), flush=True)\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    return binary, config, protected


def cursor_coding(tmp_path, monkeypatch, domain):
    from desktop_runtime.agent_status import pin_provider
    from desktop_runtime.owned_coding import OwnedCoding
    from desktop_runtime.owned_processes import OwnedProcesses

    pin_provider("cursor")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text("ven_fixture\n")
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug=domain, provider="cursor",
        tracker=OwnedProcesses(), authorize=lambda: True)
    coding.loop_mode = True
    monkeypatch.setattr(coding, "_workspace", lambda: workspace)
    return coding, workspace


@pytest.mark.parametrize("domain", DOMAINS)
def test_cursor_probe_and_build_keep_login_but_not_the_parent_shell_or_keys(monkeypatch, tmp_path, domain):
    from desktop_runtime.agent_status import _status

    fake_cursor(tmp_path, monkeypatch, check_env=True)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for key in ("BASH_ENV", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "OTHER_SECRET"):
        monkeypatch.setenv(key, "never-inherit-this-fixture")
    coding, _ = cursor_coding(tmp_path, monkeypatch, domain)
    try:
        assert _status("cursor")["available"]
        assert coding.run(domain, time.monotonic() + 10)["outcome"] == "pass"
    finally:
        coding.tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("disable_fails", [False, True])
def test_cursor_disables_workspace_mcps_before_build_or_stops(monkeypatch, tmp_path, domain, disable_fails):
    names = ("search-server", "local_index")
    _, config, _ = fake_cursor(tmp_path, monkeypatch, mcps=names, disable_fails=disable_fails)
    mcp_config = config.with_name("mcp.json")
    mcp_config.write_text('{"mcpServers":{"search-server":{},"local_index":{}}}')
    originals = config.read_bytes(), mcp_config.read_bytes()
    coding, workspace = cursor_coding(tmp_path, monkeypatch, domain)
    try:
        result = coding.run(domain, time.monotonic() + 10)
        commands = [args for cwd, args in map(json.loads, (tmp_path / "calls.jsonl").read_text().splitlines())
                    if cwd == str(workspace)]
        assert commands == [["mcp", "list"], ["mcp", "disable", names[0]], *(
            [] if disable_fails else [["mcp", "disable", names[1]], ["-p"]])]
        assert (workspace / "index.html").exists() is not disable_fails
        assert result["outcome"] == ("stopped" if disable_fails else "pass")
        if disable_fails:
            assert "choose Codex or Claude Code" in result["failure_message"]
        assert (config.read_bytes(), mcp_config.read_bytes()) == originals
        assert coding.tracker.stopped()
    finally:
        coding.tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
def test_cursor_without_stream_events_stops_with_a_manual_next_step(monkeypatch, tmp_path, domain):
    from desktop_runtime import owned_coding

    fake_cursor(tmp_path, monkeypatch, idle=True)
    coding, workspace = cursor_coding(tmp_path, monkeypatch, domain)
    monkeypatch.setattr(owned_coding, "_NO_PROGRESS_SECONDS", .5, raising=False)
    try:
        result = coding.run(domain, time.monotonic() + 5)
        assert result["outcome"] == "stopped"
        assert result["failure_message"] == ("Cursor stopped responding. Ask Charlie to retry, or choose Codex or Claude Code "
            "in Settings > Keys and connections > Coding tools.")
        assert not (workspace / "index.html").exists()
        assert coding.tracker.stopped()
    finally:
        coding.tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("help_text,ready", [
    (CURSOR_HELP, True),
    ("\n".join(line for line in CURSOR_HELP.splitlines() if not line.startswith("  --sandbox ")), False),
    ("\n".join(line for line in CURSOR_HELP.splitlines() if not line.startswith("  --workspace ")), False),
    (CURSOR_HELP.replace("--sandbox", "--sandbox-profile"), False),
    (CURSOR_HELP.replace(SANDBOX_PROMISE, 'Sandbox mode (choices: "enabled", "disabled")'), True),
    *((help_text, False) for help_text in UNSAFE_SANDBOX_HELP),
])
@pytest.mark.parametrize("signed_in", [True, False])
def test_cursor_readiness_requires_login_and_installed_isolation_flags(monkeypatch, tmp_path, domain, help_text, ready, signed_in):
    from desktop_runtime.agent_status import _status, launch_identity

    binary, config, _ = fake_cursor(tmp_path, monkeypatch, help_text, signed_in)
    before = config.read_bytes()
    status = _status("cursor", expected=launch_identity(binary))
    assert status["available"] is (ready and signed_in), domain
    assert status["subscription_login"] is signed_in
    assert "person@" not in json.dumps(status)
    assert config.read_bytes() == before
    if status["available"]:
        from tests.company_os_harness import load_company_os_app
        load_company_os_app()
        from app.build import device_orders
        monkeypatch.setattr(device_orders, "is_local", lambda: True)
        monkeypatch.setattr(device_orders, "coding_descriptor", lambda manifest: {"provider": "cursor"})
        monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {"cursor": True})
        monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [status])
        monkeypatch.setattr("app.core.local_capabilities.enabled", lambda name: True)
        device_orders.assert_coding_loop_available({}, "fixture-owner")


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("help_text", [CURSOR_HELP.replace(flag, "unsupported")
    for flag in ("--sandbox", "--workspace")] + UNSAFE_SANDBOX_HELP)
def test_cursor_execution_rechecks_help_before_launch(monkeypatch, tmp_path, domain, help_text):
    from desktop_runtime.owned_coding import OwnedCoding
    from desktop_runtime.owned_processes import OwnedProcesses

    binary, _, _ = fake_cursor(tmp_path, monkeypatch, help_text)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug=domain, provider="cursor", tracker=tracker, authorize=lambda: True)
    try:
        with pytest.raises(ValueError, match="coding_provider_upgrade_required"):
            coding._provider_version(str(binary), workspace)
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
def test_connected_executable_does_not_follow_path_or_replacements(monkeypatch, tmp_path, domain):
    from desktop_runtime import agent_status, owned_coding

    binary, _, _ = fake_cursor(tmp_path, monkeypatch)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    agent_status.pin_provider("cursor")
    monkeypatch.chdir(binary.parent)
    assert agent_status.tool_binary("cursor", ".:" + str(binary.parent)) is None
    coding = owned_coding.OwnedCoding(venture_id="ven_fixture", workspace_slug=domain,
        provider="cursor", tracker=None, authorize=lambda: True)
    monkeypatch.setenv("PATH", ".:")
    assert coding.preflight() == str(binary)
    replacement = binary.with_suffix(".new")
    replacement.write_bytes(binary.read_bytes())
    replacement.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(coding, "_workspace", lambda: workspace)
    monkeypatch.setattr(coding, "_provider_version", lambda *_: "fixture-version")
    monkeypatch.setattr(coding, "_disable_cursor_mcps", lambda *_: True)
    checks = []

    def authorize():
        checks.append(True)
        if len(checks) == 3:
            replacement.replace(binary)
        return True

    coding.authorize = authorize
    with pytest.raises(ValueError, match="coding_provider_changed"):
        coding.run(domain, time.monotonic() + 5)
    assert not coding.spawn_attempted
    calls = tmp_path / "calls.jsonl"
    before = calls.read_bytes()
    assert not agent_status._status("cursor")["available"]
    with pytest.raises(ValueError, match="coding_provider_changed"):
        coding.preflight()
    assert calls.read_bytes() == before


@pytest.mark.parametrize("domain", DOMAINS)
def test_probe_uses_private_temp_and_controlled_path(monkeypatch, tmp_path, domain):
    import os

    from desktop_runtime.agent_status import _probe

    binary = tmp_path / "fixture"
    binary.write_text(f"#!{sys.executable}\nimport json, os, stat\n"
        "tmp = os.environ['TMPDIR']\n"
        "print(json.dumps([tmp, os.getcwd(), stat.S_IMODE(os.stat(tmp).st_mode), os.environ['PATH']]))\n")
    binary.chmod(0o700)
    ambient = tmp_path / domain
    ambient.mkdir()
    monkeypatch.setenv("TMPDIR", str(ambient))
    monkeypatch.setenv("PATH", str(ambient))
    code, output = _probe(str(binary), ["--version"])
    temp, cwd, mode, path = json.loads(output)
    assert code == 0 and temp == cwd and mode == 0o700
    assert temp != str(ambient) and not Path(temp).exists()
    assert str(ambient) not in path.split(os.pathsep)


@pytest.mark.parametrize("domain", DOMAINS)
def test_workspace_snapshot_restore_and_metadata_do_not_follow_hardlinks(monkeypatch, tmp_path, domain):
    from desktop_runtime import coding_loop, owned_coding, owned_terminal

    outside = tmp_path / "outside"
    outside.write_text(domain)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "index.html"
    source.write_text("snapshot")
    saved = owned_coding.workspace_files(workspace)
    source.unlink()
    source.hardlink_to(outside)
    with pytest.raises(ValueError, match="coding_changed_path_invalid"):
        owned_coding.workspace_files(workspace)
    coding_loop._restore(workspace, saved)
    assert source.read_text() == "snapshot"
    owned_coding._trusted_git(workspace, "init", "--quiet", "--initial-branch=main")
    exclude = workspace / ".git/info/exclude"
    exclude.unlink()
    exclude.hardlink_to(outside)
    with pytest.raises(ValueError):
        owned_coding.OwnedCoding._env(workspace)
    monkeypatch.setattr(owned_terminal, "venture_workspace_root", lambda *_: tmp_path / "workspaces")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    terminal = owned_terminal.OwnedTerminal(venture_id="ven_fixture", workspace_slug="venture",
        tracker=None, authorize=lambda: True)
    terminal.workspace()
    record = tmp_path / "data/ventures/ven_fixture/workspace.json"
    record.unlink()
    record.hardlink_to(outside)
    with pytest.raises(ValueError):
        terminal.workspace()
    assert outside.read_text() == domain


@pytest.mark.parametrize("domain", DOMAINS)
def test_activity_scrubs_registered_and_unlabeled_credentials_before_storage(monkeypatch, tmp_path, domain):
    from core import logging as core_logging
    from desktop_runtime import coding_loop
    from desktop_runtime.owned_coding import _provider_failure

    registered = "a recognized fixture phrase"
    unknown = "mF6w9Vb1Qp4xZaL3"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(core_logging, "_LOCAL_SECRETS", {registered})
    stored, sent = [], []

    class Worker:
        last = {}
        gateway = type("Gateway", (), {"coding_event": staticmethod(sent.append)})()

        def progress(self, state, **values):
            self.last.update(values)
            stored.extend(values.get("events", []))

    coding_loop._event(Worker(), f'Reviewed {domain}: SESSION={unknown} and {registered}. Mode=development.', tmp_path)
    assert stored and sent
    for event in stored + sent:
        assert unknown not in event["text"] and registered not in event["text"]
        assert domain in event["text"] and "Mode=development" in event["text"]
    failure = {"type": "turn.failed", "error": {"message": f'Reviewed {domain}: session: "{unknown}"'}}
    notice = _provider_failure(json.dumps(failure).encode(), "codex")
    assert domain in notice and unknown not in notice


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("code,outcome", [("authentication_failed", "auth_required"), ("usage_limit_reached", "quota_paused")])
def test_cursor_sandbox_envelope_preserves_login_and_usage_failures(domain, code, outcome):
    from desktop_runtime.owned_coding import provider_outcome

    event = {"type": "error", "error": {"code": code, "message": "Sandbox request failed for " + domain}}
    assert provider_outcome(json.dumps(event).encode(), "cursor")["outcome"] == outcome


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("loop_mode", [True, False], ids=["product-loop", "landing-build"])
@pytest.mark.parametrize("outside", [False, True], ids=["build", "sandbox-denial"])
def test_cursor_owned_build_stream_and_sandbox_failure(monkeypatch, tmp_path, domain, loop_mode, outside):
    from desktop_runtime import loop_diagnose

    _, config, protected = fake_cursor(tmp_path, monkeypatch, outside=outside)
    before = config.read_bytes()
    coding, workspace = cursor_coding(tmp_path, monkeypatch, domain)
    tracker = coding.tracker
    coding.loop_mode = loop_mode
    lines = []
    coding.line_callback = lines.append
    try:
        if outside and not loop_mode:
            with pytest.raises(ValueError, match="coding_sandbox_denied"):
                coding.run(domain, time.monotonic() + 5)
        else:
            result = coding.run(domain, time.monotonic() + 5)
            if loop_mode:
                assert result["outcome"] == ("sandbox_denied" if outside else "pass")
            else:
                assert any(item["path"] == "index.html" for item in result["changed_files"])
            assert result["provider_version"] == "cursor-agent 2026.04.17-787b533"
            if outside:
                assert "denied access outside your build folder" in result["failure_message"]
                assert "Your files are ready" not in result["failure_message"]
        history = "\n".join(text for line in lines if (text := loop_diagnose.stream_text(line, "cursor", workspace)))
        assert "Building your requested files" in history
        assert "python3 --version" in history
        if outside:
            assert "denied access outside your build folder" in history
            assert "Build activity" in history and "venture folder" in history
            assert not (protected / "outside.txt").exists()
            assert not (workspace / "index.html").exists()
        else:
            assert "Your files are ready" in history
            assert (workspace / "index.html").is_file()
        assert config.read_bytes() == before
        assert tracker.stopped()
    finally:
        tracker.stop_all()
        protected.chmod(0o700)


def test_cursor_argv_includes_trust_sandbox_force(tmp_path: Path):
    ad = CursorAdapter()
    req = CodingRequest(prompt="hi", workdir=tmp_path, allow_write=True, allow_network=False)
    argv = ad.build_argv(req)
    assert "--trust" in argv
    assert "--sandbox" in argv
    assert "enabled" in argv
    assert "--force" in argv
    assert "--workspace" in argv
    assert str(tmp_path) in argv
