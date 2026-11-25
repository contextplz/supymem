"""Hosted default-model path requires auth; BYOK never hits company Azure."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.hosted import BYOK_PLATFORM_FEE_MICRO, PUBLIC_MODEL_ID
from llm_gateway.hosted_server import AuthFailed, HostedLLMHooks, create_hosted_llm_router


async def _azure(**kwargs: Any) -> dict[str, Any]:
    return {"id": "chatcmpl_test", "model": "should-be-stripped", "choices": []}


def _auth(token: str | None) -> str:
    if not token:
        raise AuthFailed("missing")
    if token == "platform-secret":
        raise AuthFailed("platform key")
    return "usr_test"


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> HostedLLMHooks:
    monkeypatch.setenv("HOSTED_LLM_GATEWAY_ENABLED", "1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    return HostedLLMHooks(authenticate=_auth, azure_complete=_azure)


@pytest.fixture
def client(hooks: HostedLLMHooks) -> TestClient:
    app = FastAPI()
    app.include_router(create_hosted_llm_router(hooks, prefix="/api/v1/llm"))
    return TestClient(app)


def test_unauthenticated_default_path_does_not_call_azure(
    client: TestClient, hooks: HostedLLMHooks
) -> None:
    res = client.post(
        "/api/v1/llm/v1/chat/completions",
        json={"model": PUBLIC_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 401
    assert hooks.azure_calls == []


def test_authenticated_default_path_records_azure_hop(
    client: TestClient, hooks: HostedLLMHooks
) -> None:
    res = client.post(
        "/api/v1/llm/v1/chat/completions",
        headers={"Authorization": "Bearer user-jwt"},
        json={"model": PUBLIC_MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 200
    assert res.json()["model"] == PUBLIC_MODEL_ID
    assert len(hooks.azure_calls) == 1


def test_byok_usage_does_not_debit_company_azure(
    client: TestClient, hooks: HostedLLMHooks
) -> None:
    res = client.post(
        "/api/v1/llm/usage/byok",
        headers={"Authorization": "Bearer user-jwt"},
        json={"idempotency_key": "byok-1", "tokens_in": 10, "tokens_out": 4},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["azure_contacted"] is False
    assert body["fee_micro"] == BYOK_PLATFORM_FEE_MICRO
    assert hooks.azure_calls == []
    assert hooks.byok_fees == ["usr_test"]


def test_healthz_does_not_echo_base_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://secret.invalid/openai")
    res = client.get("/api/v1/llm/healthz")
    assert res.status_code == 200
    dumped = res.text
    assert "secret.invalid" not in dumped
    assert "LLM_BASE_URL" not in dumped


def test_platform_key_as_bearer_is_rejected(
    client: TestClient, hooks: HostedLLMHooks
) -> None:
    res = client.post(
        "/api/v1/llm/v1/chat/completions",
        headers={"Authorization": "Bearer platform-secret"},
        json={"model": PUBLIC_MODEL_ID, "messages": []},
    )
    assert res.status_code == 401
    assert hooks.azure_calls == []
