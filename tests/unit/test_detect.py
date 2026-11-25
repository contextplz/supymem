from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coding_agents.detect import detect
from tests.coding_tool_fixtures import private_test_install


@pytest.mark.usefixtures("private_coding_install")
def test_detect_fake_binaries(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("cursor-agent", "claude", "codex"):
        p = bin_dir / name
        p.write_text("#!/bin/sh\necho 1.0.0\n")
        p.chmod(0o755)
    found = detect(path_env=str(bin_dir))
    names = {f.name for f in found}
    assert names == {"cursor", "claude", "codex"}

    assert all(agent.auth_mode == "unknown" for agent in found)


@pytest.mark.parametrize("domain", ["warehouse scheduling", "personal recipe journal"], ids=["business", "consumer"])
@pytest.mark.parametrize("binary,provider", [("cursor-agent", "cursor"), ("agent", "cursor"), ("opencode", "opencode")])
def test_detect_unapproved_tools_and_cursor_alias_without_login(tmp_path, monkeypatch, domain, binary, provider):
    directory = tmp_path / domain
    directory.mkdir(mode=0o700)
    executable = directory / binary
    version = "2026.04.17-787b533" if provider == "cursor" else "1.18.23"
    arguments = ["status"] if provider == "cursor" else ["auth", "list"]
    login = "Logged in as person@example.invalid" if provider == "cursor" else "●  OpenAI oauth"
    executable.write_text(f"#!{sys.executable}\nimport sys\n" +
        f"if sys.argv[1:] == ['--version']: print({version!r})\n" +
        f"elif sys.argv[1:] == {arguments!r}: print({login!r})\n" +
        "else: sys.exit(97)\n")
    executable.chmod(0o755)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    private_test_install(monkeypatch, tmp_path)
    found = detect(path_env=str(directory))
    assert len(found) == 1
    assert found[0].name == provider
    assert found[0].binary == str(executable)
    assert found[0].auth_mode == found[0].version == "unknown"


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
def test_local_detection_does_not_probe_unapproved_tools(tmp_path, monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    private_test_install(monkeypatch, tmp_path)
    calls = tmp_path / "calls.jsonl"
    versions = {"claude": "2.1.284", "codex": "codex-cli 0.138.0",
                "cursor-agent": "2026.11.15-current", "opencode": "1.18.23"}
    logins = {"claude": '{"loggedIn":true,"authMethod":"claude.ai"}',
              "codex": "Logged in using ChatGPT", "cursor-agent": "Logged in as person@example.invalid",
              "opencode": "● OpenAI oauth"}
    for name in versions:
        binary = tmp_path / name
        binary.write_text(f"#!{sys.executable}\n" +
            "import json, sys\nfrom pathlib import Path\n" +
            f"with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps([{name!r}, sys.argv[1:]]) + '\\n')\n" +
            f"print({versions[name]!r} if sys.argv[1:] == ['--version'] else {logins[name]!r})\n")
        binary.chmod(0o755)
    for _request in range(4):
        found = detect(path_env=str(tmp_path))
        assert len(found) == 4, domain
        assert all(agent.auth_mode == "unknown" for agent in found)
    assert not calls.exists()


@pytest.mark.parametrize("domain", ["Freight invoicing", "Weekend cycling"])
def test_version_probe_uses_the_bounded_group_runner(monkeypatch, domain):
    import importlib
    detection = importlib.import_module("coding_agents.detect")
    from desktop_runtime import agent_status
    calls = []
    def probe(binary, arguments):
        calls.append((binary, arguments))
        return 0, b"1.2.3\n"
    monkeypatch.setattr(agent_status, "_probe", probe)
    assert detection._version("/missing/" + domain) == "1.2.3"
    assert calls == [("/missing/" + domain, ["--version"])]
