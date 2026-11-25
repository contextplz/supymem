"""Fast signed dogfood builds retain the PyInstaller runtime entitlement."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_fast_signed_runtime_uses_library_validation_entitlement(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "desktop_fast_prepare", ROOT / "scripts/desktop/prepare_fast_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    (tmp_path / "src-tauri").mkdir()
    identity = "Developer ID Application: Test Founder (TEAM123456)"
    monkeypatch.setenv("APPLE_SIGNING_IDENTITY", identity)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: identity)

    module.main()

    macos = json.loads((tmp_path / "src-tauri/tauri.release.json").read_text())["bundle"]["macOS"]
    assert macos == {"signingIdentity": identity, "hardenedRuntime": True,
                     "entitlements": "entitlements.plist"}
