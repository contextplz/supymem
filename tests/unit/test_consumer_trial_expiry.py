"""14-day default plan expiry denies hosted LLM until they buy again."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.credit_lease import generate_keypair
from llm_gateway.hosted import PUBLIC_MODEL_ID
from tests.company_os_harness import COMPANY_OS, load_company_os_app


@pytest.fixture()
def consumer_env(tmp_path, monkeypatch):
    if not (COMPANY_OS / "app/api/hosted_llm_api.py").is_file():
        pytest.skip("Hosted model-access trials are not in the public local release.")
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/trial.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "account-session-secret-32bytes!!")
    monkeypatch.setenv("FOUNDER_ADMIN_EMAILS", "founder@supymem.com")
    monkeypatch.setenv("HOSTED_LLM_GATEWAY_ENABLED", "1")
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
def app_client(consumer_env, monkeypatch):
    from app.api.consumer_api import router
    from llm_gateway.hosted_server import HostedLLMHooks, create_hosted_llm_router
    from app.api.hosted_llm_api import _authenticate, _charge_default

    azure_calls: list[dict] = []

    async def azure(**kwargs):
        return {"id": "cmpl", "choices": []}

    hooks = HostedLLMHooks(
        authenticate=_authenticate,
        azure_complete=azure,
        charge_default=_charge_default,
    )
    hooks.azure_calls = azure_calls
    app = FastAPI()
    app.include_router(router)
    app.include_router(create_hosted_llm_router(hooks, prefix="/api/v1/llm"))
    client = TestClient(app)
    client.azure_calls = azure_calls  # type: ignore[attr-defined]
    return client


def _signup(client: TestClient, email: str = "ada@example.com") -> tuple[str, dict]:
    client.post("/api/auth/v2/cognito/email-otp/start", json={"email": email})
    res = client.post(
        "/api/auth/v2/cognito/email-otp/verify",
        json={"email": email, "code": "246801"},
    )
    body = res.json()
    return body["session_token"], body["account"]


def test_expired_trial_denies_hosted_llm_without_azure(app_client: TestClient) -> None:
    from app.consumer.users import get_user
    from app.credits._txn import naive_utc, session_factory
    from app.db.models import ConsumerUser

    token, account = _signup(app_client)
    user = get_user(account["user_id"])
    assert user is not None
    factory = session_factory()
    with factory() as session:
        row = session.get(ConsumerUser, user.id)
        assert row is not None
        row.plan_expires_at = naive_utc() - timedelta(days=1)
        session.commit()

    res = app_client.post(
        "/api/v1/llm/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": PUBLIC_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 402
    assert "plan_expired" in str(res.json())
    assert app_client.azure_calls == []  # type: ignore[attr-defined]


def test_buying_again_reopens_hosted_window(app_client: TestClient) -> None:
    from app.consumer.users import extend_plan, get_user
    from app.credits._txn import naive_utc, session_factory
    from app.db.models import ConsumerUser

    token, account = _signup(app_client, email="buy@example.com")
    factory = session_factory()
    with factory() as session:
        row = session.get(ConsumerUser, account["user_id"])
        assert row is not None
        row.plan_expires_at = naive_utc() - timedelta(days=1)
        session.commit()

    denied = app_client.post(
        "/api/v1/llm/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": PUBLIC_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert denied.status_code == 402

    extend_plan(account["user_id"])
    refreshed = get_user(account["user_id"])
    assert refreshed is not None
    assert refreshed.plan_expires_at > naive_utc()

    allowed = app_client.post(
        "/api/v1/llm/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": PUBLIC_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert allowed.status_code == 200
    assert len(app_client.azure_calls) == 1  # type: ignore[attr-defined]
