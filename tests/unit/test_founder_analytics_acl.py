"""Founder analytics stay hidden from regular users; users see only their usage."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.credit_lease import generate_keypair
from tests.company_os_harness import COMPANY_OS, load_company_os_app


@pytest.fixture()
def consumer_env(tmp_path, monkeypatch):
    if not (COMPANY_OS / "app/api/consumer_api.py").is_file():
        pytest.skip("Hosted account analytics are not in the public local release.")
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/acl.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "account-session-secret-32bytes!!")
    monkeypatch.setenv("FOUNDER_ADMIN_EMAILS", "founder@supymem.com")
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
    from app.api.consumer_api import account_router, auth_router, founder_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(founder_router)
    return TestClient(app)


def _signup(client: TestClient, email: str) -> str:
    client.post("/api/auth/v2/cognito/email-otp/start", json={"email": email})
    res = client.post(
        "/api/auth/v2/cognito/email-otp/verify",
        json={"email": email, "code": "246801"},
    )
    return res.json()["session_token"]


def test_founder_analytics_hidden_from_normal_users(client: TestClient) -> None:
    user_token = _signup(client, "user@example.com")
    founder_token = _signup(client, "founder@supymem.com")

    denied = client.get(
        "/api/v1/founder/analytics",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert denied.status_code == 403

    users_denied = client.get(
        "/api/v1/founder/users",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert users_denied.status_code == 403

    granted = client.get(
        "/api/v1/founder/analytics",
        headers={"Authorization": f"Bearer {founder_token}"},
    )
    assert granted.status_code == 200
    body = granted.json()
    assert body["signups"] >= 2
    assert "credits_granted" in body
    assert "agents" in body


def test_founder_grant_hidden_from_normal_users(client: TestClient) -> None:
    user_token = _signup(client, "user@example.com")
    denied = client.post(
        "/api/v1/founder/credits/grant",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"email": "user@example.com", "credits": 10, "reason": "should fail"},
    )
    assert denied.status_code == 403


def test_user_sees_only_own_usage(client: TestClient) -> None:
    from app.consumer.session import parse_session
    from app.consumer.users import record_usage

    token_a = _signup(client, "a@example.com")
    token_b = _signup(client, "b@example.com")
    uid_a = parse_session(token_a).user_id
    uid_b = parse_session(token_b).user_id
    record_usage(
        user_id=uid_a,
        route="spend_default_llm",
        tokens_in=11,
        tokens_out=7,
        credits_micro=100,
    )
    record_usage(
        user_id=uid_b,
        route="spend_default_llm",
        tokens_in=99,
        tokens_out=88,
        credits_micro=500,
    )

    usage_a = client.get(
        "/api/v1/account/usage",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert usage_a.status_code == 200
    items_a = usage_a.json()["items"]
    assert items_a
    assert all(row["tokens_in"] != 99 for row in items_a)
    assert any(row["tokens_in"] == 11 for row in items_a)
    assert usage_a.json()["user_id"] == uid_a

    usage_b = client.get(
        "/api/v1/account/usage",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    items_b = usage_b.json()["items"]
    assert any(row["tokens_in"] == 99 for row in items_b)
    assert all(row["tokens_in"] != 11 for row in items_b)
