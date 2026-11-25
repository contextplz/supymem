"""Offline synthetic checks: public errors, diagnostics and service credentials."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.company_os_harness import load_company_os_app

ROOT = Path(__file__).resolve().parents[2]
SENSITIVE = "synthetic-private-provider-details"


@pytest.fixture()
def company_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-platform-key")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/security.db")
    load_company_os_app()


def _load_module(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["models", "chat/completions"])
async def test_gateway_transport_errors_are_fixed_and_stream_client_closes(monkeypatch, tmp_path, path):
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(ROOT / "services/gateway"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    config = ModuleType("app.config")
    config.settings = SimpleNamespace(company_os_url="http://synthetic-private-core")
    rate = ModuleType("app.ratelimit")
    rate.limiter = Mock()
    monkeypatch.setitem(sys.modules, "app.config", config)
    monkeypatch.setitem(sys.modules, "app.ratelimit", rate)
    proxy = _load_module(monkeypatch, "security_openai_proxy", "services/gateway/app/openai_proxy.py")
    vault = sys.modules["app.vault"].vault
    assert vault.path == tmp_path / "vault.enc"
    with pytest.raises(ValueError, match="requires a session secret"):
        vault._derive_key()
    client = Mock()
    client.request = AsyncMock(side_effect=httpx.ConnectError(SENSITIVE))
    client.send = AsyncMock(side_effect=httpx.ConnectError(SENSITIVE))
    client.aclose = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", Mock(return_value=client))

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [
        (b"authorization", b"Bearer synthetic-app-key"), (b"content-type", b"application/json"),
    ]}, receive)
    response = await proxy.forward_openai(path, request)
    assert response.status_code == 503
    assert SENSITIVE not in response.body.decode()
    assert json.loads(response.body)["code"] == "core_restarting"
    assert response.headers["x-request-id"].startswith("req_")
    if path == "chat/completions":
        client.aclose.assert_awaited_once()


@pytest.mark.parametrize("stream", [False, True])
def test_adapter_errors_never_echo_provider_details(monkeypatch, stream):
    monkeypatch.syspath_prepend(str(ROOT / "services/llm_adapter"))
    monkeypatch.setenv("LLM_API_KEY", "synthetic-platform-key")
    monkeypatch.delenv("LLM_ADAPTER_INBOUND_KEY", raising=False)
    monkeypatch.delenv("HOSTED_LLM_GATEWAY_ENABLED", raising=False)
    adapter = _load_module(monkeypatch, "security_llm_adapter", "services/llm_adapter/llm_adapter/main.py")
    monkeypatch.setattr(adapter, "chat_completion", AsyncMock(side_effect=RuntimeError(SENSITIVE)))
    response = TestClient(adapter.app).post("/v1/messages", json={"messages": [], "stream": stream})
    assert response.status_code == (200 if stream else 502)
    assert SENSITIVE not in response.text
    assert "The AI service could not be reached." in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_builder_ingest_failure_is_fixed(company_env, monkeypatch, chunked):
    from app.api import builder
    from app.firstlook import service

    monkeypatch.setattr(builder, "_venture", lambda _: None)
    monkeypatch.setattr(builder, "active_run", lambda _: SimpleNamespace(id="synthetic-run", status="running"))
    monkeypatch.setattr(builder, "get_run_gate", lambda *args: "done")
    monkeypatch.setattr(builder, "set_run_gate", lambda *args: None)
    monkeypatch.setattr(builder, "_choice_payload", lambda _: {})
    monkeypatch.setattr(service, "ingest_builder_page", Mock(side_effect=RuntimeError(SENSITIVE)))
    html = "<html><body>" + ("synthetic content " * 5) + "</body></html>"
    if chunked:
        result = await builder.receive_builder_page_chunk("synthetic-venture", builder.BuilderPageChunk(
            name="index.html", index=0, total=1, b64=base64.b64encode(html.encode()).decode(),
        ))
    else:
        result = await builder.finish_builder_session("synthetic-venture", builder.BuilderDoneBody(html=html))
    assert result["site"]["error"] == "page_ingest_failed"
    assert SENSITIVE not in repr(result)


@pytest.mark.parametrize("exc,status,code", [
    (RuntimeError(SENSITIVE), 503, "opengtm_unavailable"),
    (ValueError(SENSITIVE), 400, "opengtm_invalid_input"),
    (subprocess.TimeoutExpired(SENSITIVE, 1), 504, "opengtm_timeout"),
])
def test_opengtm_maps_raw_exceptions_to_fixed_codes(company_env, exc, status, code):
    from app.api.ops_api import _opengtm_exc

    error = _opengtm_exc(exc)
    assert error.status_code == status
    assert error.detail["code"] == code
    assert SENSITIVE not in repr(error.detail)


@pytest.mark.parametrize("connect", [False, True])
def test_gmail_route_errors_are_fixed(company_env, monkeypatch, connect):
    from app.api import ops_api
    from app.gtm.supymem import pipeline
    from app.marketing import lab

    monkeypatch.setattr(ops_api, "_auth_workspace", lambda *args: "synthetic-workspace")
    monkeypatch.setattr(pipeline, "sync_gmail_accounts", Mock(side_effect=RuntimeError(SENSITIVE)))
    monkeypatch.setattr(lab, "start_mail_connect", Mock(side_effect=RuntimeError(SENSITIVE)))
    with pytest.raises(HTTPException) as caught:
        if connect:
            ops_api.marketing_lab_mail_connect(body={})
        else:
            ops_api.ai_gtm_gmail_accounts()
    assert SENSITIVE not in repr(caught.value.detail)
    assert caught.value.detail["code"] == ("gmail_connect_unavailable" if connect else "gmail_accounts_unavailable")


@pytest.mark.parametrize("secret", [None, "", "too-short"])
def test_internal_ops_refuses_missing_or_short_secret(company_env, monkeypatch, secret):
    from app.api.internal_ops import _require_internal_secret

    monkeypatch.delenv("QM_INTERNAL_CALLBACK_SECRET", raising=False)
    if secret is not None:
        monkeypatch.setenv("QM_INTERNAL_CALLBACK_SECRET", secret)
    with pytest.raises(HTTPException) as caught:
        _require_internal_secret(secret)
    assert caught.value.status_code == 503


def test_internal_ops_uses_constant_time_bytes_comparison(company_env, monkeypatch):
    from app.api import internal_ops

    secret = "synthetic-internal-secret-" + "x" * 32
    monkeypatch.setenv("QM_INTERNAL_CALLBACK_SECRET", secret)
    comparison = Mock(wraps=internal_ops.hmac.compare_digest)
    monkeypatch.setattr(internal_ops.hmac, "compare_digest", comparison)
    internal_ops._require_internal_secret(secret)
    comparison.assert_called_with(secret.encode(), secret.encode())
    with pytest.raises(HTTPException) as caught:
        internal_ops._require_internal_secret("wrong-♥")
    assert caught.value.status_code == 401


@pytest.mark.parametrize("secret", [None, "short", "outreach-unsub-local-secret-32bytes!!"])
def test_unsubscribe_has_no_hosted_default_secret(company_env, monkeypatch, secret):
    from app.gtm.relationships import unsubscribe
    from app.gtm.relationships.service import OutreachError

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("OUTREACH_UNSUB_SECRET", raising=False)
    monkeypatch.delenv("ACCOUNT_SESSION_SECRET", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    if secret is not None:
        monkeypatch.setenv("OUTREACH_UNSUB_SECRET", secret)
    with pytest.raises(OutreachError) as caught:
        unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")
    assert caught.value.code == "unsubscribe_unavailable"
    assert caught.value.status == 503


def test_unsubscribe_local_and_configured_hosted_roundtrips(company_env, monkeypatch):
    from app.gtm.relationships import unsubscribe

    monkeypatch.delenv("OUTREACH_UNSUB_SECRET", raising=False)
    monkeypatch.delenv("ACCOUNT_SESSION_SECRET", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    local_token = unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")
    assert unsubscribe._decode(local_token) == ("synthetic-workspace", "synthetic-contact", "")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OUTREACH_UNSUB_SECRET", "synthetic-unsubscribe-secret-" + "x" * 32)
    hosted_token = unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")
    assert unsubscribe._decode(hosted_token) == ("synthetic-workspace", "synthetic-contact", "")
    assert unsubscribe._decode(local_token) is None


@pytest.mark.parametrize("session", [None, "short", "dev-session-secret-change-me-32b!!"])
def test_unsubscribe_refuses_missing_weak_or_default_session_fallback(company_env, monkeypatch, session):
    from app.gtm.relationships import unsubscribe
    from app.gtm.relationships.service import OutreachError

    monkeypatch.setenv("APP_ENV", "production")
    for name in ("OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    if session is not None:
        monkeypatch.setenv("SESSION_SECRET", session)
    with pytest.raises(OutreachError, match="unsubscribe_unavailable"):
        unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")


def test_unsubscribe_derives_a_purpose_key_from_strong_session_fallback(company_env, monkeypatch):
    import hashlib
    import hmac
    from app.gtm.relationships import unsubscribe

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("OUTREACH_UNSUB_SECRET", raising=False)
    monkeypatch.delenv("ACCOUNT_SESSION_SECRET", raising=False)
    session = "synthetic-session-secret-" + "x" * 32
    monkeypatch.setenv("SESSION_SECRET", session)
    derived = unsubscribe._secret()
    assert derived == hmac.new(session.encode(), b"supymem:outreach-unsubscribe:v1", hashlib.sha256).digest()
    assert derived != session.encode()
    token = unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")
    assert unsubscribe._decode(token) == ("synthetic-workspace", "synthetic-contact", "")
    monkeypatch.setenv("OUTREACH_UNSUB_SECRET", "synthetic-dedicated-secret-" + "y" * 32)
    assert unsubscribe._secret() != derived


@pytest.mark.parametrize("source", ["OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"])
@pytest.mark.parametrize("public_default", [
    "outreach-unsub-local-secret-32bytes!!",
    "local-account-session-secret-32b!!",
    "dev-session-secret-change-me-32b!!",
])
@pytest.mark.parametrize("padding", ["", " \t"])
def test_every_hosted_unsubscribe_source_rejects_all_public_defaults(company_env, monkeypatch, source, public_default, padding):
    from app.gtm.relationships import unsubscribe
    from app.gtm.relationships.service import OutreachError

    monkeypatch.setenv("APP_ENV", "production")
    for name in ("OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(source, padding + public_default + padding)
    with pytest.raises(OutreachError, match="unsubscribe_unavailable"):
        unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")


@pytest.mark.parametrize("source", ["OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"])
@pytest.mark.parametrize("padding", ["", " \t"])
def test_strong_unsubscribe_sources_are_normalized_and_keep_roundtrips(company_env, monkeypatch, source, padding):
    from app.gtm.relationships import unsubscribe

    monkeypatch.setenv("APP_ENV", "production")
    for name in ("OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    value = "synthetic-strong-unsubscribe-key-" + "x" * 32
    monkeypatch.setenv(source, padding + value + padding)
    key = unsubscribe._secret()
    if source == "SESSION_SECRET":
        assert key != value.encode()
    else:
        assert key == value.encode()
    token = unsubscribe.issue_unsubscribe_token("synthetic-workspace", "synthetic-contact")
    assert unsubscribe._decode(token) == ("synthetic-workspace", "synthetic-contact", "")


@pytest.mark.parametrize("code,status", [("unsubscribe_unavailable", 503), ("not_found", 404)])
def test_public_unsubscribe_preserves_not_found_and_explains_unavailable(company_env, monkeypatch, code, status):
    from app.api import ops_api
    from app.gtm.relationships import unsubscribe
    from app.gtm.relationships.service import OutreachError

    monkeypatch.setattr(unsubscribe, "honor_unsubscribe_token", Mock(side_effect=OutreachError(code, status)))
    with pytest.raises(HTTPException) as caught:
        ops_api.public_unsubscribe(ops_api.UnsubscribeBody(token="synthetic-token"))
    assert caught.value.status_code == status
    assert caught.value.detail == (
        "Unsubscribe is temporarily unavailable. Please try again." if status == 503 else "You can reply to your email with no thanks to stop these emails."
    )


@pytest.fixture()
def hosted_crash_api(company_env):
    if not (ROOT / "services/company_os/app/api/consumer_api.py").is_file():
        pytest.skip("The hosted crash-report endpoint is not in the public local release.")
    from app.api import consumer_api

    return consumer_api


@pytest.fixture()
def crash_client(hosted_crash_api):
    consumer_api = hosted_crash_api
    consumer_api._CRASH_LIMIT.reset()
    app = FastAPI()
    app.include_router(consumer_api.reports_router)
    return TestClient(app)


@pytest.mark.parametrize("payload,status", [
    ({"stack": "x" * (128 * 1024)}, 413),
    ({"logs": "x" * 32_001}, 422),
    ({"title": "x" * 201}, 422),
    ({"install_id": "x" * 81}, 422),
])
def test_crash_rejects_oversize_before_storage_without_echo(crash_client, monkeypatch, payload, status):
    from app.api import consumer_api

    storage = Mock(return_value={"ok": True})
    monkeypatch.setattr(consumer_api, "ingest", storage)
    response = crash_client.post("/api/v1/reports/crash", json={
        "app_version": "synthetic", "os": "synthetic", "body": SENSITIVE, **payload,
    })
    assert response.status_code == status
    assert SENSITIVE not in response.text
    storage.assert_not_called()


def test_crash_rate_limit_cannot_be_bypassed_by_install_ids(crash_client, monkeypatch):
    from app.api import consumer_api

    storage = Mock(return_value={"ok": True})
    monkeypatch.setattr(consumer_api, "ingest", storage)
    for index in range(10):
        response = crash_client.post("/api/v1/reports/crash", json={
            "app_version": "synthetic", "os": "synthetic", "install_id": f"synthetic-{index}",
        })
        assert response.status_code == 200
    response = crash_client.post("/api/v1/reports/crash", json={"app_version": "synthetic", "os": "synthetic"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert storage.call_count == 10


@pytest.mark.parametrize("kind", ["crash", "user"])
def test_shared_report_store_redacts_before_return_and_persistence(company_env, kind):
    from app.consumer import reports

    # Build fake credential shapes at runtime so no secret literal is committed.
    credential = "sk-" + "a" * 32
    row = reports.ingest(
        kind=kind, app_version="synthetic", os_name="synthetic", stack=credential,
        logs=credential, body=credential, title=credential,
    )
    stored = reports.get_report(row["id"])
    assert credential not in repr(row)
    assert credential not in repr(stored)
    for field in ("stack", "logs", "title", "body"):
        assert stored[field] == "[REDACTED]"


@pytest.mark.asyncio
async def test_crash_chunked_body_cannot_bypass_size_limit(hosted_crash_api):
    hosted_crash_api._CRASH_LIMIT.reset()
    chunks = iter([b"x" * 70_000, b"x" * 70_000])

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [
        (b"content-type", b"application/json"),
    ]}, receive)
    with pytest.raises(HTTPException) as caught:
        await hosted_crash_api._read_crash_body(request)
    assert caught.value.status_code == 413
