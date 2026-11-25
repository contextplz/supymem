"""Credential shapes are redacted in strings and in every log record, JSON logs or not."""

from __future__ import annotations

import io
import logging

import pytest

from core.redact import redact_string

# Dummy values shaped like real credentials; none are live.
SAMPLES = [
    "sk-proj-" + "a1B2c3D4e5_F6g7H8-i9J0k1L2",
    "sk-ant-api03-" + "Z" * 40,
    "AIza" + "S" * 35,
    "fc-" + "0123456789abcdef" * 2,
    "github_pat_" + "11ABCDE" + "x" * 30,
    "gho_" + "q" * 36,
]


@pytest.mark.parametrize("secret", SAMPLES)
def test_redact_string_covers_provider_keys(secret: str) -> None:
    assert secret not in redact_string(f"calling with key={secret} now")


def test_redact_string_strips_url_password() -> None:
    out = redact_string("db=postgres://admin:hunter2pass@db.internal:5432/app")
    assert "hunter2pass" not in out
    assert "db.internal:5432/app" in out


def test_ordinary_text_untouched() -> None:
    text = "commit 3f2a9c1 deployed to https://example.com/path?x=1"
    assert redact_string(text) == text


def test_plain_logging_redacts_msg_and_args() -> None:
    from core.logging import install_log_redaction

    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log = logging.getLogger("redaction.unit")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("key %s url %s", SAMPLES[0], "postgres://u:s3cretpw@h/db")
        log.warning("inline %s", RuntimeError(f"bad token {SAMPLES[2]}"))
        log.info(f"f-string {SAMPLES[4]}")
    finally:
        log.removeHandler(handler)
    text = stream.getvalue()
    for secret in (SAMPLES[0], "s3cretpw", SAMPLES[2], SAMPLES[4]):
        assert secret not in text
    assert "[REDACTED]" in text


def test_core_app_installs_redaction_without_json_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COS_JSON_LOGS", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/r.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    logging.setLogRecordFactory(logging.LogRecord)
    from tests.company_os_harness import load_company_os_app

    load_company_os_app().create_app()
    from core.logging import _redacting_factory

    assert logging.getLogRecordFactory() is _redacting_factory
