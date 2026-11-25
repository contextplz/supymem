"""Cleanup process admission/credential boundary; no AWS lookup or source execution."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture()
def runtime():
    with service_modules("company_os"):
        from app.imports import storage, cleanup_process, intake
        from app.builder.model_proxy import BuildModelError
        yield SimpleNamespace(storage=storage, process=cleanup_process, intake=intake, error=BuildModelError)


def _config(d, monkeypatch):
    values = ("test-imports", "source-intake", "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
    for key, value in zip(d.storage.ENV_FIELDS, values, strict=True):
        monkeypatch.setenv(key, value)
    return d.storage.configuration()


def test_no_build_bucket_fallback_and_no_flag_bypass_of_parser_acceptance(runtime, monkeypatch):
    d = runtime
    for name in d.storage.ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BUILD_ARTIFACT_S3_BUCKET", "must-not-fall-back")
    with pytest.raises(d.error, match="configuration_unavailable"):
        d.storage.configuration()
    config = _config(d, monkeypatch)
    with pytest.raises(d.error, match="not_accepted"):
        d.storage.cleanup_ready()
    monkeypatch.setenv("IMPORT_CLEANUP_ENABLED", "1")
    monkeypatch.setenv("IMPORT_CLEANUP_ACCEPTANCE", d.storage.fingerprint(config))
    monkeypatch.setenv("IMPORT_INTAKE_ENABLED", "1")
    monkeypatch.setenv("IMPORT_INTAKE_STORAGE_ACCEPTANCE", d.storage.fingerprint(config))
    assert d.storage.cleanup_ready() == config
    status = d.intake.settings()
    assert status["ready"] is False and "import_parser_isolation_unavailable" in status["reasons"]
    monkeypatch.setenv("IMPORT_STORAGE_S3_PREFIX", "changed")
    with pytest.raises(d.error, match="not_accepted"):
        d.storage.cleanup_ready()


def test_fixed_supervised_entry_never_inherits_master_credentials(runtime, monkeypatch):
    d = runtime
    _config(d, monkeypatch)
    monkeypatch.setattr(d.process.sys, "platform", "linux")
    for name in ("AZURE_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "HTTP_PROXY", "PYTHONPATH", "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        monkeypatch.setenv(name, "forbidden")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/reviewed-task-id")
    calls = []
    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[1] == "-I" and argv[2].endswith("/imports/cleanup_entry.py")
        assert kwargs["timeout"] == 120 and not kwargs.get("shell")
        assert kwargs["env"]["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"].endswith("reviewed-task-id")
        assert "forbidden" not in kwargs["env"].values()
        assert json.loads(kwargs["input"])["operation"] == "fence"
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true,"result":{"fixture":true}}')
    monkeypatch.setattr(d.process.subprocess, "run", child)
    assert d.process.run("fence", {"scoped_fixture": True}) == {"fixture": True}
    assert len(calls) == 1
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "http://untrusted/credentials")
    with pytest.raises(d.error, match="credential_endpoint_invalid"):
        d.process.run("fence", {})
    assert len(calls) == 1


def test_child_timeout_does_not_claim_definitive_failure_or_lose_slot(runtime, monkeypatch):
    d = runtime
    monkeypatch.setattr(d.process.sys, "platform", "linux")
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixed", 120)
    monkeypatch.setattr(d.process.subprocess, "run", timeout)
    for _ in range(2):
        with pytest.raises(d.error, match="outcome_unconfirmed"):
            d.process.run("erase", {})
    assert d.process._SLOT.acquire(blocking=False)
    try:
        with pytest.raises(d.error, match="capacity"):
            d.process.run("fence", {})
    finally:
        d.process._SLOT.release()
