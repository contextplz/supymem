"""Reviewed provider isolation and local evidence; no model calls or API keys."""
import json
import asyncio
import builtins
import os
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_runtime import owned_coding
from desktop_runtime.owned_coding import OwnedCoding
from desktop_runtime.owned_processes import OwnedProcesses


pytestmark = pytest.mark.usefixtures("private_coding_install")


DOMAINS = ["equipment maintenance for factories", "practice schedules for amateur musicians"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX subprocess fixture")
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("hangs", [False, True], ids=["exits", "hangs"])
def test_pipe_reader_streams_slow_child_with_bounded_wait(domain, hangs):
    from desktop_runtime.pipe_reader import PipeReader

    script = ("import sys, time; print(sys.argv[1], flush=True); time.sleep(.05); "
              "print(sys.argv[1], file=sys.stderr, flush=True)")
    if hangs:
        script += "; time.sleep(30)"
    with subprocess.Popen([sys.executable, "-c", script, domain],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, PipeReader() as reader:
        try:
            reader.add(process.stdout, "stdout")
            reader.add(process.stderr, "stderr")
            received = {"stdout": b"", "stderr": b""}
            deadline = time.monotonic() + 2
            while reader.pending and time.monotonic() < deadline:
                event = reader.read(.05)
                if event is not None:
                    name, chunk = event
                    received[name] += chunk
                if hangs and all(received.values()):
                    assert reader.read(.05) is None
                    assert process.poll() is None
                    break
            assert received == dict.fromkeys(received, domain.encode() + b"\n")
            if not hangs:
                assert not reader.pending
                assert process.wait(timeout=1) == 0
        finally:
            if process.poll() is None:
                process.kill()


@pytest.mark.parametrize("domain", DOMAINS)
def test_win32_probe_and_runner_do_not_import_selectors(monkeypatch, tmp_path, domain):
    from desktop_runtime import agent_status

    original_import = builtins.__import__

    def import_without_selectors(name, *args, **kwargs):
        assert name != "selectors", "Windows subprocess pipes cannot use selectors"
        return original_import(name, *args, **kwargs)

    install, workspace = tmp_path / "tools", tmp_path / "workspace"
    install.mkdir()
    workspace.mkdir()
    binary = install / "codex.exe"
    shutil.copy2(sys.executable, binary)
    shutil.copy2("/usr/bin/git", install / "git.exe")
    monkeypatch.setenv("PATH", str(install))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(os, "killpg", raising=False)
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", import_without_selectors)
        runtime = runpy.run_path(owned_coding.__file__)
        probe = runpy.run_path(agent_status.__file__)
    agent_status.atexit.unregister(probe["stop_probes"])
    monkeypatch.setattr(owned_coding, "_windows_acl_safe", lambda _: True)
    monkeypatch.setitem(runtime["launch_identity"].__globals__, "_windows_acl_safe", lambda _: True)
    assert probe["_probe"](str(binary), ["-c", "import sys; print(sys.argv[1])", domain],
        cwd=workspace) == (0, domain.encode() + b"\n")

    (workspace / ".supymem-venture").write_text("fixture")
    tracker = OwnedProcesses()
    coding = runtime["OwnedCoding"](venture_id="fixture", workspace_slug="fixture", provider="codex",
        tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    coding._binary_identity = runtime["launch_identity"](binary)
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_workspace", lambda: workspace)
    monkeypatch.setattr(coding, "_provider_version", lambda *_args: "codex-cli 0.163.0")
    event = json.dumps({"type": "turn.completed"})
    monkeypatch.setattr(coding, "_argv", lambda *_args: [str(binary), "-c",
        "import sys, time; print(sys.argv[1], file=sys.stderr); time.sleep(.1); "
        "print(sys.argv[2], end='')", domain, event])
    read_pipe = runtime["PipeReader"].read

    def first_read_times_out_after_tool_exit(reader, timeout):
        monkeypatch.setattr(runtime["PipeReader"], "read", read_pipe)
        (_, process), = tracker._values()
        process.wait(timeout=2)
        return None

    monkeypatch.setattr(runtime["PipeReader"], "read", first_read_times_out_after_tool_exit)
    try:
        receipt = coding.run(domain, time.monotonic() + 5)
        assert receipt["outcome"] == "pass"
        assert receipt["output_bytes"] == len(domain.encode()) + 1 + len(event.encode())
        assert tracker.stopped()
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
def test_build_cleanup_only_removes_its_managed_workspace_scratch(tmp_path, monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=None, authorize=lambda: True)
    workspace = coding._workspace()
    scratch = workspace / ".tmp"
    (scratch / "node_modules").mkdir()
    (scratch / "node_modules/stale").write_text(domain)
    database = workspace / "venture.sqlite3"
    database.write_bytes(b"SQLite format 3\x00")
    work = workspace / "index.html"
    work.write_text(domain)
    backup = tmp_path / "backups/newest.tar.gz"
    backup.write_text(domain)
    (scratch / "backup-link").symlink_to(backup.parent, target_is_directory=True)

    coding.clear_temporary()

    assert not scratch.exists()
    assert database.read_bytes() == b"SQLite format 3\x00"
    assert work.read_text() == backup.read_text() == domain
    coding._workspace()
    (scratch / "keep").write_text(domain)
    owned_coding._trusted_git(workspace, "add", "-f", "--", ".tmp/keep")
    coding.clear_temporary()
    assert (scratch / "keep").read_text() == domain


@pytest.mark.parametrize("domain", DOMAINS)
def test_opencode_uses_its_own_noninteractive_json_argv(tmp_path, domain):
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="opencode",
        tracker=None, authorize=lambda: True)
    assert coding._argv("opencode", domain, tmp_path) == [
        "opencode", "run", "--format", "json", "--agent", "supymem-build", domain]


@pytest.mark.parametrize("domain", DOMAINS)
def test_codex_disables_skill_prompts_and_ignores_personal_instructions(tmp_path, domain):
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=None, authorize=lambda: True)
    coding.loop_mode = True
    argv = coding._argv("codex", domain, tmp_path)
    assert "skills.include_instructions=false" in argv
    assert "project_doc_max_bytes=0" in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert "Ignore personal skills" in coding._approved_execution_instruction(domain)


@pytest.mark.parametrize("domain", DOMAINS)
def test_unrelated_unreadable_skill_remark_does_not_fail_a_completed_tool(monkeypatch, tmp_path, domain):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / ".supymem-venture").write_text("ven_fixture")
    binary = tmp_path.parent / (tmp_path.name + "-codex")
    events = [{"type": "item.completed", "item": {"type": "agent_message",
        "text": "I can't read a personal skill file, but saved the requested files for " + domain}},
        {"type": "turn.completed"}]
    payload = "\n".join(json.dumps(event) for event in events)
    binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
        "Path('review').mkdir(exist_ok=True)\nPath('review/spec.md').write_text('Saved specification')\n"
        "Path('review/tasks.json').write_text('[]')\n" + f"print({payload!r}, flush=True)\n")
    binary.chmod(0o700)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_provider_version", lambda *args: "codex-cli 0.163.0")
    monkeypatch.setattr(coding, "_workspace", lambda: tmp_path)
    try:
        result = coding.run(domain, time.monotonic() + 10)
        assert result["outcome"] == "pass" and result["exit_code"] == 0
        assert "failure_message" not in result
        assert (tmp_path / "review/spec.md").is_file() and (tmp_path / "review/tasks.json").is_file()
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_loop_timeout_saves_the_tools_last_message(monkeypatch, tmp_path, domain, stream):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / ".supymem-venture").write_text("ven_fixture")
    message = "Still checking the local files for " + domain
    binary = tmp_path.parent / (tmp_path.name + "-codex")
    payload = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": message}})
    binary.write_text(f"#!{sys.executable}\nimport sys, time\nprint({payload!r}, file=sys.{stream}, flush=True)\ntime.sleep(30)\n")
    binary.chmod(0o700)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_provider_version", lambda *args: "codex-cli 9.0.0")
    monkeypatch.setattr(coding, "_workspace", lambda: tmp_path)
    events = []
    coding.line_callback = events.append
    try:
        result = coding.run(domain, time.monotonic() + 1)
        assert result["outcome"] == "timeout"
        assert message in result["failure_message"]
        assert "Build activity" in result["failure_message"]
        assert any(message in line.decode() for line in events)
        assert tracker.stopped()
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_mac_coding_profile_reads_existing_toolchain_only(monkeypatch, tmp_path, domain, provider):
    import tomllib

    active = "/Applications/Developer Suite.app/Contents/Developer"
    existing = {active, "/Library/Developer", "/opt/homebrew"}
    monkeypatch.setattr(sys, "platform", "darwin")
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: str(path) in existing or exists(path)
        if str(path) not in {"/usr/local"} else False)
    calls = []
    def select(argv, **options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=active + "\n")
    monkeypatch.setattr(owned_coding.subprocess, "run", select)
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider=provider,
        tracker=None, authorize=lambda: True)
    coding.loop_mode = True
    argv = coding._argv(provider, domain, tmp_path)
    assert calls == [["/usr/bin/xcode-select", "-p"]]
    if provider == "codex":
        policy = next(value for value in argv if value.startswith("permissions.supymem_product="))
        profile = tomllib.loads(policy)["permissions"]["supymem_product"]
        assert all(profile["filesystem"][path] == "read" for path in existing)
        assert "/usr/local" not in profile["filesystem"]
        assert profile["filesystem"][":root"] == profile["filesystem"][":tmpdir"] == "deny"
        assert profile["network"]["enabled"] is False
        assert profile["filesystem"][":workspace_roots"][".tmp"] == "write"
        assert all(profile["filesystem"][":workspace_roots"][path] == "deny"
            for path in (".env*", "**/.env*", ".ssh", ".aws", ".codex", ".claude", ".cursor"))
    else:
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
        assert set(sandbox["filesystem"]["allowRead"]) == {str(tmp_path), *existing}
        assert sandbox["network"]["deniedDomains"] == ["*"]
        assert all(str(tmp_path / path) in sandbox["filesystem"]["denyRead"]
            for path in (".ssh", ".aws", ".codex", ".claude", ".cursor"))


@pytest.mark.parametrize("domain", DOMAINS)
def test_coding_temp_is_private_ignored_and_not_source(monkeypatch, tmp_path, domain):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    monkeypatch.setenv("TMPDIR", "/tmp/outside-workspace")
    env = OwnedCoding._env(tmp_path)
    temporary = Path(env["TMPDIR"])
    assert temporary == tmp_path / ".tmp"
    assert temporary.stat().st_mode & 0o777 == 0o700
    (temporary / "cache").write_text(domain)
    assert owned_coding._trusted_git(tmp_path, "check-ignore", ".tmp/cache") == ".tmp/cache"
    assert ".tmp/cache" not in owned_coding.workspace_files(tmp_path, include_git=True)
    from desktop_runtime.coding_loop import _checkpoint
    _checkpoint(tmp_path, "Fixture baseline")
    assert ".tmp/cache" not in owned_coding._trusted_git(tmp_path, "ls-files")
    temporary.rename(tmp_path / "cache")
    temporary.symlink_to(tmp_path / "cache", target_is_directory=True)
    with pytest.raises(ValueError, match="coding_changed_path_invalid"):
        OwnedCoding._env(tmp_path)


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_tool_refusal_finishes_fast_with_saved_messages_commands_and_exit_code(monkeypatch, tmp_path, domain, provider):
    from desktop_runtime import coding_loop, device_journal, loop_diagnose

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path.parent / (tmp_path.name + "-journal")))
    scope = ("http://127.0.0.1", "person_fixture", "device_fixture")
    device_journal.create(scope, "ven_fixture", "dwo_fixture")

    refusal = f"I couldn't implement {domain} because the compiler is unavailable."
    events = ([{"type": "item.completed", "item": {"type": "agent_message", "text": "Working on " + domain}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "python3 --version", "exit_code": 127}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": refusal}},
        {"type": "turn.failed", "error": {"message": refusal}}] if provider == "codex" else [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on " + domain}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "python3 --version"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "node --version"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Exit code 127"}]}},
        {"type": "result", "is_error": True, "subtype": "error_during_execution", "result": refusal}])
    binary = tmp_path / "provider"
    binary.write_text(f'#!{sys.executable}\nimport json,sys,time\n'
        'if "--version" in sys.argv:\n print("codex-cli 0.163.0" if "codex" in sys.argv[0] else "2.1.284"); sys.exit(0)\n'
        f'for event in {events!r}:\n print(json.dumps(event),flush=True)\n'
        'time.sleep(30)\n')
    binary.chmod(0o755)
    if provider == "codex":
        binary.rename(tmp_path / "codex")
        binary = tmp_path / "codex"
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider=provider,
        tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_workspace", lambda: tmp_path)
    (tmp_path / ".supymem-venture").write_text("ven_fixture\n")
    popen = owned_coding.psutil.Popen
    def scoped_process(argv, **options):
        temporary = Path(options["env"]["TMPDIR"])
        if argv[1:] == ["--version"]:
            assert temporary != tmp_path / ".tmp" and temporary.stat().st_mode & 0o777 == 0o700
        else:
            assert temporary == tmp_path / ".tmp"
        return popen(argv, **options)
    monkeypatch.setattr(owned_coding.psutil, "Popen", scoped_process)
    saved = []
    worker = SimpleNamespace(last={"state": "running_coding"})
    def save(state, **values):
        worker.last.update(state=state, **values)
        device_journal.save(scope, "ven_fixture", "dwo_fixture", worker.last)
        saved.append(json.loads(json.dumps(worker.last)))
    worker.progress = save
    coding.line_callback = lambda line: coding_loop._event(worker, text, tmp_path) if (
        text := loop_diagnose.stream_text(line, provider, tmp_path)) else None
    try:
        result = coding.run(domain, time.monotonic() + 4)
        assert result["outcome"] == "cli_error"
        assert refusal in result["failure_message"]
        assert "Settings > Keys and connections > Coding tools" in result["failure_message"]
        restored = device_journal.read(scope, "ven_fixture", "dwo_fixture")
        assert restored == saved[-1]
        history = "\n".join(event["text"] for event in restored["events"])
        assert "Working on " + domain in history and "python3 --version" in history
        assert "127" in history and refusal in history
        if provider == "claude":
            assert "node --version" in history
        assert tracker.stopped()
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_tool_stderr_failure_surfaces_its_scrubbed_sentence(monkeypatch, tmp_path, domain, provider):
    from desktop_runtime import loop_diagnose

    refusal = f"Your tool could not compile {domain}."
    binary = tmp_path / provider
    binary.write_text(f'#!{sys.executable}\nimport sys\n'
        'if "--version" in sys.argv:\n print("codex-cli 0.163.0" if "codex" in sys.argv[0] else "2.1.284"); sys.exit(0)\n'
        f'print({(refusal + " token=short-fixture")!r},file=sys.stderr,flush=True)\nsys.exit(2)\n')
    binary.chmod(0o755)
    (tmp_path / ".supymem-venture").write_text("ven_fixture\n")
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider=provider,
        tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_workspace", lambda: tmp_path)
    lines = []
    coding.line_callback = lines.append
    try:
        result = coding.run(domain, time.monotonic() + 5)
        assert result["outcome"] == "cli_error" and result["exit_code"] == 2
        assert refusal in result["failure_message"]
        history = "\n".join(text for line in lines if (text := loop_diagnose.stream_text(line, provider, tmp_path)))
        assert refusal in history
        assert "Settings > Keys and connections > Coding tools" in history
        assert "short-fixture" not in history + result["failure_message"]
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_permission_discovery_cannot_launch_after_authority_is_lost(monkeypatch, tmp_path, domain, provider):
    allowed = [True]
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider=provider,
        tracker=None, authorize=lambda: allowed[0])
    monkeypatch.setattr(coding, "preflight", lambda: "/bin/" + provider)
    monkeypatch.setattr(coding, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(coding, "_provider_version", lambda *args: "fixture")
    def discover(*args):
        allowed[0] = False
        return ["/bin/" + provider]
    monkeypatch.setattr(coding, "_argv", discover)
    monkeypatch.setattr(owned_coding.psutil, "Popen", lambda *args, **options:
        pytest.fail("Expired authority must not start your coding tool"))
    with pytest.raises(ValueError, match="coding_authority_unconfirmed"):
        coding.run(domain, time.monotonic() + 5)


@pytest.mark.parametrize("domain", DOMAINS)
def test_private_event_fields_are_hidden_regardless_of_type_and_usage_is_retained(domain):
    from coding_agents.protocol import AgentEvent, AgentEventKind
    event = AgentEvent(kind=AgentEventKind.MESSAGE, text=domain, tokens_in=21, raw={
        "password": 12345, "secret_access_key": "x" * 40,
        "api_key": {"value": "private"}, "usage": {"input_tokens": 21}})
    assert event.raw["password"] == "[REDACTED]"
    assert event.raw["secret_access_key"] == "[REDACTED]"
    assert event.raw["api_key"] == "[REDACTED]"
    assert event.tokens_in == event.raw["usage"]["input_tokens"] == 21


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude", "cursor", "opencode"])
def test_nested_provider_events_retain_activity_without_secrets(tmp_path, domain, provider):
    from coding_agents import load_adapter
    from coding_agents.protocol import AgentEventKind

    private = "sk-" + "z" * 25
    payloads = {
        "codex": {"type": "item.completed", "item": {"id": "cmd_1", "type": "command_execution",
            "command": "node --test", "aggregated_output": domain + " " + private}},
        "claude": {"type": "assistant", "message": {"content": [{"type": "tool_use",
            "id": "cmd_1", "name": "Bash", "input": {"command": "node --test", "api_key": private}}]}},
        "cursor": {"type": "tool_call", "subtype": "started", "call_id": "cmd_1",
            "tool_call": {"shellToolCall": {"args": {"command": "node --test", "password": private}}}},
        "opencode": {"type": "tool_use", "sessionID": "session_1", "part": {"type": "tool",
            "callID": "cmd_1", "tool": "bash", "state": {"status": "completed",
                "input": {"command": "node --test", "token": private}, "output": domain}}},
    }
    event = load_adapter(provider).parse_event(payloads[provider])
    assert event is not None
    assert event.kind in {AgentEventKind.TOOL_CALL, AgentEventKind.TOOL_RESULT}
    assert event.tool_call_id == "cmd_1"
    assert "node --test" in event.model_dump_json()
    assert private not in event.model_dump_json()
    result = load_adapter(provider).parse_event({"type": "result", "is_error": True,
        "subtype": "error_during_execution", "result": domain})
    if provider in {"claude", "cursor"}:
        assert result.kind == AgentEventKind.FAILED


@pytest.mark.parametrize("domain", DOMAINS)
def test_codex_nested_message_file_changes_and_quota(tmp_path, domain):
    from coding_agents.codex import CodexAdapter
    from coding_agents.protocol import AgentEventKind

    adapter = CodexAdapter()
    message = adapter.parse_event({"type": "item.completed", "item": {
        "type": "agent_message", "text": domain}})
    assert message.kind == AgentEventKind.MESSAGE and message.text == domain
    changed = adapter.parse_event({"type": "item.completed", "item": {"type": "file_change",
        "changes": [{"path": "index.html", "kind": "update"}]}})
    assert changed.kind == AgentEventKind.FILE_CHANGED and changed.path == "index.html"
    quota = owned_coding.provider_outcome(json.dumps({"type": "turn.failed", "error": {
        "code": "usage_limit_reached", "message": domain, "reset_at": 1900000000}}).encode(), "codex", 1)
    assert quota == {"outcome": "quota_paused", "reset_at": 1900000000}


@pytest.mark.parametrize("domain", DOMAINS)
def test_guarded_codex_requires_terminal_success_without_loop(monkeypatch, tmp_path, domain):
    from tests.coding_tool_fixtures import pin_test_tool

    monkeypatch.setenv("HOME", str(tmp_path))
    binary = tmp_path / "codex"
    binary.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "codex-cli 0.163.0"; exit 0; fi\n'
        'echo changed > index.html\necho \'{"type":"turn.failed","error":{"message":"stopped"}}\'\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    pin_test_tool(monkeypatch, "codex", binary)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=tracker, authorize=lambda: True)
    try:
        with pytest.raises(ValueError, match="coding_provider_result_unconfirmed"):
            coding.run(domain, time.monotonic() + 10)
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
def test_owned_output_is_memory_only_and_callbacks_are_scrubbed(monkeypatch, tmp_path, domain):
    from tests.coding_tool_fixtures import pin_test_tool

    monkeypatch.setenv("HOME", str(tmp_path))
    binary = tmp_path / "codex"
    private = "sk-" + "z" * 25
    binary.write_text(f'#!{sys.executable}\nimport json,os,sys\n'
        'if "--version" in sys.argv:\n print("codex-cli 0.163.0"); sys.exit(0)\n'
        'os.write(2,b" " * 150000 + b"\\n")\n'
        f'print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":{private!r}}}}}),flush=True)\n'
        'print("{\\"type\\":\\"turn.completed\\"}",flush=True)\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    pin_test_tool(monkeypatch, "codex", binary)
    popen = owned_coding.psutil.Popen
    def memory_only(argv, **kwargs):
        if "exec" in argv:
            assert kwargs["stdout"] == owned_coding.subprocess.PIPE
        return popen(argv, **kwargs)
    monkeypatch.setattr(owned_coding.psutil, "Popen", memory_only)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    lines = []
    coding.line_callback = lines.append
    try:
        receipt = coding.run(domain, time.monotonic() + 10)
        assert receipt["outcome"] == "secret_detected"
        assert lines and private.encode() not in b"\n".join(lines)
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
def test_file_delta_ignores_preexisting_dirty_files_and_tracks_ignored_deletions(tmp_path, domain):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "existing.txt").write_text(domain)
    (tmp_path / "ignored.txt").write_text(domain)
    before = OwnedCoding._git_state(tmp_path)
    (tmp_path / "ignored.txt").unlink()
    (tmp_path / "created.txt").write_text(domain)
    delta = OwnedCoding._verify_changes(tmp_path, before)
    assert [entry["path"] for entry in delta["changed_files"]] == ["created.txt", "ignored.txt"]
    assert delta["changed_files"][1]["deleted"] is True


@pytest.mark.parametrize("domain", DOMAINS)
def test_generated_source_cannot_contain_a_bare_secret(tmp_path, domain):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    before = OwnedCoding._git_state(tmp_path)
    (tmp_path / "index.html").write_text(domain + " sk-" + "z" * 25)
    with pytest.raises(ValueError, match="coding_secret_detected"):
        OwnedCoding._verify_changes(tmp_path, before)


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("failure", ["auth_failure", "timed_out", "exception"])
def test_local_dispatch_never_switches_tools_after_attempt(monkeypatch, tmp_path, domain, failure):
    from coding_agents import dispatcher
    from coding_agents.protocol import CodingRequest, CodingResult
    from coding_agents.select import Selection

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(dispatcher, "detect", lambda: [])
    monkeypatch.setattr(dispatcher, "require_consent", lambda _name: None)
    calls = []
    def choose(**kwargs):
        return Selection("codex" if not calls else "claude", "local", "fixture")
    async def run(choice, req, **kwargs):
        calls.append(choice.adapter_name)
        if len(calls) == 1:
            if failure == "exception":
                (req.workdir / "partial.txt").write_text(domain)
                raise OSError("private startup detail")
            return CodingResult(ok=False, error=failure)
        return CodingResult(ok=True)
    monkeypatch.setattr(dispatcher, "select", choose)
    monkeypatch.setattr(dispatcher, "_cloud_keys_present", lambda: {})
    monkeypatch.setattr(dispatcher, "_run_selection", run)
    result = asyncio.run(dispatcher.dispatch_request(CodingRequest(prompt=domain, workdir=tmp_path)))
    assert not result.ok
    assert calls == ["codex"]
    assert "private startup detail" not in (result.error or "")


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_selection_never_uses_api_or_stub_fallback(monkeypatch, domain):
    from coding_agents.select import select
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert select(kind="code", detected=[], bridge_connected=False,
        cloud_keys={"OPENAI_API_KEY": True}, inhouse_hosted_available=True) is None, domain


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_schema_request_keeps_your_selected_tool(monkeypatch, domain):
    from coding_agents.detect import DetectedAgent
    from coding_agents.select import select
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    detected = [DetectedAgent(name=name, binary=name, version="1.0.0", auth_mode="subscription")
                for name in ("codex", "claude")]
    choice = select(kind="schema", prefs=["claude"], detected=detected)
    assert choice.adapter_name == "claude", domain


@pytest.mark.parametrize("domain", DOMAINS)
def test_requested_subscription_build_is_independent_of_chat_offline_flag(monkeypatch, tmp_path, domain):
    from coding_agents import dispatcher
    from coding_agents.protocol import CodingRequest, CodingResult
    from coding_agents.select import Selection
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setattr(dispatcher, "detect", lambda: [])
    monkeypatch.setattr(dispatcher, "select", lambda **kwargs: Selection("codex", "local", "fixture"))
    async def run(*args, **kwargs):
        return CodingResult(ok=True)
    monkeypatch.setattr(dispatcher, "_run_selection", run)
    assert asyncio.run(dispatcher.dispatch_request(CodingRequest(prompt=domain, workdir=tmp_path))).ok


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_mode_detection_overrides_legacy_hosted_switches(monkeypatch, domain):
    from coding_agents.env import hosted_execution
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "production")
    assert not hosted_execution(), domain


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("writes", [True, False])
def test_runner_drains_stderr_scrubs_logs_and_reports_only_real_deltas(monkeypatch, tmp_path, domain, writes):
    from coding_agents.codex import CodexAdapter
    from coding_agents.protocol import CodingRequest
    from coding_agents.subprocess_runner import run_live
    spawn = asyncio.create_subprocess_exec
    async def bounded_stream(*argv, **options):
        options["limit"] = 65536
        return await spawn(*argv, **options)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", bounded_stream)

    (tmp_path / "existing.txt").write_text(domain)
    (tmp_path / "removed.txt").write_text(domain)
    private = "sk-" + "z" * 25
    message = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": private}})
    code = ("import json,os,pathlib; "
        "os.write(2,b'warning\\n'*25000); "
        f"print({message!r}); "
        "print('{\"type\":\"turn.completed\"}'); ")
    if writes:
        code += "pathlib.Path('created.txt').write_text('done'); pathlib.Path('removed.txt').unlink()"
    adapter = CodexAdapter()
    adapter.name = "fixture"
    adapter.build_argv = lambda req: [sys.executable, "-c", code, "--sandbox", "workspace-write"]
    log = tmp_path / "logs" / "stream.ndjson"
    result = asyncio.run(run_live(adapter=adapter,
        req=CodingRequest(prompt=domain, workdir=tmp_path, timeout_s=2), transcript_path=log))
    assert result.ok is writes
    assert result.changed_files == (["created.txt", "removed.txt"] if writes else [])
    assert private not in log.read_text()
    assert private not in result.model_dump_json()


@pytest.mark.parametrize("domain", DOMAINS)
def test_stop_kills_new_session_descendant_and_preserves_unrelated_process(tmp_path, domain):
    import psutil
    from coding_agents.codex import CodexAdapter
    from coding_agents.protocol import CodingRequest
    from coding_agents.subprocess_runner import run_live

    marker = tmp_path / "child.pid"
    child = "import time; time.sleep(30)"
    code = ("import pathlib,subprocess,sys,time; "
        f"child_process=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,stdout=subprocess.DEVNULL); "
        f"pathlib.Path({str(marker)!r}).write_text(str(child_process.pid)); time.sleep(30)")
    adapter = CodexAdapter()
    adapter.name = "fixture"
    adapter.build_argv = lambda req: [sys.executable, "-c", code]
    unrelated = psutil.Popen([sys.executable, "-c", child])
    try:
        result = asyncio.run(run_live(adapter=adapter, req=CodingRequest(prompt=domain,
            workdir=tmp_path, timeout_s=5, allow_write=False), kill_switch=marker.exists))
        assert not result.ok and result.error == "kill_switch"
        assert unrelated.is_running()
        process = psutil.Process(int(marker.read_text())) if psutil.pid_exists(int(marker.read_text())) else None
        assert process is None or process.status() == psutil.STATUS_ZOMBIE
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("domain", DOMAINS)
def test_detection_uses_guarded_cli_fallback_without_claiming_subscription(monkeypatch, tmp_path, domain):
    from coding_agents.detect import detect
    from desktop_runtime import agent_status
    binary = tmp_path / ".local/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "codex-cli 0.163.0"\n')
    binary.chmod(0o755)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agent_status, "_probe", lambda *args, **kwargs: pytest.fail("Unapproved probe"))
    found = detect(path_env="/missing")
    row = next(item for item in found if item.name == "codex")
    assert row.binary == str(binary) and row.version == "unknown", domain
    assert row.auth_mode == "unknown"


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("field", ["api_key", "secret_access_key"])
def test_private_instruction_is_rejected_before_any_tool_process(monkeypatch, domain, field):
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=None, authorize=lambda: True)
    monkeypatch.setattr(coding, "preflight", lambda: pytest.fail("Private instruction reached a tool"))
    with pytest.raises(ValueError, match="coding_instruction_private"):
        coding.run(domain + f" {field}=" + "x" * 25, time.monotonic() + 10)
    assert coding.spawn_attempted is False


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider,version", [("codex", "codex-cli 0.163.0"), ("claude", "2.1.284")])
def test_version_probes_return_only_public_version_not_cli_diagnostics(monkeypatch, tmp_path, domain, provider, version):
    from coding_agents.detect import _version
    binary = tmp_path / provider
    binary.write_text(f'#!/bin/sh\necho "{version} api_key={"x" * 25}"\n')
    binary.chmod(0o755)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider=provider,
        tracker=tracker, authorize=lambda: False)
    coding.loop_mode = True
    try:
        assert coding._provider_version(str(binary)) == version, domain
        assert _version(str(binary)) == version
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("subscription", [False, True])
def test_local_adapter_uses_verified_guarded_session_without_live_flag(
        monkeypatch, tmp_path, domain, provider, subscription):
    from types import SimpleNamespace
    from coding_agents import load_adapter
    from coding_agents.protocol import CodingRequest, AgentEventKind
    from desktop_runtime import agent_status
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "api_key")
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "Documents/Supymem/ventures/fixture"
    workspace.mkdir(parents=True)
    (workspace / ".supymem-venture").write_text("ven_fixture\n")
    monkeypatch.setattr(agent_status, "_status", lambda _provider: {
        "subscription_login": subscription, "available": subscription})
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: name == "build_with_my_tool"))
    calls = []
    def guarded_run(coding, prompt, deadline):
        calls.append(prompt)
        assert coding.loop_mode is True and coding.authorize()
        (coding._workspace() / "index.html").write_text(domain)
        coding.line_callback(json.dumps({"type": "message", "text": domain}).encode())
        coding.line_callback(json.dumps({"type": "finished"}).encode())
        return {"outcome": "pass"}
    monkeypatch.setattr(OwnedCoding, "run", guarded_run)
    owned_coding._trusted_git(workspace, "init", "--quiet", "--initial-branch=main")
    adapter = load_adapter(provider)
    result = asyncio.run(adapter.run(CodingRequest(prompt=domain, workdir=workspace)))
    assert result.ok is subscription
    assert calls == ([domain] if subscription else [])
    if subscription:
        assert result.changed_files == ["index.html"]
        assert result.events[-1].kind == AgentEventKind.FINISHED
        assert result.argv == []
    else:
        assert "API-key login is not used" in result.error
        assert "Settings > Keys and connections > Coding tools" in result.error
    if provider == "claude":
        assert "--bare" not in adapter.build_argv(CodingRequest(prompt=domain, workdir=workspace))


def test_claude_uses_only_strict_sandboxed_bash_and_subscription_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "platform-key-must-not-reach-provider")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-provider")
    monkeypatch.setenv("USER", "qa-founder")
    monkeypatch.setenv("LOGNAME", "qa-founder")
    coding = OwnedCoding(venture_id="ven_one", workspace_slug="one", provider="claude",
                         tracker=None, authorize=lambda: True)
    argv = coding._argv("/bin/claude", "Implement the reviewed scope", tmp_path)
    assert argv[argv.index("--tools") + 1] == "Bash"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert {"--strict-mcp-config", "--safe-mode", "--disable-slash-commands", "--no-chrome",
            "--no-session-persistence"} <= set(argv)
    assert "--ide" not in argv
    assert "--dangerously-skip-permissions" not in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["sandbox"] == {
        "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False, "excludedCommands": [],
        "filesystem": {"disabled": False, "denyRead": [str(Path.home()), "/Volumes",
                        str(tmp_path / ".env"), str(tmp_path / ".env.*"),
                        str(tmp_path / "**/.env"), str(tmp_path / "**/.env.*"),
                        str(tmp_path / ".aws"), str(tmp_path / ".ssh"),
                        str(tmp_path / ".codex"), str(tmp_path / ".claude"),
                        str(tmp_path / ".cursor"), "/tmp", "/private/tmp", "/var/folders", "/private/var/folders"],
                       "allowRead": [str(tmp_path)], "allowWrite": [str(tmp_path / ".tmp")],
                       "denyWrite": ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders", str(tmp_path / ".reference")]},
        "network": {"allowedDomains": [], "deniedDomains": ["*"],
                    "allowLocalBinding": False, "allowAllUnixSockets": False}}
    assert "ANTHROPIC_API_KEY" not in coding._env()
    assert "AWS_SECRET_ACCESS_KEY" not in coding._env()
    assert coding._env()["USER"] == coding._env()["LOGNAME"] == "qa-founder"
    assert coding._env()["DISABLE_AUTOUPDATER"] == "1"


@pytest.mark.parametrize("version,result,error", [
    ("2.1.284", '{"type":"result","subtype":"success","is_error":false}', None),
    ("2.1.283", '{"type":"result","subtype":"success","is_error":false}', "upgrade_required"),
    ("2.1.284", '{"type":"result","subtype":"error_during_execution","is_error":true}', "result_unconfirmed"),
    ("2.1.284", '{"type":"assistant","text":"done"}', "result_unconfirmed"),
])
def test_claude_fixture_requires_supported_version_and_success_receipt(
        monkeypatch, tmp_path, version, result, error):
    from tests.coding_tool_fixtures import pin_test_tool

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    binary = tmp_path / "claude"
    binary.write_text(f'''#!/bin/sh
if [ "$1" = "--version" ]; then
  sleep 0.05
  echo "{version} (Claude Code)"
  exit 0
fi
echo implementation > product.txt
echo '{result}'
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    pin_test_tool(monkeypatch, "claude", binary)
    prompts = []
    popen = owned_coding.psutil.Popen
    def capture_prompt(argv, **kwargs):
        if "-p" in argv:
            prompts.append(argv[-1])
        return popen(argv, **kwargs)
    monkeypatch.setattr(owned_coding.psutil, "Popen", capture_prompt)
    scope = "Implement the reviewed scope. Prepare its coding card; do not execute until card approved."
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_claude", workspace_slug="claude-fixture",
                         provider="claude", tracker=tracker, authorize=lambda: True)
    try:
        if error:
            with pytest.raises(ValueError, match=f"coding_provider_{error}"):
                coding.run(scope, time.monotonic() + 10)
            if error == "upgrade_required":
                assert not (coding._workspace() / "product.txt").exists()
        else:
            receipt = coding.run(scope, time.monotonic() + 10)
            assert receipt["provider"] == "claude"
            assert receipt["artifact_verified"] is True
            assert receipt["task_acceptance_verified"] is False
            assert [entry["path"] for entry in receipt["changed_files"]] == ["product.txt"]
    finally:
        tracker.stop_all()
    assert tracker.stopped()
    if error != "upgrade_required":
        assert len(prompts) == 1 and prompts[0].endswith(scope)
        assert "coding card/order is approved is satisfied for this run" in prompts[0]
    else:
        assert prompts == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_successful_authority_refresh_cannot_erase_cancel_or_deadline(
        monkeypatch, tmp_path, cancelled):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    tracker = SimpleNamespace(register=lambda _p: None, capture=lambda: None, quiesce=lambda: True)
    checks = iter([False, False, False, cancelled])
    coding = OwnedCoding(venture_id="ven_one", workspace_slug="one", provider="codex",
                         tracker=tracker, authorize=lambda: True,
                         cancelled=lambda: next(checks, cancelled))
    monkeypatch.setattr(coding, "preflight", lambda: "/bin/codex")
    monkeypatch.setattr(coding, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(coding, "_git_state", lambda _p: {})
    monkeypatch.setattr(coding, "_stop_process_group", lambda _p: None)
    polls = iter([None, 0])
    process = SimpleNamespace(poll=lambda: next(polls, 0), returncode=0,
                              wait=lambda **_kw: None)
    monkeypatch.setattr(owned_coding.psutil, "Popen", lambda *a, **kw: process)
    clock = iter([0, 0, 0, 0, 0, 0, 5])
    monkeypatch.setattr(owned_coding, "time", SimpleNamespace(
        monotonic=lambda: next(clock, 5), sleep=lambda _s: None))
    with pytest.raises(ValueError, match="coding_cancelled_or_expired"):
        coding.run("Implement the reviewed scope", 100 if cancelled else 5)


def test_claude_preflight_finds_subscription_cli_outside_finder_path(monkeypatch, tmp_path):
    from desktop_runtime.agent_status import pin_provider

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    binary = tmp_path / ".local/bin/claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\ncase \"$1\" in\n"
        "--version) echo '2.1.284';;\n"
        "auth) echo '{\"loggedIn\":true,\"authMethod\":\"claude.ai\"}';;\n"
        "*) exit 97;;\nesac\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    pin_provider("claude")
    coding = OwnedCoding(venture_id="ven_one", workspace_slug="one", provider="claude",
                         tracker=None, authorize=lambda: True)
    assert coding.preflight() == str(binary)


def test_minimal_finder_environment_retains_keychain_user_identity(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setattr(owned_coding.getpass, "getuser", lambda: "qa-founder")
    env = OwnedCoding._env()
    assert env["USER"] == env["LOGNAME"] == "qa-founder"


@pytest.mark.parametrize('scope', [
    'Use Codex to build current approved task. Prepare its exact coding card for review; do not execute until card approved.',
    'Prepare documentation-only CODING_CARD.md. Do not implement the landing page, even after order approval.',
])
@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_approved_execution_phase_preserves_original_focus_and_all_other_gates(tmp_path, scope, provider):
    coding = OwnedCoding(venture_id='ven_one', workspace_slug='one', provider=provider,
                         tracker=None, authorize=lambda: True)
    prompt = coding._approved_execution_instruction(scope)
    assert prompt.endswith('Original immutable reviewed instruction (verbatim):\n' + scope)
    assert 'coding card/order is approved is satisfied for this run' in prompt
    assert 'keep it documentation-only' in prompt
    assert 'Do not broaden the focus' in prompt
    assert 'source acceptance, QA completion, publication, deployment' in prompt
    argv = coding._argv('/bin/' + provider, prompt, tmp_path)
    assert argv[-1] == prompt


def test_denied_coding_authority_never_constructs_approved_provider_prompt(monkeypatch, tmp_path):
    owned_coding._trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    coding = OwnedCoding(venture_id='ven_one', workspace_slug='one', provider='codex',
                         tracker=None, authorize=lambda: False)
    monkeypatch.setattr(coding, 'preflight', lambda: '/bin/codex')
    monkeypatch.setattr(coding, '_workspace', lambda: tmp_path)
    monkeypatch.setattr(coding, '_git_state', lambda _: {})
    def forbidden(*args, **kwargs):
        pytest.fail('Denied authority must not prepare or launch approved execution')
    monkeypatch.setattr(coding, '_approved_execution_instruction', forbidden)
    monkeypatch.setattr(owned_coding.psutil, 'Popen', forbidden)
    with pytest.raises(ValueError, match='coding_authority_unconfirmed'):
        coding.run('Implement root index.html', time.monotonic() + 10)
