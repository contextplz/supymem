"""Preview backend selection and Coolify/local contracts (doc 104 B4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from preview.base import PreviewNotConfigured, PreviewResult, get_preview
from preview.coolify import CoolifyPreview
from preview.detect import suggested_dockerfile
from preview.local import LocalDockerPreview


def test_local_env_selects_docker(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    backend = get_preview()
    assert backend.name == "local_docker"


def test_production_without_coolify_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("COOLIFY_API_URL", raising=False)
    with pytest.raises(PreviewNotConfigured):
        get_preview()


def test_suggested_dockerfile_static_html(tmp_path: Path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    text = suggested_dockerfile(tmp_path)
    assert text is not None
    assert "nginx" in text


def test_local_preview_uses_injected_runner(tmp_path: Path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM nginx:alpine\nEXPOSE 80\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = "abc123\n" if argv[1] == "run" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    preview = LocalDockerPreview(runner=runner)
    result = preview.publish(str(tmp_path), slug="demo")
    assert result.backend == "local_docker"
    assert result.url.startswith("http://127.0.0.1:")
    assert any(cmd[1] == "build" for cmd in calls)
    assert any(cmd[1] == "run" for cmd in calls)
    preview.teardown(result)
    assert any(cmd[1] == "rm" for cmd in calls)


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
@pytest.mark.parametrize("source_name", ["index.html", "package.json"])
def test_local_preview_requires_tool_authored_dockerfile(tmp_path: Path, title, source_name):
    source = "<title>" + title + "</title>" if source_name == "index.html" else '{"type":"module"}'
    (tmp_path / source_name).write_text(source, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    calls = []
    preview = LocalDockerPreview(runner=lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))

    with pytest.raises(RuntimeError, match="Settings > Keys and connections > Coding tools"):
        preview.publish(str(tmp_path), slug="requested-preview", port=4173)

    assert calls == []
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_local_preview_keeps_tool_authored_source_bytes(tmp_path: Path, title):
    (tmp_path / "index.html").write_text("<title>" + title + "</title>", encoding="utf-8")
    (tmp_path / "Dockerfile").write_bytes(b"FROM nginx:alpine\r\nEXPOSE 8080/tcp\r\n")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    calls = []

    def runner(argv):
        calls.append(argv)
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
        return subprocess.CompletedProcess(argv, 0, stdout="verified-container\n", stderr="")

    result = LocalDockerPreview(runner=runner).publish(str(tmp_path), slug="requested-preview", port=4173)
    assert result.container_id == "verified-container"
    assert "127.0.0.1:4173:8080" in calls[1]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_local_preview_rejects_dockerfile_outside_tool_source(tmp_path: Path, title):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM nginx:alpine\nEXPOSE 80\n", encoding="utf-8")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "index.html").write_text("<title>" + title + "</title>", encoding="utf-8")
    (workspace / "Dockerfile").symlink_to(outside / "Dockerfile")
    calls = []
    preview = LocalDockerPreview(runner=lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))

    with pytest.raises(RuntimeError, match="Settings > Keys and connections > Coding tools"):
        preview.publish(str(workspace), slug="requested-preview", port=4173)

    assert calls == []
    assert (workspace / "Dockerfile").is_symlink()


def test_coolify_create_dockerfile_payload(tmp_path: Path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/applications/dockerfile"
        assert request.headers["authorization"] == "Bearer tok"
        body = request.read()
        assert b"FROM nginx" in body
        return httpx.Response(
            201,
            json={"uuid": "app-1", "fqdn": "https://demo.apps.example.com"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    coolify = CoolifyPreview(
        api_url="https://coolify.example",
        token="tok",
        project_uuid="p1",
        server_uuid="s1",
        domain="apps.example.com",
        client=client,
    )
    result = coolify.publish(str(tmp_path), slug="demo")
    assert result.application_uuid == "app-1"
    assert result.url == "https://demo.apps.example.com"
