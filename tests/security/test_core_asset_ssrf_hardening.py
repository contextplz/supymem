"""Offline checks of upload, download, pinned fetch and webhook boundaries."""

import asyncio
import io
import socket
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from PIL import Image

from core.research_web import guard
from tests.company_os_harness import load_company_os_app


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/security.db")
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    load_company_os_app()
    from app.api import ops_api
    from app.chat import hosting
    from app.marketing.media import service, store
    from app.surfaces import webhooks
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    monkeypatch.setattr(store, "MEDIA_ROOT", tmp_path / "media")
    workspace = ensure_desk(LOCAL_PERSON_ID).id
    monkeypatch.setattr(ops_api, "_auth_workspace", lambda *args: workspace)
    return ops_api, service, store, hosting, webhooks, workspace


def raster(format):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), (40, 50, 60)).save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize("filename,body,mime", [
    ("image.png", raster("PNG"), "image/png"),
    ("image.jpg", raster("JPEG"), "image/jpeg"),
    ("image.webp", raster("WEBP"), "image/webp"),
    ("image.gif", raster("GIF"), "image/gif"),
    ("document.pdf", b"%PDF-1.7\nfixture", "application/pdf"),
    ("clip.mp4", b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42", "video/mp4"),
    ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/svg+xml"),
])
def test_allowed_uploads_and_download_headers(modules, filename, body, mime):
    ops_api, service, _, _, _, workspace = modules
    app = FastAPI()
    app.include_router(ops_api.router)
    with TestClient(app) as client:
        response = client.post("/api/v1/marketing/assets", files={"file": (filename, body, "text/html")},
                               data={"kind": "logo" if filename.endswith(".svg") else "other"})
        assert response.status_code == 200
        asset = response.json()["asset"]
        assert asset["mime"] == mime
        response = client.get(f'/api/v1/marketing/assets/{asset["id"]}/file')
        assert response.content == body
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
        if mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            assert "content-disposition" not in response.headers
        else:
            assert response.headers["content-disposition"].startswith("attachment;")
    assert service.ingest_upload(workspace_id=workspace, kind="other", filename="empty.png", data=b"")["error"] == "empty_file"


@pytest.mark.parametrize("filename,body", [
    ("image.png", b"<html><script>alert(1)</script></html>"),
    ("image.jpg", raster("PNG")), ("image.png", b"\x89PNG\r\n\x1a\ntruncated"),
    ("document.pdf", b"<html>not a PDF</html>"), ("clip.mp4", b"not video"),
    ("clip.mp4", b"\x00\x00\x00\x18ftypavif\x00\x00\x02\x00avifmif1"),
    ("logo.svg", b"<html><script>alert(1)</script></html>"), ("logo.svg", b"<svg>"),
    ("page.html", b"<html>active</html>"),
])
def test_bad_upload_never_writes_to_disk(modules, filename, body):
    _, service, store, _, _, workspace = modules
    result = service.ingest_upload(workspace_id=workspace, kind="other", filename=filename, data=body)
    assert result["ok"] is False
    assert not list(store.MEDIA_ROOT.rglob("upl*"))
    assert store.list_assets(workspace) == []


def test_upload_read_is_bounded_and_service_also_caps_callers(modules, monkeypatch):
    ops_api, service, _, _, _, workspace = modules
    limit = service.MAX_UPLOAD_BYTES
    upload = UploadFile(filename="image.png", file=io.BytesIO(b"x" * (limit + 100)))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(ops_api.marketing_asset_upload(file=upload, kind="other"))
    assert caught.value.status_code == 413
    assert upload.file.tell() == limit + 1
    assert service.ingest_upload(workspace_id=workspace, kind="other", filename="image.png",
                                 data=b"x" * (limit + 1))["error"] == "file_too_large"
    ingest = Mock(return_value={"ok": True})
    monkeypatch.setattr(service, "ingest_upload", ingest)
    exact = UploadFile(filename="image.png", file=io.BytesIO(b"x" * limit))
    assert asyncio.run(ops_api.marketing_asset_upload(file=exact, kind="other")) == {"ok": True}
    assert len(ingest.call_args.kwargs["data"]) == limit


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.170.2", "10.0.0.1", "::ffff:127.0.0.1"])
def test_pinned_guard_blocks_private_literal_addresses(address):
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(PermissionError):
        guard.pin_request(httpx.Request("POST", f"https://{host}/hook"))


def test_pinned_guard_rejects_empty_dns(monkeypatch):
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [])
    with pytest.raises(PermissionError):
        guard.pin_request(httpx.Request("POST", "https://empty.example/hook"))


@pytest.mark.parametrize("ip", ["8.8.8.8", "2606:4700:4700::1111"])
def test_hosting_pins_redirects_and_preserves_logical_url(modules, monkeypatch, ip):
    _, _, _, hosting, _, _ = modules
    lookup = Mock(return_value=[(socket.AF_INET6 if ":" in ip else socket.AF_INET,
        socket.SOCK_STREAM, 6, "", (ip, 443))])
    monkeypatch.setattr(guard.socket, "getaddrinfo", lookup)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/live"}) if len(requests) == 1 else httpx.Response(200, text="<title>Live</title>")

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    assert hosting._get("https://site.example/start") == (200, "https://site.example/live", "<title>Live</title>")
    assert lookup.call_count == 2
    assert [request.url.host for request in requests] == [ip, ip]
    assert [request.headers["host"] for request in requests] == ["site.example"] * 2
    assert [request.extensions["sni_hostname"] for request in requests] == ["site.example"] * 2


@pytest.mark.parametrize("location", ["https://169.254.170.2/task", "http://8.8.8.8/", "/rebound"])
def test_hosting_rejects_unsafe_redirect_before_connect(modules, monkeypatch, location):
    _, _, _, hosting, _, _ = modules
    lookup = Mock(side_effect=[[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
                               [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]])
    monkeypatch.setattr(guard.socket, "getaddrinfo", lookup)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    assert hosting.check("https://site.example/")["ok"] is False
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["https://127.0.0.1/hook", "https://169.254.170.2/hook",
    "http://127.0.0.1/hook", "http://127.0.0.1.evil.example/hook"])
def test_hosted_webhook_subscription_refuses_private_targets(modules, monkeypatch, url):
    _, _, _, _, webhooks, workspace = modules
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(webhooks.WebhookError):
        webhooks.subscribe(workspace_id=workspace, url=url, events=["engagement.created"])
    assert webhooks.list_subscriptions(workspace) == []


def test_webhook_delivery_pins_and_rechecks_saved_target(modules, monkeypatch):
    _, _, _, _, webhooks, workspace = modules
    monkeypatch.setenv("APP_ENV", "production")
    lookup = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(guard.socket, "getaddrinfo", lookup)
    sub = webhooks.subscribe(workspace_id=workspace, url="https://hooks.example/event", events=["engagement.created"])
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "hooks.example"
        assert request.extensions["sni_hostname"] == "hooks.example"
        assert webhooks.verify(sub.secret, request.content, request.headers["X-CompanyOS-Signature"])
        return httpx.Response(307, headers={"location": "https://169.254.170.2/task"})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    ids = webhooks.emit("engagement.created", workspace_id=workspace, payload={"summary": "created"})
    attempts = webhooks.deliver_pending()
    assert attempts[0].status == "retrying" and attempts[0].error == "HTTP 307"
    assert len(requests) == 1
    lookup.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.170.2", 443))]
    from core.clock import utcnow

    refused = webhooks._attempt(ids[0], sub.id, "{}", utcnow())
    assert refused.status == "retrying" and refused.error == "PermissionError"
    assert len(requests) == 1


def test_legacy_local_webhook_can_still_deliver(modules, monkeypatch):
    _, _, _, _, webhooks, workspace = modules
    webhooks.subscribe(workspace_id=workspace, url="http://127.0.0.1:9000/hook", events=["engagement.created"])
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs:
        original(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(204))))
    webhooks.emit("engagement.created", workspace_id=workspace, payload={})
    assert webhooks.deliver_pending()[0].status == "delivered"


def test_dead_unsafe_helpers_are_removed():
    import ast

    root = Path(__file__).resolve().parents[2]
    for path, removed in [
        ("services/company_os/app/imports/service.py", {"resolve_source", "start_import", "run_analyze"}),
        ("services/company_os/app/api/consumer_api.py", {"install_cors", "cors_origins"}),
    ]:
        tree = ast.parse((root / path).read_text())
        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not names & removed
