"""Synthetic quarantine checks: no Docker, SDK execution, secrets or paid calls."""
from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from tests.company_os_harness import load_company_os_app

ROOT = Path(__file__).resolve().parents[2]


def _private_key() -> str:
    return "sk_app_" + "synthetic" + "." + "a" * 24


@pytest.fixture
def proxy(monkeypatch):
    path = ROOT / "services/ai_proxy/app/main.py"
    if not path.is_file():
        pytest.skip("Hosted legacy proxy is not in the public local release.")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    spec = importlib.util.spec_from_file_location("security_legacy_proxy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = module._v1
    def forbidden(*args, **kwargs):
        pytest.fail("quarantined proxy crossed a key, guardrail or metering boundary")
    keys = ModuleType("app.apps.keys")
    keys.lookup_bearer = forbidden
    monkeypatch.setitem(sys.modules, "app.apps.keys", keys)
    monkeypatch.setattr(api, "_guard", forbidden)
    monkeypatch.setattr(api, "_meter", forbidden)
    from llm_gateway import sole
    monkeypatch.setattr(sole, "transmit", forbidden)
    return TestClient(module.app), api, keys


def _proxy_requests(client, token):
    headers = {"Authorization": "Bearer " + token}
    return [
        client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "synthetic"}]}, headers=headers),
        client.post("/v1/embeddings", json={"input": "synthetic"}, headers=headers),
        client.post("/v1/moderations", json={"input": "synthetic"}, headers=headers),
        client.get("/v1/models", headers=headers), client.get("/v1/usage", headers=headers),
    ]


@pytest.mark.parametrize("app_env,offline", [
    ("local", "0"), ("local", "1"), ("production", "0"), ("production", "1"),
    ("staging", "1"), (None, "1"), ("invalid", "1"),
])
def test_duplicate_proxy_refuses_before_io_in_every_environment(proxy, monkeypatch, app_env, offline):
    client, api, _ = proxy
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("LLM_OFFLINE", offline)
    with pytest.raises(HTTPException) as denied:
        api._auth("Bearer " + _private_key())
    assert denied.value.status_code == 503
    for response in _proxy_requests(client, _private_key()):
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "legacy_proxy_unavailable"
    # Keep the image's existing liveness/readiness contract unchanged.
    assert client.get("/health").status_code == client.get("/ready").status_code == 200


@pytest.mark.parametrize("app_env,offline", [("local", "1"), ("production", "0")])
def test_public_ingest_key_never_authorizes_legacy_ai(proxy, monkeypatch, app_env, offline):
    client, api, _ = proxy
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("LLM_OFFLINE", offline)
    public = "pk_" + "app_" + "synthetic"
    assert api._parse_token("Bearer " + public) is None
    denied = api._auth("Bearer " + public)
    assert isinstance(denied, JSONResponse) and denied.status_code == 401
    assert all(response.status_code == 401 for response in _proxy_requests(client, public))


@pytest.mark.parametrize("scopes,allowed", [(["chat"], True), (["hermes"], True), (["ingest"], False), ([], False)])
def test_source_owned_mock_auth_backend_requires_ai_scope(proxy, monkeypatch, scopes, allowed):
    _, api, _ = proxy
    # Only a source-owned mock replaces the retired lookup. Actual HTTP and
    # direct authentication remain unavailable, including local offline mode.
    monkeypatch.setattr(api, "_lookup", lambda authorization: {
        "id": "key_synthetic", "app_state": "live", "scopes": scopes})
    result = api._auth("Bearer " + _private_key())
    if allowed:
        assert isinstance(result, tuple) and result[0]["id"] == "key_synthetic"
    else:
        assert isinstance(result, JSONResponse) and result.status_code == 403


@pytest.fixture
def legacy_builder(monkeypatch):
    if not (ROOT / "services/builder/app/openhands_runtime.py").is_file():
        pytest.skip("Hosted legacy builder is not in the public local release.")
    from tests.builder_harness import prefer_builder_path
    from tests.warden_harness import purge_app_modules, restore_app_modules

    monkeypatch.setenv("APP_ENV", "local")
    saved, old = purge_app_modules(), prefer_builder_path()
    try:
        from app import openhands_runtime
        from app.llm import BuilderLLMConfig
        yield openhands_runtime, BuilderLLMConfig
    finally:
        sys.path[:] = old
        restore_app_modules(saved)


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("sandbox", ["openhands", "openhands_local"])
def test_hosted_legacy_builder_refuses_before_sdk_and_provider_key(legacy_builder, monkeypatch, tmp_path, app_env, sandbox):
    runtime, config = legacy_builder
    monkeypatch.setenv("APP_ENV", app_env)
    importer = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "openhands" or name.startswith("openhands."):
            pytest.fail("hosted legacy runtime imported the code-execution SDK")
        return importer(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    def forbidden(*args, **kwargs):
        pytest.fail("hosted legacy runtime constructed an LLM or container")
    monkeypatch.setattr(runtime, "_build_llm", forbidden)
    monkeypatch.setattr(runtime, "register_workspace", forbidden)
    outcome = runtime.run_openhands(prompt="synthetic", workdir=tmp_path / "repo", timeout_s=1,
        llm=config(model="synthetic", api_key="synthetic", base_url=None, api_version=None, provider="synthetic"),
        allow_write=True, sandbox=sandbox)
    assert not outcome.ok and outcome.error == "legacy_sandbox_forbidden"
    assert outcome.changed_files == [] and outcome.events == []
    assert not list(tmp_path.iterdir())


@pytest.fixture
def local_secret_router(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/synthetic.db")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.api import secrets_api
    isolated = FastAPI()
    isolated.include_router(secrets_api.router)
    return TestClient(isolated), secrets_api


@pytest.mark.parametrize("app_env", ["staging", "production", "invalid"])
def test_accidentally_mounted_secret_slots_refuse_all_hosted_routes(local_secret_router, monkeypatch, app_env):
    client, api = local_secret_router
    monkeypatch.setenv("APP_ENV", app_env)
    def forbidden(*args, **kwargs):
        pytest.fail("hosted secret-slot route crossed metadata or credential boundary")
    for name in ("public_catalog", "pending_requests", "slot_status", "resolve_slot_id", "request_secret", "save_secret"):
        monkeypatch.setattr(api, name, forbidden)
    responses = [client.get("/secret-slots"), client.get("/secret-slots/pending"),
        client.get("/secret-slots/openai.api_key/status"),
        client.post("/secret-slots/request", json={"slot_id": "openai.api_key"}),
        client.post("/secret-slots/openai.api_key/value", json={"values": {"OPENAI_API_KEY": "synthetic"}})]
    assert all(response.status_code == 403 for response in responses)
    assert all(response.json()["detail"]["code"] == "local_secret_slots_only" for response in responses)


def test_local_secret_catalog_fixture_remains_available(local_secret_router, monkeypatch):
    client, api = local_secret_router
    monkeypatch.setattr(api, "public_catalog", lambda: [{"id": "synthetic-local-tool"}])
    assert client.get("/secret-slots").json() == {"items": [{"id": "synthetic-local-tool"}]}
