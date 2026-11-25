"""BYOK LLM settings keep secrets in the broker and platform defaults intact."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import types
import subprocess

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["freight-ledgers.invalid", "balcony-herbs.invalid"])
def test_env_model_key_stays_bound_to_its_service_url(local_provider_env, monkeypatch, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import local_env_import, local_secrets, providers_local
    from app.llm.founder_provider import FounderLLMConfigError, resolve_llm_config

    monkeypatch.setattr(local_env_import, "repo_root", lambda: local_provider_env / "repository")
    application = FastAPI()
    application.include_router(providers_local.router)
    first_url, second_url = (f"https://{name}.{domain}/v1" for name in ("primary", "alternate"))
    first_key, second_key = "fixture-primary-credential", "fixture-alternate-credential"
    env_file = local_provider_env / ".env"

    def write_env(endpoint, key):
        env_file.write_text(f"OPENAI_COMPATIBLE_BASE_URL={endpoint}\nOPENAI_COMPATIBLE_API_KEY={key}\n"
                            "SUPYMEM_MODEL_CHAT=chat-model\n")

    write_env(first_url, first_key)
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.get("/local/providers").json()["configured"]
        assert client.put("/local/providers", json={"provider": "openai_compatible", "base_url": first_url}).is_success
        write_env(second_url, second_key)
        local_env_import.import_local_env(strict=True)
        assert local_secrets.get_secret("model", "openai_compatible") == first_key

        write_env(first_url, first_key)
        response = client.put("/local/providers", json={"provider": "openai_compatible", "base_url": second_url})
        assert response.is_success and response.json()["secrets_set"] == {"api_key": False}
        local_env_import.import_local_env(strict=True)
        assert local_secrets.get_secret("model", "openai_compatible") is None
        with pytest.raises(FounderLLMConfigError, match="Keys and connections"):
            resolve_llm_config(domain)
        response = client.put("/local/providers", json={"provider": "openai_compatible", "api_key": second_key})
        assert response.is_success and second_key not in response.text
        config = resolve_llm_config(domain)
        assert config.base_url == second_url and config.local_config.api_key.get_secret_value() == second_key


@pytest.mark.parametrize("domain", ["invoice-routing.invalid", "patio-birding.invalid"])
@pytest.mark.parametrize("replace", [False, True])
def test_partial_legacy_provider_migration_resumes_after_restart(local_provider_env, monkeypatch, domain, replace):
    from uuid import uuid4
    from app.core import local_secrets, providers_local

    root = providers_local._root()
    root.mkdir(parents=True, mode=0o700)
    first_ref, second_ref = str(uuid4()), str(uuid4())
    first_key, second_key, replacement = "fixture-first-legacy", "fixture-delayed-legacy", "fixture-owner-key"
    second_url = f"https://inference.{domain}/v1"
    path = root / "settings.json"
    local_secrets._atomic_write(path, json.dumps({"provider": "openai", "providers": {
        "openai": {"base_url": f"https://chat.{domain}/v1", "models": {"chat": "first-model"}, "key_ref": first_ref},
        "openai_compatible": {"base_url": second_url, "models": {"chat": "second-model"}, "key_ref": second_ref},
    }}).encode())
    material = {"key": None, "storage": "keyfile", "legacy": {first_ref: first_key}}
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: material)
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    assert local_secrets.get_secret("model", "openai") == first_key
    assert local_secrets.get_secret("model", "openai_compatible") is None
    assert path.exists()
    marker = local_provider_env / "local-credentials-migrated.json"
    assert not marker.exists()
    if replace:
        providers_local.save_settings(provider="openai", api_key=replacement)
    material = {**material, "legacy": {first_ref: first_key, second_ref: second_key}}
    assert local_secrets.get_secret("model", "openai_compatible") == second_key
    assert local_secrets.get_secret("model", "openai") == (replacement if replace else first_key)
    assert not path.exists()
    assert set(json.loads(marker.read_text())) == {first_ref, second_ref}


@pytest.mark.parametrize("domain", ["parts-forecast.invalid", "weeknight-meals.invalid"])
def test_saved_provider_status_distinguishes_pending_key_pipe(local_provider_env, monkeypatch, domain):
    import base64
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import local_secrets, providers_local

    providers_local.save_settings(provider="openai_compatible", base_url=f"https://{domain}/v1",
                                  api_key="fixture-startup-key", models={"chat": "saved-model"})
    wrapping_key = base64.b64encode((local_provider_env / "local-key.bin").read_bytes()).decode()
    material = None
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: material)
    application = FastAPI()
    application.include_router(providers_local.router)
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        pending = client.get("/local/providers")
        assert pending.status_code == 503
        assert pending.json()["detail"]["code"] == "local_credentials_pending"
        assert "system keychain" in pending.json()["detail"]["message"]
        material = {"key": wrapping_key, "storage": "keychain", "legacy": {}}
        assert client.get("/local/providers").json()["configured"] is True


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_python_children_never_import_keyring(local_provider_env, domain):
    code = '''
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "keyring" or name.startswith("keyring."):
        raise AssertionError("Python must not open the system keychain")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from app.core.local_secrets import update_settings, get_secret
from app.core.providers_local import save_settings, load_local_config
update_settings("smtp", {"host": sys.argv[1]}, {"password": "test-credential"})
save_settings(provider="openai", api_key="model-credential", models={"chat": sys.argv[1]})
assert get_secret("smtp", "password") == "test-credential"
assert get_secret("model", "openai") == "model-credential"
assert load_local_config().api_key.get_secret_value() == "model-credential"
'''
    environment = {**os.environ, "SUPYMEM_DESKTOP_INSTANCE_ID": domain}
    result = subprocess.run([sys.executable, "-c", code, domain], env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_child_is_ready_before_the_shell_credential_read_finishes(local_provider_env, domain):
    import base64
    from app.core.local_secrets import update_settings
    update_settings("smtp", {"host": domain}, {"password": "test-only-password"})
    material = {"key": base64.b64encode((local_provider_env / "local-key.bin").read_bytes()).decode(), "legacy": {}, "storage": "keychain"}
    code = '''
import threading
from app.core.local_secrets import get_secret
assert get_secret("smtp", "password") is None
print("ready", flush=True)
for thread in threading.enumerate():
    if thread is not threading.main_thread():
        thread.join(timeout=5)
assert get_secret("smtp", "password") == "test-only-password"
print("connected", flush=True)
'''
    process = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "SUPYMEM_LOCAL_KEY_PIPE": "1"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        process.stdin.write(json.dumps(material) + "\n")
        process.stdin.close()
        assert process.stdout.readline().strip() == "connected"
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture()
def local_provider_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / ".env"))
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    for name in (
        "SUPYMEM_LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
        *(f"SUPYMEM_MODEL_{role}" for role in ("CHAT", "PLANNING", "EXTRACTION", "VISION", "REALTIME")),
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_provider_write_keeps_one_backend_decision(local_provider_env, monkeypatch, domain):
    from app.core import providers_local
    from app.core.local_secrets import read_settings
    root = providers_local._root()
    document = {"provider": "openai", "providers": {"openai": {"models": {"chat": domain}}}}
    with providers_local._locked(root):
        providers_local._write(root, document)
    assert read_settings("model")["values"] == document
    assert not (root / "settings.json").exists()
    assert not (root / "settings.enc").exists()


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_local_provider_key_and_endpoint_are_one_snapshot(local_provider_env, monkeypatch, domain):
    from app.core import providers_local

    old_key = domain + "-old-credential"
    new_key = domain + "-new-credential"
    providers_local.save_settings(provider="openai_compatible", base_url="https://before.example/v1",
        api_key=old_key, models={"chat": domain})
    original_read = providers_local._read
    pending = True

    def change_after_read(root):
        nonlocal pending
        document = original_read(root)
        if pending:
            pending = False
            providers_local.save_settings(provider="openai_compatible", base_url="https://after.example/v1",
                api_key=new_key, models={"chat": domain})
        return document

    monkeypatch.setattr(providers_local, "_read", change_after_read)
    config = providers_local.load_local_config()
    assert config.base_url == "https://before.example/v1"
    assert config.api_key.get_secret_value() == old_key
    config = providers_local.load_local_config()
    assert config.base_url == "https://after.example/v1"
    assert config.api_key.get_secret_value() == new_key


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_shared_store_migration_reads_endpoint_and_key_as_one_snapshot(local_provider_env, monkeypatch, domain):
    from app.core import providers_local
    from app.core.local_secrets import update_settings
    providers_local.save_settings(provider="openai_compatible", base_url="https://before.example/v1", api_key="old-test-credential", models={"chat": domain})
    original = providers_local.read_settings
    changed = False
    def update_between_reads(section):
        nonlocal changed
        result = original(section)
        if section == "model" and not changed:
            changed = True
            update_settings("model", {"provider": "openai_compatible", "providers": {"openai_compatible": {
                "base_url": "https://after.example/v1", "models": {"chat": domain}}}}, {"openai_compatible": "new-test-credential"})
        return result
    monkeypatch.setattr(providers_local, "read_settings", update_between_reads)
    config = providers_local.load_local_config()
    assert config.base_url == "https://after.example/v1"
    assert config.api_key.get_secret_value() == "new-test-credential"


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_existing_desktop_keys_survive_secret_service_outage(local_provider_env, monkeypatch, caplog, domain):
    from app.core import providers_local
    from app.core.local_secrets import get_secret
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    second = domain + "-second-credential"
    replacement = domain + "-replacement-credential"
    providers_local.save_settings(provider="openai_compatible", base_url="https://models.example/v1", api_key=second, models={"chat": domain})
    providers_local.save_settings(provider="openai", api_key=domain + "-first-credential", models={"chat": domain})
    providers_local.save_settings(provider="openai", api_key=replacement)
    assert providers_local.load_local_config().api_key.get_secret_value() == replacement
    assert get_secret("model", "openai_compatible") == second
    assert (local_provider_env / "local-key.bin").is_file()
    for secret in (second, replacement):
        assert secret not in caplog.text
        assert secret.encode() not in (local_provider_env / "local-settings.json").read_bytes()


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_desktop_without_secret_service_uses_encrypted_store(local_provider_env, monkeypatch, caplog, domain):
    from app.core import providers_local
    from app.core.local_secrets import storage_notice
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    secret = domain + "-credential"
    providers_local.save_settings(provider="openai", api_key=secret, models={"chat": domain})
    assert (local_provider_env / "local-key.bin").is_file()
    assert (local_provider_env / "local-key.bin").stat().st_mode & 0o077 == 0
    assert providers_local.load_local_config().api_key.get_secret_value() == secret
    if sys.platform == "linux":
        assert "protected file because no system keychain was found" in storage_notice()
        assert "Keep your data folder private" in storage_notice()
    else:
        assert "key file in your data folder" in storage_notice()
        assert "Keep that folder private" in storage_notice()
        assert "Unlock your system keychain" in storage_notice()
    assert secret not in caplog.text
    assert secret.encode() not in (local_provider_env / "local-settings.json").read_bytes()


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
@pytest.mark.parametrize("desktop", [False, True])
def test_env_provider_import_and_app_precedence(local_provider_env, monkeypatch, domain, desktop):
    from app.core import local_env_import, providers_local
    from app.core.local_secrets import get_secret, read_settings

    monkeypatch.setattr(local_env_import, "repo_root", lambda: local_provider_env / "source")
    if desktop:
        monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    else:
        monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    path = local_provider_env / ".env"
    path.write_text("SUPYMEM_LLM_PROVIDER=openai\nOPENAI_API_KEY=" + domain + "-env\n"
        "SUPYMEM_MODEL_CHAT=" + domain + "\nSUPYMEM_MODEL_PLANNING=planning-model\n"
        "AZURE_OPENAI_API_KEY=azure-credential\nAZURE_OPENAI_ENDPOINT=https://azure.example\n"
        "ANTHROPIC_API_KEY=anthropic-credential\nOPENAI_COMPATIBLE_API_KEY=compatible-credential\n"
        "OPENAI_COMPATIBLE_BASE_URL=https://compatible.example/v1\n")
    local_env_import.import_local_env()
    assert get_secret("model", "openai") == domain + "-env"
    assert get_secret("model", "azure_openai") == "azure-credential"
    assert get_secret("model", "anthropic") == "anthropic-credential"
    assert get_secret("model", "openai_compatible") == "compatible-credential"
    assert providers_local.load_local_config()._local_role_models["planning"] == "planning-model"
    assert read_settings("model")["sources"]["openai.api_key"] == "env"
    assert providers_local._provider_view()["sources"]["api_key"] == "env"
    providers_local.save_settings(provider="openai", api_key=domain + "-app", models={"chat": domain + "-saved-model"})
    path.write_text("SUPYMEM_LLM_PROVIDER=anthropic\nOPENAI_API_KEY=changed-env-key\n"
        "OPENAI_BASE_URL=https://other.example/v1\nSUPYMEM_MODEL_CHAT=changed-model\n")
    local_env_import.import_local_env()
    config = providers_local.load_local_config()
    assert config.provider == "openai"
    assert config.api_key.get_secret_value() == domain + "-app"
    assert config.base_url == "https://api.openai.com/v1"
    assert config._local_role_models["chat"] == domain + "-saved-model"
    assert read_settings("model")["sources"]["openai.api_key"] == "app"
    assert providers_local._provider_view()["sources"]["api_key"] == "app"


@pytest.mark.parametrize("domain", ["supplier-audit", "cooking-classes"])
@pytest.mark.parametrize("desktop", [False, True])
def test_sample_setup_never_reads_keys_or_contacts_a_model(local_provider_env, monkeypatch, domain, desktop):
    import asyncio
    from time import perf_counter
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import local_owner, providers_local

    monkeypatch.setenv("LLM_OFFLINE", "1")
    if desktop:
        monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    else:
        monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    application = FastAPI()
    application.include_router(providers_local.router)
    application.add_middleware(local_owner.LocalOwnerMiddleware)
    monkeypatch.setattr(local_owner, "ensure_owner", lambda: types.SimpleNamespace(id=f"per_{domain}", email=None))
    monkeypatch.delenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", raising=False)

    def unavailable(*args, **kwargs):
        pytest.fail("Sample answers must not read provider storage, unlock a keychain or probe a model")

    monkeypatch.setattr(providers_local, "configured_providers", unavailable)
    monkeypatch.setattr(providers_local, "_store_settings", unavailable)
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        from core.local_security import _proxy_secret

        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        session = client.get("/auth/session").json()
        assert session["authenticated"] is True and session["email"] is None
        started = perf_counter()
        response = client.post("/local/providers/sample", json={})
        assert perf_counter() - started < 1
        assert response.status_code == 200, domain
        assert response.json()["configured"] is True
        assert client.get("/local/providers").json()["configured"] is True
        private_file = local_provider_env / "private-data"
        private_file.write_text(domain)
        private_file.chmod(0o600)
        marker = providers_local._root() / "sample"
        marker.unlink()
        os.link(private_file, marker)
        assert client.post("/local/providers/sample", json={}).status_code == 200
        assert private_file.read_text() == domain
    config = providers_local.load_local_config()
    assert not config.base_url
    assert config.source == "founder_override"
    from llm_gateway.client import LLMClient

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    model = LLMClient()
    assert model.offline is True
    assert asyncio.run(model.chat([{"role": "user", "content": domain}], purpose="env_chat_completion")).content
    assert not (local_provider_env / "vault/providers/settings.enc").exists()
    assert not (local_provider_env / "vault/providers/settings.json").exists()


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
def test_desktop_keychain_identity_preserves_existing_credentials(local_provider_env, monkeypatch, domain):
    from app.core import local_secrets
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    monkeypatch.setattr(local_secrets, "_material_started", True)
    monkeypatch.setattr(local_secrets, "_material", {"key": None, "legacy": {domain: domain + "-credential"}, "storage": "keyfile"})
    monkeypatch.setattr(local_secrets, "_receive_material", lambda: pytest.fail("Credential handoff was retried"))
    for _ in range(3):
        assert local_secrets.legacy_secret(domain) == domain + "-credential"
        assert local_secrets.legacy_secret("missing") is None


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
@pytest.mark.parametrize("desktop", [False, True])
def test_loaded_provider_keys_are_redacted_after_restart(local_provider_env, monkeypatch, domain, desktop):
    import logging
    from app.core import providers_local
    from core import logging as local_logging

    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    if desktop:
        monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    secret = domain + " saved provider credential"
    providers_local.save_settings(provider="openai", api_key=secret, models={"chat": domain})
    monkeypatch.setattr(local_logging, "_LOCAL_SECRETS", set())
    assert providers_local._read(providers_local._root())["providers"]["openai"]["api_key"] == secret
    record = logging.LogRecord("provider-restart", logging.INFO, __file__, 1, "Provider failed: %s", (secret,), None)
    assert secret not in local_logging.JsonFormatter().format(record)


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
@pytest.mark.parametrize("desktop", [False, True])
def test_local_setup_defaults_keep_keys_and_safe_locking(local_provider_env, monkeypatch, domain, desktop):
    from app.core import providers_local
    from app.core.local_secrets import read_settings
    if desktop:
        monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    secret = f"test-only-{domain}-credential"
    status = providers_local.save_settings(provider="openai", api_key=secret, models={"chat": domain})
    assert status["providers"]["openai"]["base_url"] == "https://api.openai.com/v1"
    root = providers_local._root()
    stored = local_provider_env / "local-settings.json"
    assert root.stat().st_mode & 0o077 == 0
    assert secret.encode() not in stored.read_bytes()
    providers_local.save_settings(provider="openai", models={"chat": domain + "-new"})
    assert providers_local.load_local_config().api_key.get_secret_value() == secret
    fields = read_settings("model")["values"]["providers"]["openai"]
    assert "api_key" not in fields and "key_ref" not in fields
    before = stored.read_bytes()
    with pytest.raises(providers_local.LocalProviderError):
        providers_local._store_settings({"provider": "openai", "models": {"chat": domain}}, sample=True)
    assert stored.read_bytes() == before
    target = local_provider_env / "lock-target"
    target.write_bytes(b"keep your existing file")
    (root / ".lock").unlink()
    (root / ".lock").symlink_to(target)
    with pytest.raises(providers_local.LocalProviderError):
        providers_local.save_settings(provider="openai", models={"chat": domain})
    assert target.read_bytes() == b"keep your existing file"
    assert stored.read_bytes() == before


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
@pytest.mark.parametrize("provider,endpoint", [("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com/v1")])
def test_local_known_provider_saves_default_url(local_provider_env, provider, endpoint, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import providers_local

    secret = "test-only-private-credential"
    status = providers_local.save_settings(provider=provider, api_key=secret, models={"chat": domain})
    assert status["providers"][provider]["configured"]
    assert status["providers"][provider]["base_url"] == endpoint
    assert providers_local._read(providers_local._root())["providers"][provider]["base_url"] == endpoint
    assert secret not in repr(status)
    app = FastAPI()
    app.include_router(providers_local.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    response = client.post("/local/providers", json={"provider": provider, "api_key": secret, "model": domain})
    assert response.status_code == 200 and response.json()["configured"]
    assert secret not in response.text


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
def test_local_connection_checks_draft_once_without_saving(local_provider_env, monkeypatch, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import providers_local
    from app.credits import metered
    from llm_gateway import sole

    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    monkeypatch.delenv("LLM_CASSETTE_MODE", raising=False)
    calls = []
    result = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"connected":true}'))], usage={"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9})
    original_call = metered.call

    async def completion(messages, **options):
        calls.append(options)
        assert options["config"].base_url == "https://api.openai.com/v1"
        assert options["config"].deployment_pro == domain
        assert options["single_attempt"] is True
        assert options["max_tokens"] == 400 and options["response_schema"] == {"type": "json_object"}
        return result

    async def charged(callback, **options):
        assert options["estimate_micro"] > 0
        assert callable(options["actual_micro"])
        reply = await callback()
        assert type(options["actual_micro"](reply)) is int
        return reply

    monkeypatch.setattr(sole, "byok_chat_completion", completion)
    monkeypatch.setattr(metered, "call", charged)
    app = FastAPI()
    app.include_router(providers_local.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    draft = {"provider": "openai", "api_key": "test-only-private-credential", "model": domain}
    response = client.post("/local/providers/test", json=draft, headers={"Origin": "http://127.0.0.1:4100"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert len(calls) == 1
    assert not providers_local._read(providers_local._root())["providers"]
    result.choices[0].message.content = ""
    response = client.post("/local/providers/test", json=draft)
    assert response.status_code == 502 and response.json()["detail"]["code"] == "model_json_unsupported"
    assert "Keys and connections" in response.text and "Chat model" in response.text
    assert draft["api_key"] not in response.text

    async def rejected(*args, **kwargs):
        raise RuntimeError(draft["api_key"])

    monkeypatch.setattr(sole, "byok_chat_completion", rejected)
    response = client.post("/local/providers/test", json=draft)
    assert response.status_code == 502 and draft["api_key"] not in response.text
    assert "try" in response.text.lower()
    before = len(calls)
    response = client.post("/local/providers/test", json={**draft, "base_url": "http://remote.invalid/v1"})
    assert response.status_code == 422 and len(calls) == before
    response = client.post("/local/providers/test", json=draft, headers={"Origin": "https://" + domain + ".invalid"})
    assert response.status_code == 403
    monkeypatch.setattr(sole, "byok_chat_completion", completion)
    monkeypatch.setattr(metered, "call", original_call)
    from app.core.local_secrets import update_settings
    update_settings("spend", {"monthly_spend_cap": "0"}, {})
    response = client.post("/local/providers/test", json=draft)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "local_spend_limit" and len(calls) == before
    assert draft["api_key"] not in response.text
    update_settings("spend", {"monthly_spend_cap": "1"}, {})
    result.choices[0].message.content = '{"connected":true}'
    response = client.post("/local/providers/test", json=draft)
    assert response.status_code == 200 and len(calls) == before + 1
    from app.credits import local_meter
    receipt = local_meter.snapshot()["tasks"][0]
    assert receipt["unconfirmed_calls"] == 0
    assert receipt["prompt_tokens"] == 8 and receipt["completion_tokens"] == 1


@pytest.fixture
def audit_provider_client(local_provider_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import providers_local
    from app.credits import metered

    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    monkeypatch.delenv("LLM_CASSETTE_MODE", raising=False)
    app = FastAPI()
    app.include_router(providers_local.router)

    async def call(callback, **options):
        return await callback()

    monkeypatch.setattr(metered, "call", call)
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))


@pytest.fixture
def audited_provider(audit_provider_client, monkeypatch):
    from llm_gateway import sole

    wires = []

    def connect(config):
        async def close():
            pass

        connection = types.SimpleNamespace(close=close)
        connection.with_options = lambda **options: connection
        return connection, config

    async def dial(client, config, kwargs):
        wires.append((config, kwargs))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"connected":true}'))])

    monkeypatch.setattr(sole, "create_async_client_from_config", connect)
    monkeypatch.setattr(sole, "_dial_chat", dial)
    monkeypatch.setattr(sole, "note_usage", lambda *args, **kwargs: None)
    return audit_provider_client, wires


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_azure_custom_deployment_uses_underlying_model_for_test_and_chat(audit_provider_client, monkeypatch, domain):
    from app.core.providers_local import load_local_config
    from llm_gateway import provider_foundry, sole
    from llm_gateway.limiter import httpx
    from llm_gateway.routing import resolve_purpose_config

    wires = []

    def provider(request):
        body = json.loads(request.content)
        wires.append((request.url.host, request.url.path, body))
        content = '{"connected":true}'
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"id": "resp_audit", "status": "completed", "model": body["model"],
                "output": [{"id": "msg_audit", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": content, "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})
        return httpx.Response(200, json={"id": "chat_audit", "object": "chat.completion", "created": 1,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    monkeypatch.setattr(provider_foundry, "_http_client", lambda hooks: httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    for deployment, model, mode, operation in [(f"{domain}-writer", "gpt-6.1-sol", "responses", "responses"),
            (f"gpt-6-{domain}-helper", "gpt-4o-mini", "chat_completions", "chat/completions")]:
        payload = {"provider": "azure_openai", "base_url": f"https://{domain}.invalid", "model": deployment,
                   "catalog_model": model, "api_key": "synthetic-azure-private-value"}
        assert audit_provider_client.put("/local/providers", json=payload).status_code == 200
        result = audit_provider_client.post("/local/providers/test", json={"provider": "azure_openai"})
        assert result.status_code == 200
        chat = resolve_purpose_config("venture.chat.assistant", load_local_config(), tier="pro")
        assert (chat.api_mode, chat.catalog_model, chat.deployment_pro) == (mode, model, deployment)
        asyncio.run(sole.byok_chat_completion([{"role": "user", "content": domain}], config=chat,
            purpose="venture.chat.assistant", tier="pro", single_attempt=True))
        for host, path, body in wires[-2:]:
            assert (host, path, body["model"]) == (f"{domain}.invalid", f"/openai/v1/{operation}", deployment)
        probe = wires[-2][2]
        if mode == "responses":
            assert probe["text"]["format"]["type"] == "json_object"
        else:
            assert probe["response_format"]["type"] == "json_object" and probe["max_tokens"] == 400


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_only_successful_saved_provider_probe_is_ready(audited_provider, monkeypatch, domain):
    from llm_gateway import sole

    client, wires = audited_provider
    payload = {"provider": "openai_compatible", "base_url": f"https://{domain}.invalid/v1", "model": domain,
               "api_key": "synthetic-compatible-private-value"}
    assert client.put("/local/providers", json=payload).json()["verified"] is False
    assert client.post("/local/providers/test", json={**payload, "model": f"{domain}-draft"}).status_code == 200
    assert client.get("/local/providers").json()["verified"] is False
    dial = sole._dial_chat

    async def reject(*args):
        raise RuntimeError("private upstream diagnostic")

    monkeypatch.setattr(sole, "_dial_chat", reject)
    assert client.post("/local/providers/test", json={"provider": payload["provider"]}).status_code == 502
    assert client.get("/local/providers").json()["verified"] is False
    monkeypatch.setattr(sole, "_dial_chat", dial)
    assert client.post("/local/providers/test", json={"provider": payload["provider"]}).status_code == 200
    assert client.get("/local/providers").json()["verified"] is True
    monkeypatch.setattr(sole, "_dial_chat", reject)
    assert client.post("/local/providers/test", json={"provider": payload["provider"]}).status_code == 502
    assert client.get("/local/providers").json()["verified"] is False
    monkeypatch.setattr(sole, "_dial_chat", dial)
    assert client.post("/local/providers/test", json={"provider": payload["provider"]}).status_code == 200
    assert client.put("/local/providers", json={**payload, "model": f"{domain}-new"}).json()["verified"] is False
    draft = {**payload, "model": f"{domain}-onboarding"}
    assert client.post("/local/providers/test", json=draft).status_code == 200
    assert client.get("/local/providers").json()["verified"] is False
    calls = len(wires)
    assert client.put("/local/providers", json=draft).json()["verified"] is True
    assert len(wires) == calls


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_saved_provider_verification_survives_restart_and_tracks_credentials(audited_provider, domain):
    from app.core.providers_local import load_local_config

    client, _ = audited_provider
    payload = {"provider": "openai_compatible", "base_url": f"https://{domain}.invalid/v1", "model": domain,
               "api_key": "synthetic-compatible-private-value"}
    saved = client.put("/local/providers", json=payload).json()
    assert saved["configured"] and not saved["verified"]
    assert load_local_config()._local_role_models["chat"] == domain
    saved_probe = {"provider": payload["provider"]}
    assert client.post("/local/providers/test", json=saved_probe).json()["verified"] is True

    restarted = subprocess.run([sys.executable, "-c", "from app.core.providers_local import _provider_view; "
                                "assert _provider_view()['verified'] is True"], capture_output=True, timeout=30)
    assert restarted.returncode == 0

    changed_key = {**saved_probe, "api_key": "synthetic-replacement-private-value"}
    assert client.put("/local/providers", json=changed_key).json()["verified"] is False
    assert client.post("/local/providers/test", json=saved_probe).json()["verified"] is True
    changed_host = {**saved_probe, "base_url": f"https://other.{domain}.invalid/v1"}
    assert client.put("/local/providers", json=changed_host).json()["verified"] is False
    reverted = client.put("/local/providers", json={**payload, **changed_key}).json()
    assert reverted["configured"] and not reverted["verified"]


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_provider_probe_requires_chat_json_and_uses_responses_budget(audited_provider, monkeypatch, domain):
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError

    client, wires = audited_provider
    dial = sole._dial_chat
    truncated = False

    async def provider(transport, config, request):
        if config.api_mode == "chat_completions" and "response_format" in request:
            raise ProviderCallError("provider_error", "private upstream diagnostic", status=400)
        if config.api_mode == "responses":
            assert request["response_format"]["type"] == "json_object"
            assert request["max_completion_tokens"] > 256
        reply = await dial(transport, config, request)
        reply.choices[0].finish_reason = "length" if truncated else "stop"
        return reply

    monkeypatch.setattr(sole, "_dial_chat", provider)
    payload = {"provider": "openai_compatible", "base_url": f"https://{domain}.invalid/v1", "model": domain,
               "api_key": "synthetic-compatible-private-value"}
    failed = client.post("/local/providers/test", json=payload)
    assert failed.status_code == 502 and failed.json()["detail"]["code"] == "model_json_unsupported"
    assert "private upstream diagnostic" not in failed.text
    assert client.post("/local/providers/test", json={**payload, "model": "gpt-6.1-sol"}).status_code == 200
    truncated = True
    assert client.post("/local/providers/test", json={**payload, "model": "gpt-6.1-sol"}).status_code == 502


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_azure_save_and_probe_reject_operation_url(audited_provider, domain):
    client, wires = audited_provider
    root = f"https://{domain}.invalid"
    draft = {"provider": "azure_openai", "api_key": "synthetic-azure-private-value", "model": domain}
    for path in ("", "/openai", "/openai/v1", "/openai/v1/", "/v1"):
        payload = {**draft, "base_url": root + path}
        saved = client.put("/local/providers", json=payload)
        assert saved.status_code == 200, path
        assert saved.json()["values"]["base_url"] == root + "/openai/v1"
        assert client.post("/local/providers/test", json=payload).status_code == 200
        assert wires[-1][0].base_url == root + "/openai/v1"
    count = len(wires)
    payload = {**draft, "base_url": f"{root}/openai/deployments/{domain}/chat/completions"}
    for response in (client.put("/local/providers", json=payload), client.post("/local/providers/test", json=payload)):
        assert response.status_code == 422
        assert all(field in response.text for field in ("Service URL", "resource endpoint", "Chat model"))
    assert len(wires) == count


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_loopback_probe_uses_entered_model_without_discovery(audited_provider, monkeypatch, domain):
    from llm_gateway import provider_foundry, sole
    from llm_gateway.errors import ProviderCallError

    client, wires = audited_provider
    discovery_status = 404

    async def models():
        error = RuntimeError("private upstream diagnostic")
        error.status_code = discovery_status
        raise error

    async def close():
        pass

    monkeypatch.setattr(provider_foundry, "create_async_client_from_config",
                        lambda config: (types.SimpleNamespace(models=types.SimpleNamespace(list=models), close=close), config))
    payload = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:16410/v1", "model": domain}
    assert client.post("/local/providers/test", json=payload).status_code == 200
    assert wires[-1][1]["model"] == domain
    count = len(wires)
    discovery_status = 401
    assert client.post("/local/providers/test", json=payload).json()["detail"]["code"] == "bad_api_key"
    assert len(wires) == count
    discovery_status = 404

    async def reject(*args):
        raise ProviderCallError("model_not_found", "private upstream diagnostic", status=404)

    monkeypatch.setattr(sole, "_dial_chat", reject)
    rejected = client.post("/local/providers/test", json=payload)
    assert rejected.status_code == 502 and "Chat model" in rejected.text
    assert "private upstream diagnostic" not in rejected.text


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_cold_loopback_probe_keeps_chat_deadline_without_extending_cloud(audited_provider, monkeypatch, domain):
    from contextlib import asynccontextmanager
    from app.core import providers_local
    from app.llm import founder_provider
    from llm_gateway import sole

    client, wires = audited_provider
    deadlines = []
    elapsed = 0
    dial = sole._dial_chat

    @asynccontextmanager
    async def timeout(seconds):
        deadlines.append(seconds)
        yield
        if elapsed >= seconds:
            raise TimeoutError

    async def models(config):
        return [domain]

    async def cold(transport, config, kwargs):
        nonlocal elapsed
        elapsed = 20
        assert config.timeout_seconds == deadlines[-1]
        return await dial(transport, config, kwargs)

    monkeypatch.setattr(providers_local, "asyncio", types.SimpleNamespace(timeout=timeout))
    monkeypatch.setattr(founder_provider, "compatible_provider_models", models)
    monkeypatch.setattr(sole, "_dial_chat", cold)
    payload = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:16410/v1", "model": domain}
    assert client.post("/local/providers/test", json=payload).status_code == 200
    assert deadlines == [10, 600]
    deadlines.clear()
    elapsed = 0
    assert client.post("/local/providers/test", json={**payload, "base_url": f"https://{domain}.invalid/v1",
                                                      "api_key": "synthetic-compatible-private-value"}).status_code == 504
    assert deadlines == [10]


@pytest.mark.parametrize("domain", ["quarry-dispatch", "porch-birdwatching"])
def test_provider_context_override_null_resets_chat_default(audited_provider, domain):
    from app.core.providers_local import load_local_config
    from llm_gateway.provider_foundry import DEFAULT_LOOPBACK_CONTEXT

    client, _ = audited_provider
    payload = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:16410/v1", "model": domain}
    assert client.put("/local/providers", json={**payload, "context_window": 32768}).status_code == 200
    assert client.put("/local/providers", json=payload).json()["values"]["context_window"] == 32768
    assert load_local_config().context_window == 32768
    saved = client.put("/local/providers", json={**payload, "context_window": None})
    assert saved.status_code == 200 and saved.json()["values"].get("context_window") is None
    assert load_local_config().context_window == DEFAULT_LOOPBACK_CONTEXT


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
@pytest.mark.parametrize("mode", ["live", "offline", "replay"])
def test_local_sample_mode_is_available_and_switches_to_saved_service(local_provider_env, monkeypatch, domain, mode):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core import providers_local

    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    monkeypatch.delenv("LLM_CASSETTE_MODE", raising=False)
    if mode == "offline":
        monkeypatch.setenv("LLM_OFFLINE", "1")
    elif mode == "replay":
        monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    app = FastAPI()
    app.include_router(providers_local.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    assert client.get("/local/providers").json()["sample_mode"] is True, domain
    response = client.post("/local/providers/sample", json={})
    assert response.status_code == 200
    assert client.get("/local/providers").json()["configured"] is True
    assert not providers_local.load_local_config().api_key.get_secret_value()
    assert providers_local.load_local_config().provider == "local"
    import asyncio
    from app.core import local_owner

    async def lifespan(*arguments):
        return None

    with monkeypatch.context() as startup:
        startup.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
        startup.setattr(local_owner, "_ensure_proxy_secret", lambda: None)
        startup.setattr(local_owner, "ensure_owner", lambda: None)
        startup.setattr(providers_local, "configured_providers", lambda: pytest.fail("Sample startup must not open your saved service key"))
        asyncio.run(local_owner.LocalOwnerMiddleware(lifespan)({"type": "lifespan"}, lifespan, lifespan))
    if mode != "live":
        assert client.post("/local/providers/test", json={}).status_code == 409
    providers_local.save_settings(provider="openai_compatible", api_key="test-only-private-credential",
        base_url="http://localhost:11434/v1", models={"chat": domain})
    assert providers_local.load_local_config().provider == "openai_compatible"
    assert providers_local.load_local_config()._local_role_models["chat"] == domain
    monkeypatch.setenv("SUPYMEM_MODEL_CHAT", "")
    snapshot = (local_provider_env / "local-settings.json").read_bytes()
    response = client.post("/local/providers/sample", json={})
    assert response.status_code == 200
    assert (local_provider_env / "local-settings.json").read_bytes() == snapshot
    assert "test-only-private-credential" not in response.text
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert client.post("/local/providers/sample", json={}).status_code == 404
    assert (local_provider_env / "local-settings.json").read_bytes() == snapshot


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
def test_local_keys_encrypted_and_role_routes_pinned(local_provider_env, monkeypatch, domain):
    from app.core.providers_local import (
        configured_providers,
        load_local_config,
        save_settings,
    )
    from app.llm import founder_provider
    from llm_gateway.routing import resolve_purpose_config
    from llm_gateway.sole import resolve_override

    models = {role: f"{domain}-{role}" for role in ("chat", "planning", "extraction", "vision", "realtime")}
    secret = f"test-only-{domain}-credential"
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1", api_key=secret, models=models)
    status = configured_providers()
    assert status["provider"] == "openai_compatible"
    assert secret not in repr(status)
    files = list((local_provider_env / "vault" / "providers").iterdir())
    assert files and all(secret.encode() not in path.read_bytes() for path in files)
    assert all(path.stat().st_mode & 0o077 == 0 for path in files)

    def no_hosted_store(*args, **kwargs):
        pytest.fail("local routing consulted the hosted credential store")

    monkeypatch.setattr(founder_provider, "get_founder_config", no_hosted_store)
    resolved = founder_provider.resolve_llm_config(domain)
    config = founder_provider.to_provider_config(domain, resolved_config=resolved)
    assert config is not None and config.source == "founder_override"
    assert resolve_override() == config
    assert secret not in repr(resolved) and secret not in repr(config)
    assert founder_provider.to_provider_config("another-workspace", resolved_config=resolved) is None
    for purpose, role in (
        ("venture.chat.assistant", "chat"), ("venture.chat.route", "chat"),
        ("task.plan", "planning"), ("memory.extract", "extraction"), ("device.vision", "vision"),
    ):
        captured = resolve_purpose_config(purpose, config)
        assert captured.deployment_pro == captured.deployment_flash == models[role]
        assert captured.api_key.get_secret_value() == secret
        monkeypatch.setenv(f"SUPYMEM_MODEL_{role.upper()}", "changed-after-admission")
        assert resolve_purpose_config(purpose, captured) == captured
    assert load_local_config().deployment_pro == models["planning"]


@pytest.mark.parametrize("provider,key_env,url_env", [
    ("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
    ("azure_openai", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
    ("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    ("openai_compatible", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"),
])
@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["wholesale", "gardening"])
async def test_local_imported_and_saved_providers_ignore_later_environment_changes(local_provider_env, monkeypatch, provider, key_env, url_env, domain):
    from app.core.local_secrets import get_secret, read_settings
    from app.core.providers_local import configured_providers, load_local_config, save_settings
    from llm_gateway.provider_foundry import (
        ProviderConfig,
        create_async_client,
        create_async_client_from_config,
        load_provider_config_from_env,
    )

    monkeypatch.setenv("SUPYMEM_LLM_PROVIDER", provider)
    monkeypatch.setenv(key_env, "your-test-credential")
    imported_host = f"https://imported-{domain}.openai.azure.com" if provider == "azure_openai" else "https://provider.example"
    monkeypatch.setenv(url_env, f"{imported_host}/v1")
    monkeypatch.setenv("SUPYMEM_MODEL_CHAT", "your-chat-model")
    monkeypatch.setenv("LLM_API_KEY", "hosted-key-must-not-be-used")
    from app.core.local_env_import import import_local_env
    import_local_env()
    config = load_provider_config_from_env()
    assert config.base_url == imported_host + ("/openai/v1" if provider == "azure_openai" else "/v1")
    assert config.api_key.get_secret_value() == "your-test-credential"
    assert config == load_local_config()
    assert (local_provider_env / "local-settings.json").is_file()
    assert "your-test-credential" not in repr(configured_providers())
    client, used = create_async_client_from_config(config)
    assert used == config and client.api_key == "your-test-credential"
    assert client._client.follow_redirects is False
    await client.close()
    client, used = create_async_client(ProviderConfig())
    assert used == config and client.api_key == "your-test-credential"
    await client.close()

    endpoint = f"https://{domain}.example/openai/v1" if provider == "azure_openai" else f"https://{domain}.example/v1"
    save_settings(provider=provider, base_url=endpoint,
        api_key="saved-test-credential", models={"chat": "saved-chat-model"})
    assert get_secret("model", provider) == "saved-test-credential"
    assert read_settings("model")["sources"][f"{provider}.api_key"] == "app"
    monkeypatch.setenv("SUPYMEM_LLM_PROVIDER", "openai" if provider == "anthropic" else "anthropic")
    config = load_provider_config_from_env()
    assert config.api_key.get_secret_value() == "saved-test-credential"
    assert config.deployment_flash == "saved-chat-model"
    assert config.base_url == endpoint
    assert config == load_local_config()
    assert configured_providers()["provider"] == provider
    assert "saved-test-credential" not in repr(configured_providers())
    client, used = create_async_client_from_config(config)
    assert used == config and client.api_key == "saved-test-credential"
    assert client._client.follow_redirects is False
    await client.close()
    client, used = create_async_client(ProviderConfig())
    assert used == config and client.api_key == "saved-test-credential"
    await client.close()


def test_local_keyless_endpoint_and_bad_settings_fail_closed(local_provider_env):
    from app.core.providers_local import (
        LocalProviderError,
        load_local_config,
        save_settings,
    )

    save_settings(provider="openai_compatible", base_url="http://[::1]:1234/v1", models={"chat": "local-model"})
    assert load_local_config().base_url == "http://[::1]:1234/v1"
    for endpoint in ("https://user:password@provider.example/v1", "https://provider.example/v1?key=secret", "file:///tmp/model", "https://provider.example/" + "x" * 2048):
        with pytest.raises(LocalProviderError):
            save_settings(provider="openai_compatible", base_url=endpoint, api_key="private", models={"chat": "model"})
    result = save_settings(provider="openai_compatible", base_url="https://provider.example/v1", models={"chat": "model"})
    assert not result["providers"]["openai_compatible"]["configured"]
    assert not result["providers"]["openai_compatible"]["api_key_set"]
    with pytest.raises(LocalProviderError, match="Keys and connections"):
        load_local_config()


def test_local_app_endpoint_and_key_win_over_environment(local_provider_env, monkeypatch):
    from app.core.providers_local import (
        LocalProviderError,
        load_local_config,
        save_settings,
    )

    save_settings(provider="openai_compatible", base_url="https://first.example/v1", api_key="first-endpoint-test-key", models={"chat": "model"})
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://second.example/v1")
    assert load_local_config().base_url == "https://first.example/v1"
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "second-endpoint-test-key")
    assert load_local_config().api_key.get_secret_value() == "first-endpoint-test-key"


def test_local_startup_and_existing_settings_api_work_before_setup(local_provider_env, monkeypatch):
    from app.api.llm_settings_api import (
        LLMSettingsBody,
        delete_llm_settings,
        get_llm_settings,
        put_llm_settings,
    )
    from llm_gateway import provider_foundry

    def unexpected_config():
        pytest.fail("startup requires a configured provider before you can open setup")

    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", unexpected_config)
    provider_foundry.log_platform_routing_config()
    assert get_llm_settings("your-workspace")["provider"] is None
    response = put_llm_settings("your-workspace", LLMSettingsBody(provider="openai", model="your-model", api_key="your-test-key"))
    assert response["model"] == "your-model" and "your-test-key" not in repr(response)
    assert delete_llm_settings("your-workspace")["provider"] is None


@pytest.mark.parametrize("damage", ["ciphertext", "machine_secret"])
def test_local_unreadable_keys_are_never_overwritten(local_provider_env, damage):
    from app.core.providers_local import LocalProviderError, configured_providers, save_settings
    save_settings(provider="openai", api_key="saved-test-key", models={"chat": "saved-model"})
    if damage == "ciphertext":
        (local_provider_env / "local-settings.json").write_bytes(b"damaged")
    else:
        (local_provider_env / "local-key.bin").unlink()
    before = {path.name: path.read_bytes() for path in local_provider_env.iterdir() if path.is_file()}
    with pytest.raises(LocalProviderError):
        configured_providers()
    with pytest.raises(LocalProviderError):
        save_settings(provider="openai", api_key="replacement-test-key", models={"chat": "new-model"})
    assert {path.name: path.read_bytes() for path in local_provider_env.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
def test_local_failed_replace_preserves_your_saved_key(local_provider_env, monkeypatch, domain):
    from app.core import providers_local

    providers_local.save_settings(provider="openai", api_key=f"{domain}-old-test-key", models={"chat": f"{domain}-model"})
    path = local_provider_env / "local-settings.json"
    before = path.read_bytes()

    def unavailable(*args, **kwargs):
        raise PermissionError

    monkeypatch.setattr(providers_local.os, "replace", unavailable)
    with pytest.raises(providers_local.LocalProviderError):
        providers_local.save_settings(provider="openai", api_key=f"{domain}-new-test-key")
    assert path.read_bytes() == before
    assert providers_local.load_local_config().api_key.get_secret_value() == f"{domain}-old-test-key"


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
@pytest.mark.parametrize("legacy", ["encrypted", "plaintext"])
def test_desktop_provider_keys_migrate_into_shared_encrypted_store(local_provider_env, monkeypatch, caplog, domain, legacy):
    from app.core import providers_local
    from app.core.local_secrets import get_secret, read_settings
    root = providers_local._root()
    root.mkdir(parents=True, mode=0o700)
    secret = f"test-only-{domain}-credential"
    document = {"provider": "openai_compatible", "providers": {"openai_compatible": {
        "base_url": f"https://{domain}.example/v1", "api_key": secret, "models": {"chat": domain}}}}
    path = root / ("settings.enc" if legacy == "encrypted" else "settings.json")
    payload = json.dumps(document).encode()
    if legacy == "encrypted":
        payload = providers_local._cipher(root, create=True).encrypt(payload)
    path.write_bytes(payload)
    path.chmod(0o600)
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    document["providers"]["openai_compatible"]["models"]["chat"] = secret
    leaked = json.dumps(document).encode()
    if legacy == "encrypted":
        leaked = providers_local._cipher(root).encrypt(leaked)
    path.write_bytes(leaked)
    with pytest.raises(providers_local.LocalProviderError):
        providers_local.load_local_config()
    assert path.read_bytes() == leaked
    path.write_bytes(payload)
    with monkeypatch.context() as patch:
        patch.setattr(providers_local.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError()))
        with pytest.raises(providers_local.LocalProviderError):
            providers_local.load_local_config()
    assert path.read_bytes() == payload
    assert providers_local.load_local_config().api_key.get_secret_value() == secret
    assert get_secret("model", "openai_compatible") == secret
    assert "api_key" not in read_settings("model")["values"]["providers"]["openai_compatible"]
    assert not path.exists()
    assert secret.encode() not in (local_provider_env / "local-settings.json").read_bytes()
    assert secret not in caplog.text
    with monkeypatch.context() as patch:
        patch.setenv("OPENAI_COMPATIBLE_BASE_URL", f"https://other-{domain}.example/v1")
        config = providers_local.load_local_config()
        assert config.base_url == f"https://{domain}.example/v1"
        assert config.api_key.get_secret_value() == secret
    providers_local.save_settings(provider="openai_compatible", models={"chat": domain + "-new"})
    assert providers_local.load_local_config().api_key.get_secret_value() == secret
    providers_local.clear_settings()
    assert not get_secret("model", "openai_compatible")


def test_local_setup_api_is_loopback_only_and_never_returns_keys(local_provider_env, monkeypatch):
    from app.api.llm_settings_api import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    application = FastAPI()
    application.include_router(router)
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        response = client.put("/local/llm-settings", json={"provider": "openai", "api_key": "api-test-secret", "models": {"chat": "test-model"}})
        assert response.status_code == 200
        assert "api-test-secret" not in response.text
        assert client.get("/local/llm-settings").json()["provider"] == "openai"
        response = client.put("/local/llm-settings", json={"provider": "openai", "api_key": "api-test-secret", "base_url": "https://provider.example/api-test-secret", "models": {"chat": "test-model"}})
        assert response.status_code == 422 and "api-test-secret" not in response.text
        assert client.put("/local/llm-settings", headers={"Origin": "https://untrusted.example"}, json={}).status_code == 403
        response = client.put("/local/llm-settings", json={"provider": "unknown", "api_key": "rejected-secret"})
        assert response.status_code == 422 and "rejected-secret" not in response.text
        monkeypatch.delenv("SUPYMEM_MODE")
        assert client.get("/local/llm-settings").status_code == 404
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with TestClient(application, base_url="http://localhost", client=("192.0.2.1", 12345)) as client:
        assert client.get("/local/llm-settings").status_code == 403


def test_local_provider_environment_is_ignored_when_hosted(llm_env, monkeypatch, tmp_path):
    from app.llm.founder_provider import resolve_llm_config
    from llm_gateway.provider_foundry import load_provider_config_from_env

    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("SUPYMEM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "ignored-local-test-key")
    monkeypatch.setenv("SUPYMEM_MODEL_CHAT", "ignored-local-model")
    assert resolve_llm_config(llm_env).source == "platform_default"
    assert load_provider_config_from_env().api_key.get_secret_value() == "platform-secret"
    assert not (tmp_path / "vault" / "providers").exists()


@pytest.fixture()
def llm_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek_direct")
    monkeypatch.setenv("LLM_BASE_URL", "https://platform.example/v1")
    monkeypatch.setenv("LLM_DEPLOYMENT_NAME", "deepseek-platform")
    monkeypatch.delenv("LLM_DEPLOYMENT_FLASH", raising=False)
    monkeypatch.delenv("LLM_DEPLOYMENT_PRO", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "platform-secret")
    # This focused unit suite does not exercise token hashing; keep the Company
    # OS router importable in lean test environments without argon2 installed.
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    load_company_os_app()

    from app import state
    from app.connectors.secrets import reset_store
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state.reset_db_factory()
    reset_store()
    return ensure_desk(LOCAL_PERSON_ID).id


def test_default_falls_back_to_platform_env(llm_env):
    from app.llm.founder_provider import resolve_llm_config

    config = resolve_llm_config(llm_env)

    assert config.source == "platform_default"
    assert config.provider == "deepseek_direct"
    assert config.base_url == "https://platform.example/v1"
    assert config.model == "deepseek-platform"
    assert config.api_key_ref is None


def test_founder_override_wins(llm_env):
    from app.llm.founder_provider import resolve_llm_config, set_founder_config

    set_founder_config(
        workspace_id=llm_env,
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="deepseek/deepseek-chat",
        api_key="founder-secret",
    )

    config = resolve_llm_config(llm_env)

    assert config.source == "founder_override"
    assert config.provider == "openrouter"
    assert config.model == "deepseek/deepseek-chat"
    assert config.api_key_ref and config.api_key_ref.startswith("vault:")


def test_to_provider_config_returns_byok(llm_env):
    from app.llm.founder_provider import set_founder_config, to_provider_config

    set_founder_config(
        workspace_id=llm_env,
        provider="openai",
        base_url="https://api.openai.com/v1",
        model="luna",
        api_key="founder-secret",
    )
    cfg = to_provider_config(llm_env)
    assert cfg is not None
    assert cfg.source == "founder_override"
    assert cfg.api_key.get_secret_value() == "founder-secret"
    assert cfg.deployment_pro == "luna"


def test_platform_view_hides_azure_endpoint_and_key(llm_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv(
        "LLM_BASE_URL",
        "https://fixture-resource.services.ai.azure.com/api/projects/fixture-project",
    )
    monkeypatch.setenv("LLM_API_KEY", "real-azure-key-must-not-leak")
    from app.api.llm_settings_api import get_llm_settings
    from app.llm.founder_provider import resolve_llm_config

    internal = resolve_llm_config(llm_env)
    assert internal.provider == "azure_foundry"
    assert internal.base_url and "fixture-project" in internal.base_url

    response = get_llm_settings(llm_env)
    blob = repr(response)
    assert response["source"] == "platform_default"
    assert response["hosted"] is True
    assert response["api_key_set"] is False
    assert response["api_key"] is None
    assert response["base_url"] is None
    assert response["provider"] == "azure_foundry"
    assert response["model"] == internal.model
    assert response["models"] == [internal.model]
    assert response["offer"]["model"] == internal.model
    assert response["offer"]["provider"] == "azure_foundry"
    assert "fixture-project" not in blob
    assert "real-azure-key-must-not-leak" not in blob


def test_get_response_never_contains_raw_key(llm_env):
    from app.api.llm_settings_api import get_llm_settings
    from app.llm.founder_provider import set_founder_config

    raw_key = "founder-secret-must-not-leak"
    set_founder_config(
        workspace_id=llm_env,
        provider="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4-5",
        api_key=raw_key,
    )

    response = get_llm_settings(llm_env)

    assert response["api_key"] == "********"
    assert response["api_key_set"] is True
    assert raw_key not in repr(response)
    assert "api_key_ref" not in response
    assert "deepseek-platform" in (response.get("offer") or {}).get("disclosure", "")


def test_platform_default_is_foundry_pro(llm_env, monkeypatch):
    for key in ("LLM_PROVIDER", "LLM_DEPLOYMENT_NAME", "LLM_DEPLOYMENT_PRO"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "legacy-flash-deployment")
    from app.llm.founder_provider import platform_offer, resolve_llm_config

    config = resolve_llm_config(llm_env)
    assert config.provider == "azure_foundry"
    assert config.model == "DeepSeek-V4-Pro"
    assert platform_offer()["model"] == config.model


def test_platform_metadata_tracks_configured_pro_deployment(llm_env, monkeypatch):
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "configured-pro-deployment")
    from app.api.llm_settings_api import get_llm_settings

    response = get_llm_settings(llm_env)
    assert response["model"] == "configured-pro-deployment"
    assert response["offer"]["model"] == response["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["", "ok"])
async def test_connection_check_requires_a_real_reply(llm_env, monkeypatch, reply):
    from app.api.llm_settings_api import test_llm_connection
    from fastapi import HTTPException
    from llm_gateway import sole

    async def completion(messages, *, max_tokens, workspace_id):
        assert workspace_id == llm_env
        assert max_tokens >= 128  # Reasoning models exhausted the previous eight-token cap.
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=reply)
        )])

    monkeypatch.setattr(sole, "env_chat_completion", completion)
    if reply:
        response = await test_llm_connection(llm_env)
        assert response == {"ok": True}
    else:
        with pytest.raises(HTTPException) as caught:
            await test_llm_connection(llm_env)
        assert caught.value.status_code == 502
        assert caught.value.detail["error"]["code"] == "empty_response"


@pytest.mark.parametrize("domain", ["fleet-planning", "home-baking"])
def test_local_provider_setup_contract_preserves_keys_and_rejects_foreign_calls(local_provider_env, monkeypatch, domain):
    from app.core import providers_local
    from app.llm import founder_provider
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    application = FastAPI()
    application.include_router(providers_local.router)
    secret = f"private-{domain}-credential"
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.get("/local/providers").json()["configured"] is False
        assert client.get("/setup/status").json()["llm_mode"] == "unconfigured"
        response = client.post("/local/providers", json={"provider": "openai_compatible",
            "base_url": "http://127.0.0.1:1234/v1", "api_key": secret, "model": domain})
        assert response.status_code == 200
        assert response.json()["configured"] is True and secret not in response.text
        response = client.put("/local/providers", json={"provider": "openai_compatible", "model": domain + "-new"})
        assert response.status_code == 200 and secret not in response.text
        assert response.json()["values"]["model"] == domain + "-new"
        assert response.json()["secrets_set"] == {"api_key": True}
        catalog = client.get("/local/providers")
        assert catalog.status_code == 200 and secret not in catalog.text
        assert catalog.json()["default_models"]["openai"] == founder_provider.DEFAULT_MODELS["openai"]
        assert catalog.json()["values"]["model"] == domain + "-new"
        assert providers_local.load_local_config().api_key.get_secret_value() == secret
        rejected = client.post("/local/providers", json={"provider": "unsupported", "api_key": secret, "model": domain})
        assert rejected.status_code == 422 and secret not in rejected.text
        assert client.get("/local/providers", headers={"Origin": f"https://{domain}.example"}).status_code == 403
        status = client.get("/setup/status").json()
        assert status["mode"] == "local" and status["llm_mode"] == "live"
        assert status["email_mode"] == "log" and secret not in str(status)
        monkeypatch.delenv("SUPYMEM_MODE")
        assert client.get("/local/providers").status_code == 404
        assert client.get("/setup/status").status_code == 404


@pytest.mark.parametrize("domain", ["factory-maintenance", "weekend-gardening"])
def test_local_vision_uses_saved_role_without_hosted_credentials(local_provider_env, monkeypatch, domain):
    from app.core import providers_local
    from app.llm import founder_provider
    from llm_gateway.routing import resolve_purpose_config

    secret = domain + "-private-key"
    models = {"chat": domain + "-chat", "planning": domain + "-plan", "vision": domain + "-vision"}
    providers_local.save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
        api_key=secret, models=models)
    monkeypatch.setattr(founder_provider, "get_founder_config", lambda *args: pytest.fail("Local vision consulted hosted settings"))
    config = founder_provider.local_vision_config(domain)
    assert config.deployment_pro == config.deployment_flash == models["vision"]
    assert config.base_url == "http://127.0.0.1:1234/v1" and config.api_key.get_secret_value() == secret
    assert config.source == "founder_override" and secret not in repr(config)
    assert resolve_purpose_config("device.ai_step.plan", config, tier="pro").deployment_pro == models["vision"]
    assert providers_local.load_local_config()._local_role_models["planning"] == models["planning"]
    monkeypatch.setenv("SUPYMEM_MODEL_VISION", "")
    assert founder_provider.local_vision_config(domain).deployment_pro == models["vision"]
    providers_local.clear_settings()
    with pytest.raises(founder_provider.FounderLLMConfigError, match="Settings"):
        founder_provider.local_vision_config(domain)
