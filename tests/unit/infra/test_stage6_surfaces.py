"""Stage 6 surfaces: health apps import and workflow YAML parse."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_ai_proxy_health() -> None:
    import importlib.util

    from fastapi.testclient import TestClient

    path = ROOT / "services" / "ai_proxy" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("ai_proxy_main", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = TestClient(mod.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "ai-proxy"
    denied = client.post("/v1/chat/completions", json={"model": "supymem-flash", "messages": []})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_api_key"


def test_workflows_parse() -> None:
    for name in ("ci.yml", "images.yml", "deploy-staging.yml"):
        path = ROOT / ".github" / "workflows" / name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["name"]
        assert "jobs" in data


def test_no_shell_true() -> None:
    import importlib.util

    path = ROOT / "scripts" / "check_no_shell_true.py"
    spec = importlib.util.spec_from_file_location("check_no_shell_true", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 0
