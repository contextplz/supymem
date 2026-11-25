"""Assembled Gateway -> real Core receipt parser/renderer, with local persistence seam."""
import importlib

import httpx
import pytest
from fastapi import FastAPI

from tests.company_os_harness import load_company_os_app
from tests.integration.test_gateway_b1_security import _close_gateway, _gateway


@pytest.fixture
def receipt_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/core.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.api import firstlook_api
    from app.surfaces.ratelimit import Limiter
    records = []
    def record(workspace_id, **fields):
        records.append((workspace_id, fields))
        return {"id": "interest_fixture", "has_email": bool(fields["email"])}
    monkeypatch.setattr(firstlook_api.fl_service, "record_interest", record)
    monkeypatch.setattr(firstlook_api, "_INTEREST", Limiter(capacity=1, per_seconds=60))
    core = FastAPI()
    core.include_router(firstlook_api.router)
    @core.middleware("http")
    async def disallowed_headers(request, call_next):
        response = await call_next(request)
        response.headers["Set-Cookie"] = "upstream_secret=must-not-forward"
        response.headers["X-Internal-Secret"] = "must-not-forward"
        return response
    main, client, saved = _gateway(tmp_path, monkeypatch)
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    upstream = []
    transport = httpx.ASGITransport(app=core)
    class CheckedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            upstream.append(request)
            assert "cookie" not in request.headers and "authorization" not in request.headers
            assert request.headers.get("x-subject") == ""
            assert "x-unsafe-extra" not in request.headers
            return await transport.handle_async_request(request)
        async def aclose(self):
            await transport.aclose()
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=CheckedTransport()))
    try:
        yield client, records, upstream
    finally:
        client.close()
        _close_gateway(saved)


def assert_receipt_headers(response):
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    assert {v.strip().lower() for v in response.headers["vary"].split(",")} >= {"accept", "origin"}
    assert response.headers["x-ratelimit-limit"] == "1"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert "set-cookie" not in response.headers and "x-internal-secret" not in response.headers


@pytest.mark.parametrize("accept,html", [
    ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
    ("application/json", False),
    ("text/html;q=0.2,application/json;q=0.9", False),
    ("text/html;q=0.9,application/json;q=0.2", True),
    ("*/*", False),
    (None, False),
])
def test_actual_gateway_preserves_receipt_negotiation_and_rate_limit(receipt_gateway, accept, html):
    client, records, upstream = receipt_gateway
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": "browser_secret=private",
               "X-Subject": "attacker", "X-Unsafe-Extra": "must-not-forward"}
    if accept is not None:
        headers["Accept"] = accept
    response = client.post("/w/wsp_receipt/interest", content="email=synthetic%40example.invalid&note=private-marker", headers=headers)
    assert response.status_code == 201, response.text
    assert response.headers["content-type"].startswith("text/html" if html else "application/json")
    if html:
        assert "Thank you" in response.text and "private-marker" not in response.text
        assert "example.invalid" not in response.text
    else:
        assert response.json() == {"id": "interest_fixture", "has_email": True, "received": True}
    assert_receipt_headers(response)
    assert len(records) == 1 and records[0][1]["email"] == "synthetic@example.invalid"
    assert upstream[0].headers.get("accept") == (accept or "*/*")
    limited = client.post("/w/wsp_receipt/interest", content="email=duplicate%40example.invalid", headers=headers)
    assert limited.status_code == 429 and len(records) == 1
    assert limited.headers["content-type"].startswith("application/json")
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0
    assert_receipt_headers(limited)


def test_actual_gateway_preserves_html_validation_error_without_persistence(receipt_gateway):
    client, records, _ = receipt_gateway
    response = client.post("/w/wsp_receipt/interest", content='{"email":',
                           headers={"Content-Type": "application/json", "Accept": "text/html"})
    assert response.status_code == 422
    assert "Check your submission" in response.text
    assert not records
    assert_receipt_headers(response)
