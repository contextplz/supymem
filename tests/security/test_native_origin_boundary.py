"""Native IPC production contracts; deterministic synthetic inputs, no GUI effects."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_native_origin_and_private_file_contract():
    if not shutil.which("cargo"):
        pytest.skip("Rust tests run in native-manifest workflow; cargo unavailable here")
    subprocess.run(["cargo", "test", "--locked", "--manifest-path", str(ROOT / "tests/native_manifest/Cargo.toml"),
                    "native_security::tests"], check=True, capture_output=True, text=True)


def test_production_capability_and_csp():
    cap = json.loads((ROOT / "src-tauri/capabilities/default.json").read_text())
    assert cap["remote"]["urls"] == ["https://app.supymem.com", "https://app.supymem.com/*"]
    assert not any(isinstance(permission, dict) and permission.get("identifier") == "shell:allow-execute"
                   for permission in cap["permissions"])
    csp = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())["app"]["security"]["csp"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp
    assert "object-src 'none'" in csp and "base-uri 'none'" in csp


def test_sensitive_ipc_guards_precede_effects():
    source = (ROOT / "src-tauri/src/lib.rs").read_text()
    assert "fn load_passphrase(" not in source
    assert '"load_passphrase"' not in (ROOT / "src-tauri/permissions/app.toml").read_text()
    for name in ("store_passphrase", "clear_passphrase", "write_hermes_files", "write_hermes_config",
                 "install_local_builder", "uninstall_local_builder", "install_docker", "start_docker",
                 "spawn_coding_control", "spawn_agent_session", "stdio_write", "pty_write",
                 "start_cursor_ide_session", "reveal_in_file_manager", "relaunch_app"):
        command = source.split("fn " + name + "(", 1)[1].split("#[tauri::command]", 1)[0]
        assert re.search(r"-> Result<.*?>\s*\{\s*require_native_(?:command|local)_window\(&window, &state\)\?;", command, re.S), name
    for name in ("store_passphrase", "clear_passphrase", "write_hermes_files"):
        command = source.split("fn " + name + "(", 1)[1].split("#[tauri::command]", 1)[0]
        assert "require_native_local_window(&window, &state)?;" in command


def test_reveal_selects_file_without_launching_its_contents():
    source = (ROOT / "src-tauri/src/lib.rs").read_text()
    helper = source.split("fn reveal_path(path: String)", 1)[1].split("#[tauri::command]", 1)[0]
    assert 'Command::new("/usr/bin/open").arg("-R").arg(&p)' in helper
    assert "open::that(&p)" not in helper
