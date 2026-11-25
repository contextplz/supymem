"""Auth and OAuth credentials must not appear in ordinary HTTP log records."""

import logging

import httpx

from core.logging import RequestQueryFilter, install_sensitive_url_filters


def test_local_access_logs_redact_credentials_not_token_limits_or_keychain_flags(monkeypatch):
    from core.logging import RedactFilter

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_TOKEN_LIMIT", "60")
    monkeypatch.setenv("SUPYMEM_KEYCHAIN_ENABLED", "1")
    for domain in ("supplier-audit", "cooking-classes"):
        credential = f"{domain}-private-credential"
        monkeypatch.setenv("SUPYMEM_API_KEY", credential)
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/%s" %d %s',
            ("127.0.0.1:60196", "POST", f"/ventures/{domain}", "1.1", 200, credential), None)
        RedactFilter().filter(record)
        assert record.getMessage() == f'127.0.0.1:60196 - "POST /ventures/{domain} HTTP/1.1" 200 [REDACTED]'


def test_access_log_preserves_route_status_without_query():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:123", "GET", "/auth/consume?token=private&purpose=email", "1.1", 200), None)
    RequestQueryFilter().filter(record)
    assert record.getMessage() == '127.0.0.1:123 - "GET /auth/consume HTTP/1.1" 200'


def test_httpx_log_removes_secret_query_from_url_objects():
    record = logging.LogRecord("httpx", logging.INFO, "", 0,
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", httpx.URL("https://core/api/v1/content/oauth/x/callback?code=private&state=nonce"),
         "HTTP/1.1", 303, "See Other"), None)
    RequestQueryFilter().filter(record)
    assert "private" not in record.getMessage()
    assert "nonce" not in record.getMessage()
    assert "/api/v1/content/oauth/x/callback" in record.getMessage()
    assert "303" in record.getMessage()


def test_filter_is_installed_once_even_without_json_logging():
    install_sensitive_url_filters()
    install_sensitive_url_filters()
    for name in ("httpx", "uvicorn.access"):
        assert sum(isinstance(f, RequestQueryFilter) for f in logging.getLogger(name).filters) == 1
