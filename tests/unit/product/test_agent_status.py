"""Only documented read-only commands, with no credentials or identity receipts."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_runtime import agent_status


pytestmark = pytest.mark.usefixtures("private_coding_install")


@pytest.fixture
def windows_tool(monkeypatch, tmp_path, domain):
    from desktop_runtime import owned_coding

    install = tmp_path / 'Program Files' / domain
    install.mkdir(parents=True)
    binary = install / 'codex.exe'
    binary.write_bytes(b'fixture executable')
    binary.chmod(0o700)
    monkeypatch.setenv('SUPYMEM_MODE', 'local')
    monkeypatch.setenv('COMPANY_OS_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.chdir(tmp_path)
    (tmp_path / binary.name).write_bytes(b'ignore current directory')
    monkeypatch.setenv('PATH', f'.:{tmp_path}:{install}')
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(owned_coding, '_windows_acl_safe', lambda path: True, raising=False)
    return binary


@pytest.mark.parametrize('domain', ['freight-invoices', 'garden-journal'])
@pytest.mark.parametrize('provider', ['codex', 'opencode'])
def test_windows_probe_launches_the_absolute_approved_executable(monkeypatch, windows_tool, provider):
    if provider == 'opencode':
        windows_tool = windows_tool.rename(windows_tool.with_name('opencode.exe'))
    assert agent_status.provider_binary(provider) == str(windows_tool)
    expected = agent_status.launch_identity(windows_tool)
    popen = agent_status.psutil.Popen
    version = '2.2.5' if provider == 'opencode' else 'codex-cli 0.140.0'

    def launch(argv, **options):
        assert argv == [str(windows_tool), '--version']
        assert options.pop('executable') == str(windows_tool)
        assert not options.pop('start_new_session')
        assert options['env']['PATH'] == str(windows_tool.parent)
        assert not options.get('shell')
        return popen([sys.executable, '-c', f'print({version!r})'], **options)

    monkeypatch.setattr(agent_status.psutil, 'Popen', launch)
    assert agent_status._probe(str(windows_tool), ['--version'], expected=expected, opencode=provider == 'opencode') == (
        0, (version + '\n').encode())


@pytest.mark.parametrize('domain, username', [('freight.example', 'operator'), ('garden.test', None)])
def test_windows_desktop_identity_can_approve_and_reload(monkeypatch, windows_tool, tmp_path, username):
    from desktop_runtime import owned_coding

    profile = tmp_path / 'Your profile ü'
    monkeypatch.setenv('USERPROFILE', str(profile))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key-not-for-probes')
    for name in ('USER', 'LOGNAME', 'USERNAME'):
        monkeypatch.delenv(name, raising=False)
    if username:
        monkeypatch.setenv('USERNAME', username)
    git = windows_tool.with_name('git.exe')
    git.write_bytes(b'git fixture')
    profile.mkdir()
    monkeypatch.chdir(profile)
    monkeypatch.setenv('PATH', str(windows_tool.parent))
    monkeypatch.setattr(owned_coding.getpass, 'getuser', lambda: pytest.fail('Windows should not call getpass'))
    launcher = agent_status.psutil.Popen

    def launch(argv, **options):
        assert options.pop('executable') == str(windows_tool)
        assert options['env']['USERPROFILE'] == str(profile)
        assert options['env'].get('USERNAME') == username
        assert 'OPENAI_API_KEY' not in options['env']
        output = 'codex-cli 0.140.0' if argv[1:] == ['--version'] else 'Logged in using ChatGPT'
        return launcher([sys.executable, '-c', f'print({output!r})'], **options)

    monkeypatch.setattr(agent_status.psutil, 'Popen', launch)
    candidate = agent_status._status('codex')['candidate']
    identity = agent_status.pin_provider('codex', candidate=candidate)
    assert agent_status.provider_pin('codex') == identity
    assert agent_status._status('codex')['available']


@pytest.mark.parametrize('domain', ['freight.example', 'garden.test'])
def test_windows_build_readiness_requires_trusted_git(monkeypatch, windows_tool, tmp_path):
    from desktop_runtime import owned_coding

    git = windows_tool.with_name('git.exe')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('PATH', str(windows_tool.parent))
    monkeypatch.setattr(agent_status, '_probe', lambda _, args, **kwargs:
        (0, b'codex-cli 0.140.0' if args == ['--version'] else b'Logged in using ChatGPT'))
    identity = agent_status.launch_identity(windows_tool)
    for installed, safe in [(False, True), (True, False), (True, True)]:
        if installed:
            git.write_bytes(b'git fixture')
        monkeypatch.setattr(owned_coding, '_windows_acl_safe', lambda path: safe or path != git)
        status = agent_status._status('codex', expected=identity)
        assert status['signed_in'] and status['available'] is (installed and safe)
        if not status['available']:
            assert 'Git for Windows' in status['build_blocked_message']
            assert 'Coding tools' in status['build_blocked_message']
            with pytest.raises(ValueError, match='coding_git_not_installed'):
                agent_status.pin_provider('codex')
            with pytest.raises(ValueError, match='coding_git_not_installed'):
                owned_coding._trusted_git(workspace, 'init')
        else:
            assert agent_status.pin_provider('codex') == identity


@pytest.mark.parametrize('domain', ['freight-invoices', 'garden-journal'])
def test_windows_refuses_tool_when_parent_permissions_are_unsafe_or_unverified(monkeypatch, windows_tool):
    from desktop_runtime import owned_coding

    for permission in (False, OSError('ACL unavailable')):
        def check_acl(path):
            if path != windows_tool.parent.parent:
                return True
            if isinstance(permission, OSError):
                raise permission
            return permission

        monkeypatch.setattr(owned_coding, '_windows_acl_safe', check_acl)
        monkeypatch.setattr(agent_status.psutil, 'Popen', lambda *args, **kwargs: pytest.fail('unsafe launch'))
        status = agent_status._status('codex')
        assert status['unsafe_location'] and not status['available']
        assert 'unsafe location' in status['unsafe_location_message']
        assert 'permissions' in status['unsafe_location_message']
        with pytest.raises(ValueError, match='coding_provider_unsafe_location'):
            agent_status.pin_provider('codex')


@pytest.mark.parametrize('domain', ['freight-invoices', 'garden-journal'])
def test_windows_refuses_file_index_change_at_launch(monkeypatch, tmp_path, windows_tool):
    import os

    expected = agent_status.launch_identity(windows_tool)
    replacement = tmp_path / 'replacement.exe'
    replacement.write_bytes(windows_tool.read_bytes())
    replacement.chmod(0o700)
    os.utime(replacement, ns=(windows_tool.stat().st_atime_ns, windows_tool.stat().st_mtime_ns))
    replacement.replace(windows_tool)
    assert windows_tool.stat().st_ino != expected[2]
    monkeypatch.setattr(agent_status.psutil, 'Popen', lambda *args, **kwargs: pytest.fail('changed launch'))
    with pytest.raises(ValueError, match='coding_provider_changed'):
        agent_status._run_probe(str(windows_tool), ['--version'], None, tmp_path, {'PATH': ''}, expected)


@pytest.mark.parametrize(('domain', 'suffix', 'entry'), [
    ('freight-invoices', '.cmd', r'%dp0%\node_modules\@openai\codex\bin\codex.js'),
    ('garden-journal', '.ps1', '$basedir/../@openai/codex/bin/codex.js'),
])
def test_windows_npm_shim_executes_neither_batch_nor_powershell_and_pins_both_files(
        monkeypatch, windows_tool, suffix, entry):
    from desktop_runtime import owned_coding

    node = windows_tool.with_name('node.exe')
    windows_tool.rename(node)
    script = node.parent / 'node_modules/@openai/codex/bin/codex.js'
    script.parent.mkdir(parents=True)
    script.write_text('#!/usr/bin/env node\n')
    shim = windows_tool.with_suffix(suffix)
    if '/../' in entry:
        shim = node.parent / 'node_modules/.bin' / shim.name
        shim.parent.mkdir()
        monkeypatch.setenv('PATH', f'{shim.parent}:{node.parent}')
    shim.write_text(f'node "{entry}"')
    assert agent_status.provider_binary('codex') == str(shim)
    with monkeypatch.context() as setup:
        setup.setattr(agent_status, '_status', lambda *args, **kwargs: {'available': True})
        expected = agent_status.pin_provider('codex')
    assert agent_status.build_provider_pin('codex') == (expected, None)
    assert [part[0] for part in expected[6:]] == [str(node), str(script)]
    with owned_coding.verified_launch([str(shim), '--version'], expected, {'PATH': ''}) as (argv, options):
        assert argv == [str(node), str(script), '--version']
        assert options['executable'] == str(node)
    for path in (node, script):
        previous = path.read_bytes()
        path.write_bytes(previous + b'updated')
        with pytest.raises(ValueError, match='coding_provider_changed'):
            with owned_coding.verified_launch([str(shim)], expected, {'PATH': ''}):
                pytest.fail('changed launch')
        path.write_bytes(previous)
        expected = agent_status.launch_identity(shim)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX tool permissions")
@pytest.mark.parametrize("domain", ["freight-invoices", "garden-journal"])
def test_discovery_and_legacy_build_do_not_run_unapproved_path_tools(monkeypatch, tmp_path, domain):
    from desktop_runtime.owned_coding import OwnedCoding
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda subject: {"codex": True})
    monkeypatch.setattr("app.workspaces.resolve.local_person", lambda: SimpleNamespace(id="owner"))
    directory = tmp_path / domain
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    binary = directory / "codex"
    binary.write_text("#!/bin/sh\nprintf 'codex-cli 1.0.0\\n'\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setattr(agent_status, "_probe", lambda *args, **kwargs: pytest.fail("Unapproved probe"))
    status = agent_status._status("codex")
    assert status["unsafe_location"] and not status["available"]
    directory.chmod(0o770)
    with monkeypatch.context() as permissions:
        permissions.setattr("grp.getgrgid", lambda gid: SimpleNamespace(gr_name="foreign-tool-group"))
        assert agent_status._status("codex")["unsafe_location"]
    directory.chmod(0o700)
    status = agent_status._status("codex")
    assert status["candidate_path"] == str(binary) and not status["available"]
    candidate = status["candidate"]
    binary.write_text("#!/bin/sh\nprintf 'changed'\n")
    with pytest.raises(ValueError, match="coding_sign_in_required"):
        agent_status.pin_provider("codex", candidate=candidate)
    build = OwnedCoding(venture_id="ven_fixture", workspace_slug=domain, provider="codex", tracker=None, authorize=lambda: True)
    with pytest.raises(ValueError, match="coding_tool_not_connected"):
        build.preflight()
    assert agent_status.provider_pin("codex") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX tool permissions and Bash")
@pytest.mark.parametrize("domain", ["freight-invoices", "garden-journal"])
@pytest.mark.parametrize("shape", ["brew", "cursor", "non_admin", "world_writable", "acl_write",
                                  "unknown_home_acl", "unknown_system_acl"])
def test_mac_installs_only_allow_the_owner_or_administrators_to_change_them(monkeypatch, tmp_path, domain, shape):
    import grp
    import os
    import pwd
    import stat

    from desktop_runtime import owned_coding

    host_platform = sys.platform
    home = tmp_path / "Users" / "founder"
    is_cursor = shape in {"cursor", "unknown_home_acl"}
    prefix = home / ".local" if is_cursor else tmp_path / "opt/homebrew"
    provider, name = ("cursor", "cursor-agent") if is_cursor else ("codex", "codex")
    launcher = prefix / "bin" / name
    version = prefix / ("share/cursor-agent/versions/1" if is_cursor else "Caskroom/codex/0.163.0")
    target = version / name if is_cursor else version / "bin" / name
    launcher.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    (home / domain).mkdir(parents=True)
    ambient = home / domain / "bash"
    ambient.write_bytes(b"not the approved runtime")
    ambient.chmod(0o700)
    target.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "${BASH_SOURCE[0]}"\n' if is_cursor else "#!/bin/sh\n")
    target.chmod(0o755)
    launcher.symlink_to(target)

    uid = os.geteuid()
    group_names = {0: "wheel", 20: "staff", 80: "admin"}
    monkeypatch.setattr(grp, "getgrgid", lambda gid: SimpleNamespace(gr_name=group_names[gid]))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="founder"))
    lstat = Path.lstat

    def permissions(path):
        info = lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=uid if path.is_relative_to(tmp_path) else 0)
        owner, group, mode = 0, 0, 0o755
        if path == home.parent:
            group = 80
        elif path.is_relative_to(home):
            owner, group, mode = uid, 20, 0o750 if path == home else 0o755
        elif path.is_relative_to(prefix):
            owner, group = uid, 80
            if shape not in {"acl_write", "unknown_system_acl"} and path in {launcher.parent, prefix / "Caskroom"}:
                mode = 0o775
        if path == launcher.parent and shape == "non_admin":
            group = 20
        if path == launcher.parent and shape == "world_writable":
            mode = 0o1777
        return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=owner, st_gid=group,
                               st_dev=info.st_dev, st_ino=info.st_ino)

    run = subprocess.run

    def has_acl(path):
        if shape == "unknown_home_acl" and path == home:
            raise PermissionError("ACL unavailable")
        return (path == home or (path == launcher.parent and shape == "acl_write")
                or (path == prefix and shape == "unknown_system_acl"))

    def read_acl(argv, **options):
        assert argv[0] == "/bin/ls"
        path = Path(argv[-1])
        if path == home:
            output = b"drwxr-x---+ home\n 0: group:everyone deny delete\n"
        elif path == launcher.parent and shape == "acl_write":
            output = b"drwxr-xr-x+ bin\n 0: group:everyone allow write,delete_child\n"
        else:
            output = b"drwxr-xr-x  no-acl\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    monkeypatch.setattr(Path, "lstat", permissions)
    monkeypatch.setattr(owned_coding, "_mac_has_acl", has_acl, raising=False)
    monkeypatch.setattr(subprocess, "run", read_acl)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", f"{ambient.parent}:{launcher.parent}")
    status = agent_status._status(provider)
    if shape not in {"brew", "cursor", "unknown_home_acl"}:
        assert status["unsafe_location"] and not status["available"]
        with pytest.raises(ValueError, match="coding_provider_unsafe_location"):
            agent_status.pin_provider(provider)
        return

    assert status["candidate_path"] == str(target) and not status["available"]
    with monkeypatch.context() as setup:
        setup.setattr(agent_status, "_status", lambda *args, **kwargs: {"available": True, "subscription_login": True})
        expected = agent_status.pin_provider(provider, candidate=status["candidate"])
    assert agent_status.build_provider_pin(provider) == (expected, None)
    assert agent_status._read_pin(provider).install is not None
    if is_cursor:
        assert expected[6][0] == str(Path("/bin/bash").resolve())
        platforms = ("darwin", "linux") if host_platform == "linux" else ("darwin",)
        for platform in platforms:
            monkeypatch.setattr(sys, "platform", platform)
            with owned_coding.verified_launch([str(target)], expected, {"PATH": str(ambient.parent)}) as (argv, options):
                assert argv == [expected[6][0], str(target)]
                assert run(argv, capture_output=True, check=True, **options).stdout.decode().strip() == str(target)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor execution")
@pytest.mark.parametrize("domain", ["freight-invoices", "garden-journal"])
def test_probe_executes_the_opened_file_after_a_symlink_swap(monkeypatch, tmp_path, domain):
    binary = tmp_path / domain
    replacement = tmp_path / "replacement"
    for tool, output in ((binary, "approved"), (replacement, "replacement")):
        tool.write_text(f"#!/bin/sh\nprintf '{output}'\n")
        tool.chmod(0o700)
    real_popen = agent_status.psutil.Popen

    def swap_then_launch(*args, **kwargs):
        binary.rename(tmp_path / "original")
        binary.symlink_to(replacement)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(agent_status.psutil, "Popen", swap_then_launch)
    assert agent_status._probe(str(binary), [], expected=agent_status.executable_identity(binary)) == (0, b"approved")


@pytest.mark.skipif(sys.platform != "linux" or not Path("/usr/bin/node").exists(), reason="Linux descriptors and Node")
@pytest.mark.parametrize("domain", ["freight-invoices", "garden-journal"])
def test_node_wrapper_uses_pinned_runtime_and_keeps_relative_imports(monkeypatch, tmp_path, domain):
    binary = tmp_path / "wrapper.mjs"
    (tmp_path / "message.mjs").write_text(f"export default {json.dumps(domain)};\n")
    binary.write_text("#!/usr/bin/env node\nimport message from './message.mjs';\n"
                      "console.log(process.env.PATH.split(':')[0], message);\n")
    binary.chmod(0o700)
    ambient = tmp_path / "ambient"
    ambient.mkdir(mode=0o777)
    ambient.chmod(0o777)
    (ambient / "node").write_text("#!/bin/sh\nprintf 'wrong runtime'\n")
    (ambient / "node").chmod(0o700)
    monkeypatch.setenv("PATH", f"{ambient}:/usr/bin")
    from desktop_runtime import owned_coding

    safe_location = owned_coding._safe_location
    monkeypatch.setattr(owned_coding, "_safe_location",
                        lambda path: Path(path) == Path("/usr/bin/node") or safe_location(path))
    identity = owned_coding.launch_identity(binary)
    monkeypatch.setenv("PATH", str(ambient))
    code, output = agent_status._probe(str(binary), [], expected=identity)
    assert code == 0 and output == f"/usr/bin {domain}\n".encode()


@pytest.fixture
def fake_providers(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    binaries = {}
    for provider in ("claude", "codex", "cursor"):
        binary = tmp_path / provider
        binary.touch(mode=0o700)
        binaries[provider] = str(binary)
    monkeypatch.setattr(agent_status, "provider_binary", binaries.get)
    return binaries


@pytest.fixture
def approved_status(fake_providers):
    def status(provider):
        return agent_status._status(provider, expected=agent_status.launch_identity(fake_providers[provider]))

    return status


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
def test_ready_snapshot_is_single_flight_nonblocking_and_execution_stays_fresh(monkeypatch, tmp_path, domain):
    monkeypatch.setenv("PATH", str(tmp_path / domain))
    entered, release = threading.Event(), threading.Event()
    probes = []
    ready = True
    def status(provider):
        probes.append(provider)
        if not ready:
            entered.set()
            assert release.wait(2)
        return {"name": provider, "installed": True, "signed_in": ready,
                "subscription_login": ready, "available": ready}
    monkeypatch.setattr(agent_status, "_status", status)
    first = agent_status.statuses()
    assert len(probes) == 4
    first[0]["available"] = False
    assert all(item["available"] for item in agent_status.statuses())
    ready = False
    monkeypatch.setattr(agent_status, "_STATUS_CHECKED_AT", time.monotonic() - 31)
    try:
        started = time.perf_counter()
        for _ in range(20):
            assert all(item["available"] for item in agent_status.statuses())
        assert time.perf_counter() - started < 1
        assert entered.wait(1)
        assert 4 < len(probes) <= 8
    finally:
        release.set()
    assert not any(item["available"] for item in agent_status.statuses(refresh=True))
    assert agent_status._status("codex")["available"] is False


@pytest.mark.parametrize("domain", ["freight-invoices", "garden-journal"])
def test_status_cache_invalidates_when_app_rule_or_executable_changes(monkeypatch, tmp_path, domain):
    install = tmp_path / domain
    install.mkdir()
    binary = install / "codex"
    binary.write_text("old executable")
    binary.chmod(0o700)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(install / "data"))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.0.0")
    monkeypatch.setattr(agent_status, "_STATUS_RULE_VERSION", 1)
    monkeypatch.setattr(agent_status, "provider_binary",
                        lambda provider, path_env=None: str(binary) if provider == "codex" else None)
    verdict = {"name": "codex", "unsafe_location": True}
    monkeypatch.setattr(agent_status, "_scan_statuses", lambda: [verdict])
    assert agent_status.statuses() == [verdict]

    verdict = {"name": "codex", "candidate": "new-approval"}
    monkeypatch.setattr(agent_status, "_STATUS_RULE_VERSION", 2)
    assert agent_status.statuses() == [verdict]

    verdict = {"name": "codex", "available": True}
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.1.0")
    assert agent_status.statuses() == [verdict]

    verdict = {"name": "codex", "changed": True}
    binary.write_text("replacement executable")
    assert agent_status.statuses() == [verdict]


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_local_api_coding_tools_reports_real_status_without_secrets(monkeypatch, title):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.api.local_tools_api import local_coding_agents
    from fastapi import HTTPException, Response

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(agent_status, "statuses", lambda **kwargs: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True, "private": title}])
    response = Response()
    assert local_coding_agents(response) == {"tools": [{"name": "codex", "label": "codex", "kind": "ide",
        "installed": True, "connected": True, "signed_in": True, "coding_available": True,
        "cli_path": "codex", "app_path": None}]}
    assert response.headers["Cache-Control"] == "no-store"
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    with pytest.raises(HTTPException) as rejected:
        local_coding_agents(Response())
    assert rejected.value.status_code == 404


@pytest.mark.parametrize("provider,output,exit_code,ready", [
    ("claude", {"loggedIn": True, "authMethod": "claude.ai", "email": "private@example.invalid"}, 0, True),
    ("claude", {"loggedIn": True, "authMethod": "api_key"}, 0, False),
    ("claude", {"loggedIn": "true", "authMethod": "claude.ai"}, 0, False),
    ("claude", {"loggedIn": True, "authMethod": "claude.ai"}, 1, False),
    ("codex", "Logged in using ChatGPT", 0, True), ("codex", "Not logged in", 1, False),
    ("codex", "Logged in using an API key - fake-redacted-key", 0, False),
])
def test_status_commands_are_bounded_boolean_only(monkeypatch, fake_providers, approved_status, provider, output, exit_code, ready):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-pass")
    calls = []

    def probe(binary, arguments, **kwargs):
        calls.append([binary, *arguments])
        data = (b"2.1.284 (Claude Code)" if provider == "claude" else b"codex-cli 0.138.0") if arguments == ["--version"] else (
            json.dumps(output).encode() if isinstance(output, dict) else output.encode())
        return (0 if arguments == ["--version"] else exit_code), data

    monkeypatch.setattr(agent_status, "_probe", probe)
    result = approved_status(provider)
    assert result["available"] is ready
    assert calls[0] == [fake_providers[provider], "--version"]
    assert calls[1] == [fake_providers[provider], *( ["auth", "status"] if provider == "claude" else ["login", "status"])]
    assert set(result) == {"name", "installed", "signed_in", "subscription_login", "available"}
    assert all(isinstance(value, bool) for key, value in result.items() if key != "name")
    assert "private" not in json.dumps(result)


def test_timeout_and_old_or_malformed_claude_fail_closed(monkeypatch, approved_status):
    for payload in [b"not json", b"[]", b"[" * 2048 + b"0" + b"]" * 2048]:
        monkeypatch.setattr(agent_status, "_probe", lambda binary, args, **kwargs: (0, b"2.1.284" if args == ["--version"] else payload))
        assert approved_status("claude")["available"] is False
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)
    monkeypatch.setattr(agent_status, "_probe", timeout)
    assert approved_status("claude")["signed_in"] is False
    calls = []
    def old(binary, args, **kwargs):
        calls.append(args)
        assert args == ["--version"], "An old CLI must never receive an auth subcommand"
        return 0, b"2.1.283"
    monkeypatch.setattr(agent_status, "_probe", old)
    assert approved_status("claude")["available"] is False
    assert calls == [["--version"]]
    monkeypatch.setattr(agent_status, "_probe", lambda binary, args, **kwargs: (0,
        b"codex-cli 0.137.0" if args == ["--version"] else b"Logged in using ChatGPT"))
    assert approved_status("codex")["available"] is False


def test_missing_tools_never_run_an_auth_command(monkeypatch, fake_providers):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    fake_providers.clear()
    monkeypatch.setattr(agent_status, "_probe", lambda *a, **k: pytest.fail("unexpected process"))
    assert agent_status._status("codex")["installed"] is False
    assert agent_status._status("cursor") == {"name": "cursor", "installed": False,
        "signed_in": False, "subscription_login": False, "available": False, "changed": False}
    assert agent_status._status("opencode")["installed"] is False


def test_probe_bounds_output_and_scrubs_environment_before_ordinary_fake_commands(monkeypatch, tmp_path):
    binary = tmp_path / "probe"
    binary.write_text(f"#!{sys.executable}\nimport sys\nexec(sys.argv[1])\n")
    binary.chmod(0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-pass")
    popen = agent_status.subprocess.Popen
    def launch(argv, **kwargs):
        assert "OPENAI_API_KEY" not in kwargs["env"] and "ANTHROPIC_API_KEY" not in kwargs["env"]
        temporary = Path(kwargs["cwd"])
        assert str(temporary) == kwargs["env"]["TMPDIR"] and temporary.stat().st_mode & 0o777 == 0o700
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is True and kwargs["stderr"] == subprocess.STDOUT
        return popen(argv, **kwargs)
    monkeypatch.setattr(agent_status.subprocess, "Popen", launch)
    assert agent_status._probe(str(binary), ["import sys; print('ok', file=sys.stderr)"]) == (0, b"ok\n")
    with pytest.raises(ValueError, match="coding_status_unavailable"):
        agent_status._probe(str(binary), ["print('x' * 9000)"])
    clock = iter([0, 6])
    monkeypatch.setattr(agent_status.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="coding_status_unavailable"):
        agent_status._probe(str(binary), ["import time; time.sleep(10)"])


@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
@pytest.mark.parametrize("output", [
    b"Logged in using ChatGPT\nLogged in using an API key - must-not-leak",
    b"Logged in using ChatGPT\nWarning: login unavailable",
    b"unknown authentication",
    b"Not logged in",
])
def test_unknown_or_conflicting_codex_login_never_verifies_subscription(monkeypatch, approved_status, domain, output):
    monkeypatch.setattr(agent_status, "_probe", lambda binary, args, **kwargs: (0,
        b"codex-cli 0.163.0" if args == ["--version"] else output))
    result = approved_status("codex")
    assert result["installed"] is True
    assert result["available"] is False
    assert result["subscription_login"] is False
    assert result["signed_in"] is False
    assert domain not in json.dumps(result)
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("domain", ["inventory planner", "home fitness journal"], ids=["business", "consumer"])
def test_version_detection_accepts_one_version_after_launcher_diagnostics(monkeypatch, approved_status, provider, domain):
    version = b"2.1.284 (Claude Code)" if provider == "claude" else b"codex-cli 0.163.0"
    auth = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode() if provider == "claude" else b"Logged in using ChatGPT"
    monkeypatch.setattr(agent_status, "_probe", lambda binary, args, **kwargs: (0,
        b"WARNING: could not create PATH aliases\n" + version if args == ["--version"] else auth))
    assert approved_status(provider)["available"] is True
    monkeypatch.setattr(agent_status, "_probe", lambda binary, args, **kwargs: (0,
        version + b"\n" + version if args == ["--version"] else auth))
    assert approved_status(provider)["available"] is False


@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
@pytest.mark.parametrize("provider,version,arguments,output,ready", [
    ("cursor", "2026.04.17-787b533", ["status"], '{"isAuthenticated":true,"hasAccessToken":true,"hasRefreshToken":true}', True),
    ("cursor", "2026.04.17-787b533", ["status"], "Logged in as person@example.invalid", True),
    ("cursor", "2026.04.17-787b533", ["status"], '{"isAuthenticated":true,"hasAccessToken":true,"hasRefreshToken":false}', False),
    ("cursor", "2026.04.17-787b533", ["status"], '{"isAuthenticated":"true","hasAccessToken":true,"hasRefreshToken":true}', False),
    ("cursor", "2026.04.17-787b533", ["status"], "Not logged in", False),
    ("cursor", "2026.04.16-older", ["status"], "Logged in as person@example.invalid", True),
    ("opencode", "2.2.5", ["auth", "list"], "┌  Credentials\n│\n●  OpenAI oauth\n│\n└  1 credentials", True),
    ("opencode", "1.18.23", ["auth", "list"], "┌  Credentials\n│\n●  Anthropic api\n│\n└  1 credentials", False),
    ("opencode", "1.18.23", ["auth", "list"], "●  OpenAI oauth\n●  Anthropic api", False),
    ("opencode", "1.18.23", ["auth", "list"], "No credentials found", False),
    ("opencode", "1.18.22", ["auth", "list"], "●  OpenAI oauth", True),
])
def test_connected_tool_login_uses_its_own_fake_binary(monkeypatch, tmp_path, domain, provider, version, arguments, output, ready):
    binary = tmp_path / ("cursor-agent" if provider == "cursor" else provider)
    calls = tmp_path / "calls.jsonl"
    binary.write_text(f"#!{sys.executable}\n" +
        "import json, sys\nfrom pathlib import Path\n" +
        f"with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n" +
        f"if sys.argv[1:] == ['--version']: print({version!r})\n" +
        f"elif sys.argv[1:] == {arguments!r}: print({output!r})\n" +
        "else: sys.exit(97)\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert agent_status.provider_binary(provider) == str(binary)
    result = agent_status._status(provider, expected=agent_status.launch_identity(binary))
    assert result["installed"] is True
    assert result["available"] is (provider == "opencode" and version == "2.2.5" and ready)
    assert result["subscription_login"] is ready
    assert set(result) == {"name", "installed", "signed_in", "subscription_login", "available"}
    assert all(isinstance(value, bool) for key, value in result.items() if key != "name")
    assert domain not in json.dumps(result)
    assert "person@" not in json.dumps(result)
    assert json.loads(calls.read_text().splitlines()[0]) == ["--version"]
    assert json.loads(calls.read_text().splitlines()[1]) == arguments


@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
def test_cursor_resolution_prefers_its_cli_over_an_unrelated_agent(monkeypatch, tmp_path, domain):
    home = tmp_path / domain
    primary = home / ".local/bin/cursor-agent"
    primary.parent.mkdir(parents=True)
    primary.write_text("#!/bin/sh\nexit 0\n")
    primary.chmod(0o755)
    alias = tmp_path / "agent"
    alias.write_text("#!/bin/sh\nexit 0\n")
    alias.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(agent_status.Path, "home", lambda: home)
    assert agent_status.tool_binary("cursor") == str(primary)


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
@pytest.mark.parametrize("provider", ["cursor", "opencode"])
def test_subscription_login_without_isolation_support_is_not_build_ready(monkeypatch, tmp_path, domain, provider):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda _subject: {provider: True})
    monkeypatch.setattr("app.workspaces.resolve.local_person", lambda: SimpleNamespace(id=domain))
    binary = tmp_path / ("cursor-agent" if provider == "cursor" else provider)
    version = "2026.11.15-current" if provider == "cursor" else "1.18.23"
    arguments = ["status"] if provider == "cursor" else ["auth", "list"]
    login = "Logged in as person@example.invalid" if provider == "cursor" else "● OpenAI oauth"
    binary.write_text(f"#!{sys.executable}\nimport sys\n"
        f"if sys.argv[1:] == ['--version']: print({version!r})\n"
        f"elif sys.argv[1:] == {arguments!r}: print({login!r})\n"
        "else: sys.exit(97)\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    status = agent_status._status(provider, expected=agent_status.executable_identity(binary))
    assert status["signed_in"] is status["subscription_login"] is True
    assert status["available"] is False
    coding = agent_status.OwnedCoding(venture_id="ven_fixture", workspace_slug=domain,
        provider=provider, tracker=None, authorize=lambda: True)
    with pytest.raises(ValueError, match="coding_sign_in_required"):
        agent_status.pin_provider(provider)
    assert agent_status.provider_pin(provider) is None
    with pytest.raises(ValueError, match="coding_tool_not_connected"):
        coding.preflight()
    with pytest.raises(ValueError, match="coding_provider_upgrade_required"):
        coding._provider_version(str(binary))
    assert "person@" not in json.dumps(status)
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.build import device_orders
    monkeypatch.setattr(device_orders, "is_local", lambda: True)
    monkeypatch.setattr(device_orders, "coding_descriptor", lambda manifest: {"provider": provider})
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {provider: True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [status])
    with pytest.raises(ValueError, match="device_coding_subscription_required"):
        device_orders.assert_coding_loop_available({}, "owner")


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
@pytest.mark.parametrize("output,ready", [
    ("Cursor Agent Status\n\n✓ Logged in as person@example.invalid\n", True),
    ("\x1b[32m● Logged in as person@example.invalid\x1b[0m\n", True),
    ("Logged in as person@example.invalid\nNot logged in", False),
    ("[]", False),
    ('{"isAuthenticated":true,"hasAccessToken":true,"hasRefreshToken":true}', True),
])
def test_cursor_status_banner_proves_login_without_returning_identity(monkeypatch, approved_status, domain, output, ready):
    monkeypatch.setattr(agent_status, "_probe", lambda binary, arguments, **kwargs: (0,
        b"2026.11.15-current" if arguments == ["--version"] else output.encode()))
    result = approved_status("cursor")
    assert result["available"] is False
    assert result["subscription_login"] is ready
    assert "person@" not in json.dumps(result)
    assert domain not in json.dumps(result)


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
@pytest.mark.parametrize("provider", ["claude", "codex", "cursor", "opencode"])
@pytest.mark.parametrize("shutdown", [False, True], ids=["timeout", "app_exit"])
def test_probe_kills_its_group_even_when_child_ignores_termination(tmp_path, domain, provider, shutdown):
    binary = tmp_path / provider
    pids = tmp_path / "pids.json"
    binary.write_text(f"#!{sys.executable}\n" +
        "import json, os, signal, sys, time\nfrom pathlib import Path\n" +
        "if sys.argv[1:] == ['--version']: print('reopened'); raise SystemExit\n" +
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" +
        "child = os.fork()\n" +
        "if child:\n" +
        f"    Path({str(pids)!r}).write_text(json.dumps([os.getpid(), child]))\n" +
        "while True: time.sleep(1)\n")
    binary.chmod(0o755)
    script = f"""
import ctypes, json, os, subprocess, sys, threading, time
from pathlib import Path
from pytest import MonkeyPatch
from desktop_runtime import agent_status
from tests.coding_tool_fixtures import private_test_install
private_test_install(MonkeyPatch(), Path({str(tmp_path.parent)!r}))
if sys.platform == 'linux':
    ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)
agent_status._PROBE_TIMEOUT_SECONDS = .3
errors = []
def probe():
    try:
        agent_status._probe({str(binary)!r}, ['status'])
    except ValueError as error:
        errors.append(str(error))
thread = threading.Thread(target=probe)
thread.start()
deadline = time.monotonic() + 2
while not Path({str(pids)!r}).exists() and time.monotonic() < deadline:
    time.sleep(.01)
assert Path({str(pids)!r}).exists()
unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL, start_new_session=True)
if {shutdown!r}:
    try: agent_status.stop_probes()
    except AttributeError: errors.append('shutdown unavailable')
thread.join(2)
stopped_promptly = not thread.is_alive()
thread.join(7)
unrelated_alive = unrelated.poll() is None
unrelated.terminate()
unrelated.wait(timeout=2)
if sys.platform == 'linux':
    while True:
        try: os.waitpid(-1, 0)
        except ChildProcessError: break
for process_id in json.loads(Path({str(pids)!r}).read_text()):
    try: os.kill(process_id, 0)
    except ProcessLookupError: continue
    raise AssertionError('probe process survived')
assert stopped_promptly
assert unrelated_alive
assert errors == ['coding_status_unavailable']
if {shutdown!r}:
    try: agent_status._probe({str(binary)!r}, ['--version'])
    except ValueError: pass
    else: raise AssertionError('probe started after shutdown')
    agent_status.start_probes()
    assert agent_status._probe({str(binary)!r}, ['--version']) == (0, b'reopened\\n')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
def test_local_tool_requests_reuse_status_cache(monkeypatch, domain):
    from fastapi import Response

    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.api.local_tools_api import local_coding_agents

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    def statuses(**options):
        calls.append(options)
        return []
    monkeypatch.setattr(agent_status, "statuses", statuses)
    for _request in range(3):
        assert local_coding_agents(Response()) == {"tools": []}, domain
    assert calls == [{}, {}, {}]
