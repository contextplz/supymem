"""Cognito/session identity on the account API."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.credit_lease import generate_keypair
from tests.company_os_harness import COMPANY_OS, load_company_os_app


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_local_cognito_import_keeps_hosted_configuration_validation(tmp_path, monkeypatch, domain):
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DEPLOYMENT_CONFIG", str(tmp_path / domain / "missing.json"))
    for name in ("COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_DOMAIN",
                 "COGNITO_POOL_ID", "LEGACY_PRODUCTION_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    root = Path(__file__).resolve().parents[2]
    name = "local_cognito_import"
    spec = importlib.util.spec_from_file_location(name, root / "services/company_os/app/consumer/cognito.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    for getter in (module.pool_id, module.client_id, module.hosted_domain):
        with pytest.raises(ValueError, match="deployment identifier"):
            getter()


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
@pytest.mark.parametrize("service,relative", [("consumer", "company_os/app/consumer/cognito.py"),
                                             pytest.param("gateway", "gateway/app/config.py", marks=pytest.mark.hosted)])
def test_hosted_cognito_environment_does_not_require_deployment_file(tmp_path, monkeypatch, domain, service, relative):
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("COMPANY_OS_DEPLOYMENT_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_TestPool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "testclient123")
    monkeypatch.setenv("COGNITO_DOMAIN", f"https://auth.{domain}")
    root = Path(__file__).resolve().parents[2]
    name = f"hosted_cognito_{service}"
    spec = importlib.util.spec_from_file_location(name, root / "services" / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    if service == "consumer":
        assert module.pool_id() == "us-east-1_TestPool"
        assert module.client_id() == "testclient123"
        assert module.hosted_domain() == f"https://auth.{domain}"
    else:
        settings = module.Settings(_env_file=None)
        assert settings.cognito_user_pool_id == "us-east-1_TestPool"
        assert settings.cognito_client_id == "testclient123"
        assert settings.cognito_domain == f"https://auth.{domain}"


@pytest.fixture()
def consumer_env(tmp_path, monkeypatch):
    if not (COMPANY_OS / "app/api/consumer_api.py").is_file():
        pytest.skip("Hosted consumer accounts are not in the public local release.")
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/consumer.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "account-session-secret-32bytes!!")
    monkeypatch.setenv("FOUNDER_ADMIN_EMAILS", "founder@supymem.com")
    monkeypatch.setenv("COGNITO_CLIENT", "fake")
    monkeypatch.setenv("HOSTED_LLM_GATEWAY_ENABLED", "0")
    priv, pub = generate_keypair()
    monkeypatch.setenv("CREDIT_LEASE_PRIVATE_KEY", priv)
    monkeypatch.setenv("CREDIT_LEASE_PUBLIC_KEY", pub)
    load_company_os_app()
    from app import state
    from app.credits.policy import invalidate
    from app.consumer.cognito import FakeCognitoClient, set_cognito_client

    state.reset_db_factory()
    state.db()
    invalidate()
    fake = FakeCognitoClient()
    set_cognito_client(fake)
    yield fake
    set_cognito_client(None)
    invalidate()


@pytest.fixture()
def client(consumer_env) -> TestClient:
    from app.api.consumer_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_account_api_identifies_by_cognito_sub_and_email(client: TestClient) -> None:
    start = client.post(
        "/api/auth/v2/cognito/email-otp/start",
        json={"email": "Ada@Example.com"},
    )
    assert start.status_code == 200
    verify = client.post(
        "/api/auth/v2/cognito/email-otp/verify",
        json={"email": "Ada@Example.com", "code": "246801"},
    )
    assert verify.status_code == 200
    body = verify.json()
    token = body["session_token"]
    account = body["account"]
    assert account["email"] == "ada@example.com"
    assert account["cognito_sub"] == "fake-ada@example.com"
    assert account["user_id"].startswith("csu_")
    assert account["plan"] == "trial"
    assert account["plan_active"] is True
    assert account["credits"]["available_micro"] > 0
    assert "lease" in body

    me = client.get("/api/v1/account", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    again = me.json()
    assert again["email"] == "ada@example.com"
    assert again["cognito_sub"] == account["cognito_sub"]
    assert again["user_id"] == account["user_id"]
    from datetime import datetime, timezone

    started = datetime.fromisoformat(account["plan_started_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(account["plan_expires_at"].replace("Z", "+00:00"))
    assert expires.tzinfo is not None
    assert started.tzinfo is not None
    assert (expires - started).days == 14
    assert expires > datetime.now(timezone.utc)


def test_account_api_requires_session(client: TestClient) -> None:
    res = client.get("/api/v1/account")
    assert res.status_code == 401


def test_consumer_signup_log_omits_email(consumer_env, caplog):
    from app.consumer.cognito import CognitoIdentity
    from app.consumer.users import upsert_from_cognito

    with caplog.at_level("INFO", logger="app.consumer.users"):
        upsert_from_cognito(CognitoIdentity(sub="opaque-cognito-id", email="private@example.test"))
    messages = [record.getMessage() for record in caplog.records if record.name == "app.consumer.users"]
    assert any("consumer signup sub=opaque-cognito-id" in message for message in messages)
    assert all("private@example.test" not in message for message in messages)
