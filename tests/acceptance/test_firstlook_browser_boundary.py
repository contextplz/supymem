"""Local browser receipt proof; requires Playwright + installed Chromium.

FIRSTLOOK_CHROMIUM_EXECUTABLE may select an existing isolated test browser.
No external requests, provider calls, or real publication are made.

This freeze port covers native form navigation to a truthful HTML receipt.
It does not claim the later main public-page CSP/sandbox suite.
"""
from __future__ import annotations

import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from tests.security.test_connector_oauth_boundary import client
from tests.security.test_core_signed_boundary import make_workspace


def test_public_page_native_form_posts_html_receipt(client, tmp_path, monkeypatch):
    """ASGI stand-in for an isolated browser form POST. No Playwright required."""
    from app.firstlook import versions
    from app.firstlook.interest import list_leads

    wid = make_workspace(client, "usr_alice")
    monkeypatch.setenv("CORE_PORT", "3300")
    body = '''<!doctype html><html><body>
<form id="waitlist" onsubmit="event.preventDefault()"><input name="email" type="email" required>
<input name="note" value="keep this private"><input name="site_version" value="1" type="hidden">
<button type="submit">Join</button></form>
</body></html>'''
    path = tmp_path / "index.html"
    path.write_text(body)
    versions.persist(
        workspace_id=wid, stage="first_look", template="test", archetype="test",
        brandkit_hash="", copy_hash="", copy={"page_html": body, "page_files": {}},
        brandkit={}, variant_index=0, path=str(path), url=f"/w/{wid}/site/",
        sha256=hashlib.sha256(body.encode()).hexdigest(), published=True,
    )
    page = client.get(f"/w/{wid}/site/")
    assert page.status_code == 200
    assert "HTMLFormElement.prototype.submit" in page.text
    assert "XMLHttpRequest" not in page.text
    response = client.post(
        f"/w/{wid}/interest",
        data={"email": "browser-boundary@example.test", "note": "keep this private", "site_version": "1"},
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )
    assert response.status_code == 201
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-ratelimit-limit"] == "30"
    assert response.headers["cache-control"].startswith("no-store")
    assert "Thank you" in response.text
    assert "Your interest has been recorded" in response.text
    assert "browser-boundary@example.test" not in response.text
    assert "keep this private" not in response.text
    assert "<script" not in response.text
    assert [lead["email"] for lead in list_leads(wid)] == ["browser-boundary@example.test"]


def test_native_form_reaches_html_receipt_after_persistence(client, tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from app.firstlook import versions
    from playwright.sync_api import sync_playwright

    wid = make_workspace(client, "usr_alice")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(("GET", self.path))
            response = client.get(self.path, headers={name:value for name, value in self.headers.items() if name.lower() in {"accept", "content-type", "origin", "referer"}})
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() not in {"content-length", "transfer-encoding"}:
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.content)

        def do_POST(self):
            seen.append(("POST", self.path))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            response = client.post(self.path, content=body, headers={name:value for name, value in self.headers.items() if name.lower() in {"accept", "content-type", "origin", "referer"}})
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() not in {"content-length", "transfer-encoding"}:
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    monkeypatch.setenv("CORE_PORT", str(port))
    base = f"http://127.0.0.1:{port}"
    body = '''<!doctype html><html><body>
<form id="waitlist" onsubmit="event.preventDefault()"><input name="email" type="email" required>
<input name="site_version" value="1" type="hidden"><button type="submit">Join</button></form>
</body></html>'''
    path = tmp_path / "index.html"
    path.write_text(body)
    versions.persist(workspace_id=wid, stage="first_look", template="test", archetype="test",
        brandkit_hash="", copy_hash="", copy={"page_html":body,"page_files":{}},
        brandkit={}, variant_index=0, path=str(path), url=f"/w/{wid}/site/",
        sha256=hashlib.sha256(body.encode()).hexdigest(), published=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("FIRSTLOOK_CHROMIUM_EXECUTABLE") or None)
            context = browser.new_context()
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
            page = context.new_page()
            page.goto(base + f"/w/{wid}/site/")
            page.locator('input[name="email"]').fill("browser-boundary@example.test")
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/w/{wid}/interest")) as received:
                page.locator('button[type="submit"]').click()
            assert received.value.status == 201
            assert received.value.headers["content-type"].startswith("text/html")
            assert received.value.headers["x-ratelimit-limit"] == "30"
            assert received.value.headers["cache-control"].startswith("no-store")
            page.get_by_role("heading", name="Thank you").wait_for()
            assert "Your interest has been recorded" in page.locator("body").inner_text()
            assert "browser-boundary@example.test" not in page.content()
            assert page.locator("script").count() == 0
            from app.firstlook.interest import list_leads
            assert [lead["email"] for lead in list_leads(wid)] == ["browser-boundary@example.test"]
            assert ("POST", f"/w/{wid}/interest") in seen
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
