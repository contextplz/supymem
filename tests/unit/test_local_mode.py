from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@contextmanager
def _owner_client(application, **options):
    from tests.company_os_harness import company_os_test_client

    with company_os_test_client(application, **options) as client:
        if not any(name.lower() == "origin" for name in dict(options.get("headers") or {})):
            client.headers.pop("Origin", None)
        yield client


@pytest.fixture(autouse=True)
def core_imports(monkeypatch, isolate_app_imports):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "services" / "company_os"))
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def test_local_mode_is_off_unless_explicitly_set(monkeypatch):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.core.local_mode import is_local

    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    assert not is_local()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert is_local()
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert not is_local()


@pytest.mark.parametrize("domain", ["warehouse-planning", "family-gardening"])
def test_local_offline_resolver_switches_to_live_after_saving(local_owner_app, monkeypatch, domain):
    from app.core import local_mode, providers_local

    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    for value in ("1", " true ", "YES", "on"):
        monkeypatch.setenv("LLM_OFFLINE", value)
        assert local_mode.is_offline()
    providers_local.save_settings(provider="openai", api_key=domain + "-credential", models={"chat": domain})
    assert not local_mode.is_offline()
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    assert local_mode.is_offline()


@pytest.mark.parametrize("domain", ["warehouse planning", "family gardening"])
def test_test_mode_requires_local_unsent_email_and_offline_or_replay(monkeypatch, domain):
    from app.core.local_mode import is_test_mode

    for mode, offline, cassette, email, expected in [
        ("local", "1", "off", "log", True), ("local", "true", "off", "log", True),
        ("local", "0", "replay", "log", True), ("local", "0", "off", "log", False),
        ("hosted", "1", "replay", "log", False), ("local", "1", "replay", "smtp", False),
    ]:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        monkeypatch.setenv("LLM_OFFLINE", offline)
        monkeypatch.setenv("LLM_CASSETTE_MODE", cassette)
        monkeypatch.setenv("EMAIL_PROVIDER", email)
        assert is_test_mode() is expected, domain


@pytest.mark.parametrize("domain", ["warehouse planning", "family gardening"])
def test_saved_sample_choice_routes_core_and_worker_without_model_env(local_owner_app, monkeypatch, domain):
    import asyncio
    import os
    import subprocess
    import sys

    from app.core import local_mode, providers_local
    from llm_gateway import LLMClient, ProviderConfig, provider_foundry, sole
    from pydantic import SecretStr

    for name in ("LLM_OFFLINE", "LLM_CASSETTE_MODE", "GUARDRAILS_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    gateway = LLMClient(config=ProviderConfig(api_key=SecretStr("unused")), offline=False)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.post("/local/providers/sample", json={}).status_code == 200
        assert local_mode.is_test_mode(), domain
        assert client.get("/setup/status").json()["llm_mode"] == "sample"

    def no_model(*args, **kwargs):
        pytest.fail("Your saved sample choice must not create a model client.")

    monkeypatch.setattr(sole, "create_async_client_from_config", no_model)
    monkeypatch.setattr(sole, "open_env_client", no_model)
    config = providers_local.load_local_config()
    assert not config.base_url
    messages = [{"role": "user", "content": domain}]
    for completion in (
        sole.byok_chat_completion(messages, config=config),
        sole.env_chat_completion(messages),
        sole.transmit(messages, purpose="env_chat_completion"),
    ):
        result = asyncio.run(completion)
        assert result.provider_used == "sample"
        assert result.usage.total_tokens == 0
    assert asyncio.run(gateway.chat(messages, purpose="env_chat_completion")).raw["sample"] is True
    for create in (provider_foundry.create_async_client, provider_foundry.create_async_client_from_config):
        with pytest.raises(RuntimeError, match="sample data"):
            create(config)

    async def stream():
        return [chunk async for chunk in sole.transmit_stream(messages, purpose="env_chat_completion")]

    chunks = asyncio.run(stream())
    assert all(chunk["provider_used"] == "sample" for chunk in chunks)
    assert chunks[-1]["usage"]["total_tokens"] == 0
    environment = {name: os.environ[name] for name in (
        "PATH", "SUPYMEM_MODE", "APP_ENV", "COMPANY_OS_DATA_DIR", "EMAIL_PROVIDER"
    ) if name in os.environ}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "packages") + ":" + str(
        Path(__file__).resolve().parents[2] / "services/company_os")
    worker = subprocess.run([sys.executable, "-c", """
import asyncio
from app.core.local_mode import is_test_mode
from llm_gateway.sole import env_chat_completion
assert is_test_mode()
result = asyncio.run(env_chat_completion([{'role': 'user', 'content': 'Your saved idea'}]))
assert result.provider_used == 'sample'
assert result.usage.total_tokens == 0
"""], env=environment, capture_output=True, text=True, timeout=30)
    assert worker.returncode == 0, worker.stderr
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    assert local_mode.is_test_mode()
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert not local_mode.is_sample_mode()


def test_guessed_emails_are_off_unless_explicitly_enabled(monkeypatch, tmp_path):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.config import Settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("GUESSED_EMAILS", raising=False)
    assert Settings(_env_file=None).guessed_emails == "off"
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert "\nGUESSED_EMAILS=off\n" in example
    monkeypatch.setenv("GUESSED_EMAILS", "on")
    assert Settings(_env_file=None).guessed_emails == "on"


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_hosted_guessed_email_default_preserves_explicit_opt_out(monkeypatch, tmp_path, domain):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / domain))
    load_company_os_app()
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.delenv("GUESSED_EMAILS", raising=False)
    from app.config import Settings

    assert Settings(_env_file=None).guessed_emails == "on"
    monkeypatch.setenv("GUESSED_EMAILS", "off")
    assert Settings(_env_file=None).guessed_emails == "off"


@pytest.fixture
def local_owner_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/owner.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(tmp_path / "owner-session.secret"))
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://localhost:3100")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.delenv("SUPYMEM_LOCAL_ALLOW_REMOTE", raising=False)
    from tests.company_os_harness import load_company_os_app

    main = load_company_os_app()
    application = main.create_app()

    @application.get("/_local_probe")
    def probe(request: Request):
        from app.admin.authn import verify_caller
        from app.consumer.session import parse_session
        from app.surfaces.auth import resolve_principal

        caller = verify_caller(request, required=True)
        return {
            "subject": caller.sub,
            "role": caller.role,
            "header": request.headers.get("x-subject"),
            "account": parse_session(None).user_id,
            "surface": resolve_principal(None).person_id,
        }

    @application.websocket("/_local_socket")
    async def socket(connection: WebSocket):
        await connection.accept()
        await connection.send_json({"subject": connection.headers.get("x-subject")})
        await connection.close()

    return application


@pytest.mark.parametrize("idea", ["Parts procurement for factories", "Balcony gardening for renters"], ids=["b2b", "consumer"])
def test_local_startup_and_venture_creation_do_not_contact_external_http(local_owner_app, monkeypatch, idea):
    import httpx
    import socket
    from core.local_security import _local_hostname
    from desktop_runtime.bridge import create_bridge_app

    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "1")
    attempted = []

    def check(host):
        if not _local_hostname(host):
            attempted.append(host)
            raise AssertionError(f"Unexpected outbound request to {host}")

    send = httpx.Client.send
    send_async = httpx.AsyncClient.send
    getaddrinfo = socket.getaddrinfo
    connect = socket.socket.connect

    def checked_send(client, request, *args, **kwargs):
        check(request.url.host)
        return send(client, request, *args, **kwargs)

    async def checked_send_async(client, request, *args, **kwargs):
        check(request.url.host)
        return await send_async(client, request, *args, **kwargs)

    def checked_getaddrinfo(host, *args, **kwargs):
        check(host.decode("ascii") if isinstance(host, bytes) else host)
        return getaddrinfo(host, *args, **kwargs)

    def checked_connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address[0])
        return connect(sock, address)

    monkeypatch.setattr(httpx.Client, "send", checked_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", checked_send_async)
    monkeypatch.setattr(socket, "getaddrinfo", checked_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    bridge = create_bridge_app(local_core="http://127.0.0.1:3300")
    with _owner_client(local_owner_app) as client, TestClient(bridge, base_url="http://127.0.0.1:3500") as bridge_client:
        assert bridge_client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        created = client.post("/ventures", json={"raw_idea": idea, "start": False})
        assert created.status_code == 201, created.text
        assert created.json()["venture"]["raw_idea"] == idea
    assert not attempted


@pytest.mark.parametrize("domain,hosted", [("procurement.example", "app.supymem.com"), ("gardening.example", "app.staging.supymem.com")])
def test_local_core_does_not_trust_a_configured_hosted_console_origin(local_owner_app, monkeypatch, domain, hosted):
    with _owner_client(local_owner_app) as client:
        for host in (domain, hosted):
            origin = f"https://{host}"
            monkeypatch.setenv("CONSOLE_ORIGIN", origin)
            assert client.get("/auth/session", headers={"Origin": origin}).status_code == 403
        assert client.get("/auth/session", headers={"Origin": "tauri://localhost"}).status_code == 200


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_support_bundle_exports_only_safe_crash_metadata(local_owner_app, domain):
    import json
    from desktop_runtime.crash_report import enqueue, enqueue_crash

    recorded = enqueue_crash(stack=f"private exception at {domain}")
    assert json.loads(recorded.read_text())["stack"] == ""
    enqueue({"kind": "crash", "crash_process": "python", "stack": f"private traceback at {domain}",
             "logs": f"private logs at {domain}", "crash_owner": {"account_id": domain}})
    with _owner_client(local_owner_app) as client:
        assert client.get("/local/support-bundle", headers={"X-Local-Proxy-Secret": ""}).status_code == 403
        report = client.get("/local/support-bundle")
        assert report.status_code == 200, report.text
        assert report.headers["content-disposition"].startswith("attachment;")
    crashes = report.json()["desktop_crashes"]
    assert len(crashes) == 2
    assert crashes[0]["process"] == "python"
    assert set(crashes[0]) == {"process", "recorded_at"}
    assert domain not in report.text


@pytest.mark.parametrize("domain", ["pallet-routing.test", "paper-lanterns.test"])
def test_packaged_worker_crash_appears_in_the_next_support_bundle(local_owner_app, tmp_path, domain):
    import subprocess
    import sys

    launcher = Path(__file__).resolve().parents[2] / "scripts/desktop/local_core.py"
    private_marker = f"unhandled worker exception at {domain}"
    crashed = subprocess.run([sys.executable, "-c", """
import runpy
import sys
from types import ModuleType

from desktop_runtime import parent_watch

parent_watch.exit_when_parent_dies = lambda: None
worker = sys.modules['app.worker.main'] = ModuleType('app.worker.main')
marker = sys.argv.pop()

def fail(_args):
    print('worker entered', flush=True)
    raise RuntimeError(marker)

worker.main = fail
launcher = sys.argv.pop(1)
runpy.run_path(launcher, run_name='__main__')
""", str(launcher), "--role", "worker", "--host", "127.0.0.1", "--port", "3301", "--data-dir",
        str(tmp_path), private_marker], capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 1 and crashed.stdout.strip() == "worker entered"
    with _owner_client(local_owner_app) as client:
        bundle = client.get("/local/support-bundle")
    assert bundle.status_code == 200
    assert [crash["process"] for crash in bundle.json()["desktop_crashes"]] == ["python"]
    assert domain not in bundle.text and private_marker not in crashed.stderr


@pytest.mark.parametrize("domain", ["meter-inventory.test", "weekend-origami.test"])
def test_compose_core_probe_needs_no_owner_cookie(local_owner_app, monkeypatch, domain):
    from contextlib import nullcontext
    import hmac
    import runpy
    import secrets
    from urllib.parse import urlsplit

    import yaml

    root = Path(__file__).resolve().parents[2]
    settings = yaml.safe_load((root / "docker-compose.local.yml").read_text())["services"]["core"]["environment"]
    monkeypatch.setenv("SUPYMEM_MODE", settings["SUPYMEM_MODE"])
    monkeypatch.setenv("PORT", settings["PORT"])
    with _owner_client(local_owner_app, base_url=f"http://127.0.0.1:{settings['PORT']}",
                       client=("127.0.0.1", 50000)) as client:
        client.headers.pop("X-Local-Proxy-Secret", None)

        def probe(url, *, timeout):
            assert timeout == 2 and urlsplit(url).netloc == f"127.0.0.1:{settings['PORT']}"
            response = client.get(url)
            if response.is_success:
                assert not response.content
                assert "Set-Cookie" not in response.headers
            return nullcontext(SimpleNamespace(status=response.status_code))

        monkeypatch.setattr("urllib.request.urlopen", probe)
        with pytest.raises(SystemExit) as result:
            runpy.run_path(str(root / "docker/python-healthcheck.py"), run_name="__main__")
        assert result.value.code == 0
        for path in ("/health", "/ready", "/auth/session"):
            assert client.get(path).status_code == 403
        assert client.get("/local-ready", headers={"Host": domain}).status_code == 403
        key, nonce = secrets.token_hex(32), secrets.token_hex(16)
        monkeypatch.setenv("SUPYMEM_LOCAL_READINESS_KEY", key)
        assert client.get("/local-ready").status_code == 403
        ready = client.get("/local-ready", headers={"X-Local-Ready-Nonce": nonce})
        assert ready.status_code == 204 and not ready.content
        assert hmac.compare_digest(ready.headers["X-Local-Ready-Proof"], hmac.digest(key.encode(), nonce.encode(), "sha256").hex())


@pytest.fixture(params=["tool calibration for factories", "birdwatching for beginners"], ids=["b2b", "consumer"])
def audited_venture(local_owner_app, monkeypatch, request):
    from app.core import local_env_import

    monkeypatch.setattr(local_env_import, "_environment", lambda: {})
    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000),
                       raise_server_exceptions=False) as client:
        created = client.post("/ventures", json={"raw_idea": request.param, "start": False})
        assert created.status_code == 201
        yield client, created.json()


def test_local_charter_lock_reuses_receipt_without_replacing_charter(audited_venture):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    client, venture = audited_venture
    base = f"/ventures/{venture['id']}"
    body = {"answers": {"audience": venture["raw_idea"]}, "other_verbatim": {"brand_tone": "plain"}}
    start = Barrier(2)

    def lock():
        start.wait(timeout=5)
        return client.post(f"{base}/charter/lock", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(lock) for _ in range(2)]
        first, second = [request.result(timeout=15) for request in requests]
    assert sorted([first.status_code, second.status_code]) == [200, 200]
    assert first.json() == second.json()
    assert client.post(f"{base}/stage", json={"stage": "paused"}).status_code == 200
    retry = client.post(f"{base}/charter/lock", json=body)
    assert retry.status_code == 200 and retry.json() == first.json()

    for changed_body in ({**body, "dissent": [{"note": "Review the scope"}]}, {"answers": {
        **body["answers"], "brand_tone": "plain", "founder_other_verbatim": body["other_verbatim"],
    }}):
        changed = client.post(f"{base}/charter/lock", json=changed_body)
        assert changed.status_code == 409
        assert "open your saved charter" in changed.json()["detail"]["error"]["message"].lower()
    saved = client.get(f"{base}/charter").json()
    assert saved["charter_id"] == first.json()["charter_id"] and saved["checksum"] == first.json()["checksum"]
    assert client.get(base).json()["stage"] == "paused"


def test_local_export_after_provider_clear_ignores_stale_model_sources(audited_venture):
    import io
    import json
    import zipfile

    from app.core.local_data import _check_public_settings
    from app.core.local_secrets import _update_settings, update_settings

    client, venture = audited_venture
    alternate = client.post("/local/providers", json={"provider": "openai", "model": "test-chat",
                                                     "api_key": "synthetic-export-regression-key"})
    assert alternate.status_code == 200
    saved = client.post("/local/providers", json={"provider": "openai_compatible", "model": "local-chat",
                                                 "base_url": "http://127.0.0.1:9876/v1"})
    assert saved.status_code == 200
    assert client.get("/local/settings/export").status_code == 200
    cleared = client.delete(f"/workspaces/{venture['workspace_id']}/llm-settings")
    assert cleared.status_code == 200
    assert client.get("/local/settings/export").status_code == 200
    sources = cleared.json()["sources"]
    assert cleared.json()["provider"] is None
    assert not any(name.startswith("openai_compatible.") for name in sources)
    assert update_settings("model", {"provider": "openai"}, {}, source="env")["values"]["provider"] is None

    _update_settings("model", {"sources": {**sources, "openai_compatible.base_url": "app",
                                           "openai.models.removed_role": "env"}}, {})
    exported = client.get("/local/settings/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        settings = archive.read("data/local-settings.json")
    assert b"synthetic-export-regression-key" not in settings
    payload = json.loads(settings)
    assert payload["secrets"] is None
    exported_sources = payload["values"]["model"]["sources"]
    assert exported_sources["provider"] == "app"
    assert exported_sources["openai.models.chat"] == "app"
    assert exported_sources["openai.base_url"] == "app"
    assert not {"openai_compatible.base_url", "openai.models.removed_role", "openai.api_key"} & exported_sources.keys()
    payload["values"]["model"]["sources"]["openai.models.removed_role"] = "env"
    with pytest.raises(ValueError):
        _check_public_settings(payload)


def test_local_ui_config_tracks_owner_routine_and_background_switches(audited_venture):
    client, _ = audited_venture
    for capability, flag in (("routines", "routines_v1"), ("background_work", "inbox_briefing_v1")):
        path = f"/local/capabilities/{capability}"
        assert client.put(path, json={"on": True}).status_code == 200
        config = client.get("/ui-config")
        assert config.status_code == 200 and config.json().get(flag) is True
        if flag == "inbox_briefing_v1":
            assert config.json()["inbox_briefing"]["mailboxes"] == []
        assert client.put(path, json={"on": False}).status_code == 200
        assert flag not in client.get("/ui-config").json()


def test_local_connection_test_without_provider_names_setup_screen(audited_venture):
    client, venture = audited_venture
    response = client.post(f"/workspaces/{venture['workspace_id']}/llm-settings/test")
    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["code"] == "invalid_llm_settings"
    assert "Keys and connections" in error["hint"] and "chat model" in error["hint"]


def test_local_unknown_stage_is_validation_error_not_transition_error(audited_venture):
    client, venture = audited_venture
    path = f"/ventures/{venture['id']}/stage"
    response = client.post(path, json={"stage": "retired-stage"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "stage"]
    assert client.post(path, json={"stage": "live"}).status_code == 409
    assert client.post(path, json={"stage": "paused"}).json()["stage"] == "paused"


@pytest.mark.parametrize("domain", ["inventory planning for shops", "weekend walks for families"])
def test_local_onboarding_without_a_run_is_idle(local_owner_app, domain):
    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201
        path = f"/ventures/{created.json()['id']}/onboarding"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "idle", "steps": [], "current_step": None}
        assert client.get("/ventures/missing/onboarding").status_code == 404


@pytest.mark.parametrize("domain", ["inventory planning for shops", "weekend walks for families"])
def test_local_payment_paths_offer_spending_settings_without_payment_routes(local_owner_app, monkeypatch, domain):
    import os
    from importlib import import_module, util

    import boto3

    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    aws = MagicMock(side_effect=AssertionError("Local payment guidance must not use AWS"))
    payments = MagicMock(side_effect=AssertionError("Local payment guidance must not create a provider"))
    monkeypatch.setattr(boto3, "client", aws)
    for provider, name in (("razorpay", "RazorpayProvider"), ("stripe", "StripeProvider")):
        module = f"app.payments.{provider}"
        if util.find_spec(module):
            monkeypatch.setattr(getattr(import_module(module), name), "__init__", payments)
    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201
        venture = f"/ventures/{created.json()['id']}"
        paths = ["/billing/checkout", venture + "/credits/topup", venture + "/credits/request",
                 venture + "/billing/stripe/connect", venture + "/connect/stripe"]
        for path in paths:
            rejected = client.post(path, json={})
            assert rejected.status_code == 404
            assert "Keys and connections > Your monthly spend cap" in rejected.json()["detail"]
        assert client.get(venture + "/connect/stripe").status_code == 404
        assert client.get("/entitlement").json()["mode"] == "local"
        schema = local_owner_app.openapi()["paths"]
        assert not any(path.startswith("/billing/checkout") or path.endswith(("/credits/topup", "/billing/stripe/connect")) for path in schema)
    aws.assert_not_called()
    payments.assert_not_called()


@pytest.mark.parametrize("domain", ["inventory planning for shops", "weekend walks for families"])
def test_local_device_orders_need_no_device_but_cannot_start_work(local_owner_app, domain):
    from app.db.models import Person, Venture, Workspace
    from app.state import db
    from core.clock import utcnow

    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201
        path = f"/ventures/{created.json()['id']}/device-work-orders"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"orders": []}
        pause = client.get(f"/ventures/{created.json()['id']}/device-desktop-pause")
        assert pause.status_code == 200
        assert pause.json()["execution_available"] is False
        assert pause.json()["unavailability_code"] == "device_desktop_execution_unavailable"
        assert client.get(path + "/unavailable").status_code == 404
        refused = client.post(path + "/from-chat", json={"instruction": domain})
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "device_chat_intent_unsupported"
        now = utcnow()
        with db()() as session:
            session.add(Person(id="per_foreign", display_name="Other", created_at=now))
            session.flush()
            session.add(Workspace(id="wsp_foreign", kind="company", name=domain, owner_id="per_foreign",
                                  entry_mode="existing_company", created_at=now, updated_at=now))
            session.flush()
            session.add(Venture(id="ven_foreign", workspace_id="wsp_foreign", name=domain,
                                raw_idea=domain, stage="idea", created_at=now, updated_at=now))
            session.commit()
        for suffix in ("device-work-orders", "device-desktop-pause", "onboarding", "clarification", "clarify/questions", "product/brief", "gstack/plan"):
            assert client.get(f"/ventures/ven_foreign/{suffix}").status_code == 404
            assert client.get(f"/ventures/missing/{suffix}").status_code == 404


@pytest.mark.parametrize("domain", ["inventory planning for shops", "weekend walks for families"])
def test_local_venture_page_load_reads_are_available(local_owner_app, domain):
    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201
        venture = created.json()
        base = f"/ventures/{venture['id']}"
        paths = ["/auth/session", "/ventures?summary=true", "/workspaces", "/ui-config", "/me/greeting", base,
                 *[f"{base}/{suffix}" for suffix in ("onboarding", "device-work-orders", "device-desktop-pause",
                    "dashboard", "chat", "chat/conversations", "chat/work", "chat/gtm/status", "money",
                    "org", "agents", "agents/live", "tasks", "activity", "idea/sessions", "plans", "apps",
                    "ui-pins", "ui-archive", "waitlist", "validation", "builder/choice", "idea-page")],
                 f"/workspaces/{venture['workspace_id']}/connections",
                 f"/workspaces/{venture['workspace_id']}/jobs?venture_id={venture['id']}&active_only=true&limit=200",
                 f"/api/v1/connections/status?workspace_id={venture['workspace_id']}"]
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)


@pytest.mark.parametrize("domain", ["Inventory planning for wholesalers", "Weekend hikes for families"])
def test_local_sample_email_guard_precedes_capability_setup(local_owner_app, monkeypatch, domain):
    from app.chat.execute import local_capability_reply
    from app.core import local_capabilities
    from app.db.models import Job
    from app.state import db
    from sqlalchemy import func, select

    def forbidden_setup(*args, **kwargs):
        raise AssertionError("Your sample email must stay blocked before capability setup")

    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.post("/local/providers/sample", json={}).status_code == 200
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201 and created.json()["sample"] is True
        with db()() as session:
            jobs_before = session.scalar(select(func.count()).select_from(Job))
        monkeypatch.setattr(local_capabilities, "capability", forbidden_setup)
        monkeypatch.setattr(local_capabilities, "enabled", forbidden_setup)
        reply = local_capability_reply("send_email")
        assert "Your sample email cannot be sent" in reply["body_md"]
        assert "Settings > Keys and connections > Your AI provider" in reply["body_md"]
        assert "create a real venture" in reply["body_md"]
        assert reply["action_started"] is False
        with db()() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == jobs_before


@pytest.mark.parametrize("domain", ["warehouse planning", "family gardening"])
def test_local_empty_optional_panels_have_successful_reads(local_owner_app, domain):
    from sqlalchemy import func, select
    from app.db.models import Job
    from app.state import db

    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201
        base = f"/ventures/{created.json()['id']}"
        with db()() as session:
            jobs_before = session.scalar(select(func.count()).select_from(Job))
        for suffix in ("clarification", "clarify/questions", "product/brief", "gstack/plan"):
            response = client.get(f"{base}/{suffix}")
            assert response.status_code == 200, suffix
            if suffix in {"product/brief", "gstack/plan"}:
                assert response.json()["brief" if suffix == "product/brief" else "plan"] is None
                assert client.post(f"{base}/{suffix}", json={"action": "accept"}).status_code == 404
            else:
                assert response.json()["questions"] == []
            assert client.get(f"/ventures/missing/{suffix}").status_code == 404
        with db()() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == jobs_before


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_pagebuilder_setup_error_names_your_saved_setting(local_owner_app, monkeypatch, domain):
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings
    from app.pagebuilder.tool import CONNECT_STEP
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert created.status_code == 201
        path = f"/ventures/{created.json()['id']}/pagebuilder/profile"
        preview = client.get(path)
        assert preview.status_code == 200
        assert preview.json()["current"] is None and preview.json()["quote"] is None
        missing = client.post(path.replace("/profile", "/build"), json={})
        assert missing.status_code == 409
        assert missing.json()["detail"]["code"] == "pagebuilder_coding_tool_required"
        assert missing.json()["detail"]["message"] == CONNECT_STEP
        assert "Settings > Keys and connections > Coding tools" in missing.json()["detail"]["message"]
        assert client.post("/local/providers/sample", json={}).status_code == 200
        assert client.get(path).status_code == 409
        assert client.post(path.replace("/profile", "/build"), json={}).status_code == 409
        sample = client.post("/ventures", json={"raw_idea": domain + " sample", "start": False})
        assert sample.status_code == 201
        path = f"/ventures/{sample.json()['id']}/pagebuilder/profile"
        assert client.get(path).status_code == 200
        configured = client.post(path.replace("/profile", "/build"), json={})
        assert configured.status_code == 409
        assert configured.json()["detail"]["code"] == "pagebuilder_coding_tool_required"
        assert configured.json()["detail"]["message"] == CONNECT_STEP
        update_settings("capabilities", {"landing_pages": False}, {})
        assert client.get(path).status_code == 200
        disabled = client.post(path.replace("/profile", "/build"), json={})
        assert disabled.status_code == 409
        assert disabled.json()["detail"]["message"] == blocked_message("landing_pages")


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_setup_status_is_mounted_without_secrets(local_owner_app, monkeypatch, domain):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        response = client.get("/setup/status")
    assert response.status_code == 200
    assert response.json() == {"mode": "local", "llm_mode": "replay", "email_mode": "log"}
    assert response.headers["cache-control"] == "no-store"
    assert domain not in response.text


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_missing_mail_gives_settings_next_action(local_owner_app, monkeypatch, domain):
    for name in ("SUPYMEM_SMTP_HOST", "SUPYMEM_SMTP_USERNAME", "SUPYMEM_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        venture = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert venture.status_code == 201
        client.headers["X-Workspace-Id"] = venture.json()["workspace_id"]
        response = client.get("/api/v1/connections/gmail/status")
    assert response.status_code == 200
    assert "Settings" in response.json()["next_step"]
    assert "configure" in response.json()["next_step"].lower()


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_owner_without_signin_preserves_tenant_checks(local_owner_app, monkeypatch, domain):
    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from core.clock import utcnow
    from sqlalchemy import func, select

    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        with db()() as database:
            assert database.scalar(select(func.count()).select_from(Person)) == 1
        assert not client.cookies
        owner_request = client.get("/_local_probe", params={"domain": domain})
        assert owner_request.status_code == 200
        assert owner_request.json() == {"subject": LOCAL_PERSON_ID, "role": "user", "header": LOCAL_PERSON_ID,
                                        "account": LOCAL_PERSON_ID, "surface": LOCAL_PERSON_ID}
        assert "set-cookie" not in owner_request.headers
        session = client.get("/auth/session")
        assert session.status_code == 200
        assert session.json()["authenticated"] is True
        assert session.json()["subject"] == LOCAL_PERSON_ID
        assert session.json()["mode"] == "local"
        assert "set-cookie" not in session.headers
        probe = client.get("/_local_probe", headers={
            "X-Subject": f"you@{domain}", "X-COS-Assertion": "invalid",
            "Authorization": "Bearer invalid", "Cookie": "cos_session=invalid",
        })
        assert probe.status_code == 200
        assert probe.json() == {"subject": LOCAL_PERSON_ID, "role": "user", "header": LOCAL_PERSON_ID,
                                "account": LOCAL_PERSON_ID, "surface": LOCAL_PERSON_ID}
        created = client.post("/workspaces", json={"kind": "company", "name": domain})
        assert created.status_code == 201
        owned = created.json()["id"]
        assert client.get(f"/workspaces/{owned}").status_code == 200
        assert client.get(f"/api/v1/workspaces/{owned}/status").status_code == 200
        now = utcnow()
        with db()() as database:
            database.add(Person(id="per_other", display_name="Other", created_at=now))
            database.flush()
            database.add(Workspace(id="wsp_other", kind="company", name=domain, entry_mode="existing_company", owner_id="per_other",
                                   created_at=now, updated_at=now))
            database.flush()
            database.add(Venture(id="ven_other", workspace_id="wsp_other", name=domain,
                                 raw_idea=domain, stage="idea", created_at=now, updated_at=now))
            database.commit()
        assert client.get("/workspaces/wsp_other", headers={"X-Subject": "per_other"}).status_code == 404
        assert client.get("/ventures/ven_other").status_code == 404
        assert client.get("/api/v1/workspaces/wsp_other/status").status_code == 403
        with db()() as database:
            database.add(WorkspaceMember(id="wsm_viewer", workspace_id="wsp_other", person_id=LOCAL_PERSON_ID,
                                         role="viewer", created_at=now))
            database.commit()
        assert client.get("/workspaces/wsp_other").status_code == 200
        assert client.patch("/workspaces/wsp_other", json={"name": "Changed"}).status_code == 403
        with db()() as database:
            database.get(Workspace, owned).settings_json = '{"blocked": true}'
            database.commit()
        assert client.get(f"/workspaces/{owned}").status_code == 423
        monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
        assert client.get("/_local_probe").status_code == 200
        assert client.get("/auth/session").json()["subject"] == LOCAL_PERSON_ID
        with db()() as database:
            assert database.scalar(select(func.count()).select_from(Person).where(Person.id == LOCAL_PERSON_ID)) == 1
        refused = client.post("/auth/cognito/start")
        assert refused.status_code == 409
        assert "authorize_url" not in refused.json()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_app_signs_you_in_without_manual_middleware(local_app, domain):
    from app.main import create_app
    from app.workspaces.resolve import LOCAL_PERSON_ID

    application = create_app()
    with _owner_client(application, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        response = client.get("/auth/session")
        assert response.status_code == 200
        assert response.json()["subject"] == LOCAL_PERSON_ID
        venture = client.post("/ventures", json={"raw_idea": domain, "start": False})
        assert venture.status_code == 201
        assert venture.json()["raw_idea"] == domain


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_proxy_keeps_the_original_host_boundary(local_owner_app, domain):
    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        for forwarded_host in (domain, f"localhost@{domain}", f"{domain}/private", "", "localhost:invalid"):
            response = client.get("/auth/session", headers={"X-Forwarded-Host": forwarded_host})
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "local_origin_rejected"
        response = client.get("/auth/session", headers={"X-Forwarded-Host": "localhost:3100"})
        assert response.status_code == 200
        assert response.json()["authenticated"] is True


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_console_proxy_authenticates_without_opening_the_network(local_owner_app, monkeypatch, tmp_path, domain):
    import secrets
    from app.workspaces.resolve import LOCAL_PERSON_ID

    secret = secrets.token_hex(32)
    secret_file = tmp_path / "proxy.key"
    secret_file.write_text(secret)
    secret_file.chmod(0o600)
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://127.0.0.1:3100")
    headers = {"Host": "core:3300", "X-Forwarded-Host": "127.0.0.1:3100", "Origin": "http://127.0.0.1:3100",
               "X-Local-Proxy-Secret": secret, "X-Subject": f"other@{domain}"}
    with _owner_client(local_owner_app, base_url="http://core:3300", client=("172.20.0.3", 50000)) as client:
        response = client.get("/auth/session", headers=headers)
        assert response.status_code == 200
        assert response.json()["subject"] == LOCAL_PERSON_ID
        assert not client.cookies
        assert "x-local-proxy-secret" not in response.headers
        for changes in [{"X-Local-Proxy-Secret": "invalid"}, {"X-Local-Proxy-Secret": ""},
                        {"Origin": f"https://{domain}"}, {"X-Forwarded-Host": domain}, {"X-Forwarded-Host": ""}]:
            rejected = client.get("/auth/session", headers={**headers, **changes, "X-Forwarded-For": "127.0.0.1"})
            assert rejected.status_code == 403
        monkeypatch.delenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE")
        assert client.get("/auth/session", headers=headers).status_code == 503
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/auth/session").status_code == 200


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_proxy_preserves_browser_checks_before_owner_and_effects(local_owner_app, monkeypatch, tmp_path, domain):
    import secrets
    from app.core import local_owner

    secret = secrets.token_hex(32)
    secret_file = tmp_path / "proxy.key"
    secret_file.write_text(secret)
    secret_file.chmod(0o600)
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(secret_file))
    effects = []

    @local_owner_app.post("/_local_proxy_mutation")
    def mutate():
        effects.append(domain)
        return {"ok": True}

    headers = {"Host": "core:3300", "X-Forwarded-Host": "localhost:3100",
               "Origin": "http://localhost:3100", "X-Local-Proxy-Secret": secret}
    with _owner_client(local_owner_app, base_url="http://core:3300", client=("172.20.0.3", 50000)) as client:
        assert client.post("/_local_proxy_mutation", headers=headers).status_code == 200
        with client.websocket_connect("ws://core:3300/_local_socket", headers=headers) as connection:
            assert connection.receive_json()["subject"]

        def unexpected_owner():
            pytest.fail("Rejected proxy requests must not create an owner")

        monkeypatch.setattr(local_owner, "ensure_owner", unexpected_owner)
        for changes in ({"Sec-Fetch-Site": "cross-site"}, {"Referer": f"https://{domain}/form"},
                        {"Origin": "null"}, {"X-Forwarded-Host": domain}, {"Host": "core:3300\x7f"},
                        {"Origin": "", "Content-Type": "text/plain"}):
            assert client.post("/_local_proxy_mutation", content=domain, headers={**headers, **changes}).status_code == 403
        originless = {name: value for name, value in headers.items() if name != "Origin"}
        assert client.post("/_local_proxy_mutation", content=domain,
                           headers={**originless, "Content-Type": "text/plain"}).status_code == 403
        for name, value in (("Host", domain), ("X-Forwarded-Host", domain),
                            ("Origin", f"https://{domain}"), ("X-Local-Proxy-Secret", secret)):
            assert client.post("/_local_proxy_mutation", headers=[*headers.items(), (name, value)]).status_code == 403
        assert effects == [domain]


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_console_proxy_secret_is_generated_once(local_owner_app, monkeypatch, tmp_path, domain):
    import os
    import re
    import subprocess
    import sys
    import yaml

    root = Path(__file__).resolve().parents[2]
    secret_file = tmp_path / domain / "proxy.key"
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(secret_file))
    subprocess.run(["sh", str(root / "docker/entrypoint.sh"), sys.executable, "-c", "pass"], check=True,
                   env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"})
    assert secret_file.is_file()
    assert secret_file.stat().st_mode & 0o777 == 0o600
    for filename, core_name in [("docker-compose.local.yml", "core"), ("docker-compose.companyos.yml", "company_os")]:
        if filename == "docker-compose.companyos.yml" and not (root / filename).is_file():
            continue
        services = yaml.safe_load((root / filename).read_text())["services"]
        console = services["console"]
        secret_path = console["environment"]["SUPYMEM_LOCAL_PROXY_SECRET_FILE"]
        assert services[core_name]["environment"]["SUPYMEM_LOCAL_PROXY_SECRET_FILE"] == secret_path
        assert any(mount.endswith(":/run/local-proxy:ro") for mount in console["volumes"])
        assert any(mount.endswith((":/run/local-proxy", ":/run/local-proxy:rw")) for mount in services[core_name]["volumes"])
        if filename == "docker-compose.local.yml":
            assert "local-proxy:/run/local-proxy:rw" in services[core_name]["volumes"]
        assert "SUPYMEM_MODE" in console["environment"]
        assert "SUPYMEM_MODE" in console["build"]["args"]
        assert "SUPYMEM_LOCAL_PROXY_SECRET_FILE" not in console["build"]["args"]
    core_uid = re.search(r"useradd --system --uid (\d+)", (root / "services/company_os/Dockerfile").read_text())[1]
    console_uid = re.search(r"useradd --system --uid (\d+)", (root / "apps/console/Dockerfile").read_text())[1]
    assert core_uid == console_uid
    secret = secret_file.read_bytes()
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/auth/session").status_code == 200
    assert len(secret) >= 32
    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert list(secret_file.parent.iterdir()) == [secret_file]
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/auth/session").status_code == 200
    assert secret_file.read_bytes() == secret


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_browser_boundary_rejects_csrf_and_ambiguous_headers(local_owner_app, domain):
    effects = []

    @local_owner_app.post("/_local_mutation")
    def mutate():
        effects.append(domain)
        return {"ok": True}

    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        for headers in (
            {"Sec-Fetch-Site": "cross-site"},
            {"Referer": f"https://{domain}/form"},
            {"Sec-Fetch-Site": "same-site", "Referer": f"http://{domain}/form"},
        ):
            assert client.post("/_local_mutation", headers=headers).status_code == 403
            assert client.get("/auth/session", headers=headers).status_code == 403
        assert effects == []
        for headers in ({}, {"Origin": "http://localhost:3100", "Sec-Fetch-Site": "same-origin"},
                        {"Authorization": f"Bearer {domain}", "Content-Type": "multipart/form-data; boundary=fixture"}):
            assert client.post("/_local_mutation", json={"domain": domain}, headers=headers).status_code == 200
        assert effects == [domain, domain, domain]
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            for authorization in (None, "Basic fixture"):
                headers = {"Content-Type": content_type}
                if authorization is not None:
                    headers["Authorization"] = authorization
                assert client.post("/_local_mutation", content=domain, headers=headers).status_code == 403
        assert effects == [domain, domain, domain]

    from app.core.local_owner import check_request
    from starlette.requests import HTTPConnection

    for headers in (
        [(b"host", b"local\thost")],
        [(b"host", b"localhost?")],
        [(b"host", b"localhost"), (b"origin", b"http://localhost#")],
        [(b"host", b"localhost"), (b"host", domain.encode())],
        [(b"host", b"localhost"), (b"origin", b"http://localhost"), (b"origin", f"https://{domain}".encode())],
    ):
        connection = HTTPConnection({"type": "http", "method": "POST", "scheme": "http", "path": "/",
                                     "headers": headers, "client": ("127.0.0.1", 12345)})
        with pytest.raises(HTTPException) as rejected:
            check_request(connection)
        assert rejected.value.status_code == 403


@pytest.mark.parametrize("service", ["gateway", "warden"])
@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_sibling_services_enforce_browser_boundary(monkeypatch, tmp_path, domain, service):
    import importlib
    import sys

    service_path = Path(__file__).resolve().parents[2] / "services" / service
    if not service_path.is_dir():
        pytest.skip("Legacy sibling services are not part of the public local release")
    from tests.warden_harness import purge_app_modules, restore_app_modules

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from app.core.local_owner import _ensure_proxy_secret

    _ensure_proxy_secret()
    with _owner_client(FastAPI()) as authenticated:
        owner_headers = dict(authenticated.headers)
    saved = purge_app_modules()
    previous_path = sys.path[:]
    monkeypatch.delenv("SUPYMEM_LOCAL_ALLOW_REMOTE", raising=False)
    try:
        sys.path.insert(0, str(service_path))
        application = importlib.import_module("app.main").create_app()
        with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 12345), headers=owner_headers) as client:
            for headers in ({"Host": domain}, {"Origin": f"https://{domain}"}, {"Sec-Fetch-Site": "cross-site"}):
                assert client.get("/health", headers=headers).status_code == 403
                assert client.options("/public/interest", headers=headers).status_code == 403
            assert client.get("/health").status_code != 403
    finally:
        sys.path[:] = previous_path
        restore_app_modules(saved)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_html_cannot_inherit_api_origin(local_owner_app, monkeypatch, domain):
    from fastapi.responses import HTMLResponse

    @local_owner_app.get("/_local_document")
    def document():
        return HTMLResponse(f'<a href="javascript:alert(1)">{domain}</a>')

    trusted_policy = "default-src 'none'; form-action 'self'"

    @local_owner_app.get("/_local_form")
    def form():
        return HTMLResponse('<form method="post"></form>', headers={"Content-Security-Policy": trusted_policy, "X-Content-Type-Options": "nosniff"})

    with _owner_client(local_owner_app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        response = client.get("/_local_document")
        policy = response.headers.get("Content-Security-Policy", "")
        assert "sandbox" in policy and "allow-same-origin" not in policy
        assert "allow-scripts" not in policy and "default-src 'none'" in policy
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        trusted = client.get("/_local_form")
        assert trusted.headers["Content-Security-Policy"] == trusted_policy
        assert trusted.headers.get_list("X-Content-Type-Options") == ["nosniff"]
        monkeypatch.delenv("SUPYMEM_MODE")
        assert "Content-Security-Policy" not in client.get("/_local_document").headers


@pytest.mark.parametrize("peer,host,origin,allow_remote,expected", [
    ("127.0.0.1", "localhost", None, False, 200),
    ("::1", "[::1]", "http://[::1]:3100", False, 403),
    ("::ffff:127.0.0.1", "127.0.0.1", "http://localhost:3100", False, 200),
    ("192.0.2.9", "localhost", None, False, 403),
    ("127.0.0.1", "attacker.example", None, False, 403),
    ("127.0.0.1", "localhost", "https://attacker.example", False, 403),
    ("127.0.0.1", "localhost", "null", False, 403),
    ("127.0.0.1", "localhost", "http://localhost:invalid", False, 403),
    ("testclient", "localhost", None, False, 403),
    ("192.0.2.9", "local.example", "http://local.example", True, 403),
    ("192.0.2.9", "local.example", "https://attacker.example", True, 403),
])
def test_local_owner_network_boundary(local_owner_app, monkeypatch, peer, host, origin, allow_remote, expected):
    if allow_remote:
        monkeypatch.setenv("SUPYMEM_LOCAL_ALLOW_REMOTE", "1")
    headers = {"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"}
    if origin:
        headers["Origin"] = origin
    with _owner_client(local_owner_app, base_url=f"http://{host}", client=(peer, 12345)) as client:
        response = client.get("/health", headers=headers)
        assert response.status_code == expected
        if expected == 403:
            assert "message" in response.json()["detail"]
            with pytest.raises(WebSocketDisconnect) as rejected, client.websocket_connect(f"ws://{host}/_local_socket", headers=headers):
                pytest.fail("Other machines and origins must not reach a local socket")
            assert rejected.value.code == 1008
        else:
            from app.workspaces.resolve import LOCAL_PERSON_ID

            with client.websocket_connect(f"ws://{host}/_local_socket", headers={**headers, "Origin": "http://localhost:3100"}) as connection:
                assert connection.receive_json() == {"subject": LOCAL_PERSON_ID}


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_middleware_is_inert_in_hosted_mode(monkeypatch, domain):
    from app.core import local_owner

    def unexpected_owner():
        pytest.fail("Hosted requests must not create a local owner")

    monkeypatch.setattr(local_owner, "ensure_owner", unexpected_owner)
    application = FastAPI()
    application.add_middleware(local_owner.LocalOwnerMiddleware)

    @application.get("/auth/session")
    def session(request: Request):
        return {"subject": request.headers.get("x-subject"), "authenticated": False}

    with TestClient(application, base_url=f"https://{domain}", client=("192.0.2.9", 12345)) as client:
        response = client.get("/auth/session", headers={"X-Subject": domain, "Origin": f"https://{domain}"})
        assert response.status_code == 200
        assert response.json() == {"subject": domain, "authenticated": False}


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_owner_recovery_preserves_proxy_and_keychain_startup(monkeypatch, tmp_path, domain):
    from app.core import local_owner, providers_local

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://localhost")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    secret_file = tmp_path / "proxy.secret"
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(secret_file))
    monkeypatch.delenv("SUPYMEM_LOCAL_ALLOW_REMOTE", raising=False)
    detail = {"code": "local_auth_not_ready", "message": "Restart your app and try again."}
    owner = MagicMock(side_effect=HTTPException(503, detail=detail))
    providers = MagicMock()
    monkeypatch.setattr(local_owner, "ensure_owner", owner)
    monkeypatch.setattr(providers_local, "_sample_mode", lambda: False)
    monkeypatch.setattr(providers_local, "configured_providers", providers)
    application = FastAPI()
    application.add_middleware(local_owner.LocalOwnerMiddleware)

    @application.api_route("/health", methods=["GET", "HEAD"])
    @application.api_route("/ready", methods=["GET", "HEAD"])
    def health():
        return {"ok": True}

    with _owner_client(application, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        assert secret_file.is_file()
        assert secret_file.stat().st_mode & 0o777 == 0o600
        providers.assert_not_called()
        assert owner.call_count == 1
        for path in ("/health", "/ready"):
            for method in ("GET", "HEAD"):
                assert client.request(method, path).status_code == 200
            assert client.get(f"http://{domain}{path}").status_code == 403
        assert owner.call_count == 1
        response = client.get("/auth/session")
        assert response.status_code == 503
        assert response.json() == {"detail": detail}
        with pytest.raises(WebSocketDisconnect) as rejected, client.websocket_connect(
            "ws://localhost/_local_socket", headers={"Origin": "http://localhost"},
        ):
            pytest.fail("An unavailable owner must not open a local socket")
        assert rejected.value.code == 1008
        assert rejected.value.reason == detail["message"]
        assert owner.call_count == 3
        owner.side_effect = None
        owner.return_value = SimpleNamespace(id=f"per_{domain}", email=None)
        response = client.get("/auth/session")
        assert response.status_code == 200
        assert response.json()["subject"] == owner.return_value.id
        providers.assert_not_called()


@pytest.mark.parametrize("resolver", ["account", "surface"])
def test_local_identity_fails_closed_without_middleware(monkeypatch, resolver):
    from app.consumer.session import parse_session
    from app.surfaces.auth import resolve_principal

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with pytest.raises(HTTPException) as rejected:
        (parse_session if resolver == "account" else resolve_principal)(None)
    assert rejected.value.status_code == 503
    assert rejected.value.detail["code"] == "local_auth_not_ready"


@pytest.fixture(params=["warehouse-tools.example", "trail-club.example"])
def local_app(request, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/local.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    return request.param


def test_local_analytics_never_writes(local_app, monkeypatch):
    from app.analytics import events

    database = MagicMock()
    monkeypatch.setattr(events, "db", database)
    session = MagicMock()
    assert events.emit("venture.created", props={"domain": local_app}) is None
    assert events.emit_once("venture.created", event_key="local", props={"domain": local_app}) is None
    events.emit_in_session(session, "venture.created", event_key="local", props={"domain": local_app})
    database.assert_not_called()
    assert not session.mock_calls


def test_local_usage_keeps_access_checks_without_storage(local_app, monkeypatch):
    from app.analytics import usage

    database = MagicMock()
    access = MagicMock(return_value=(None, "workspace"))
    monkeypatch.setattr(usage, "db", database)
    monkeypatch.setattr(usage, "subject_person_id", lambda actor: actor)
    monkeypatch.setattr(usage, "assert_venture_access", access)
    body = {"event": "ui.screen_view", "event_id": local_app, "props": {},
            "venture_id": local_app, "workspace_id": "workspace", "context_subject": "you"}
    result = usage.capture(body, "you")
    assert result["id"].startswith("usage_")
    assert usage.capture(body, "you") == result
    database.assert_not_called()
    access.assert_called_with(local_app, "you")
    body["workspace_id"] = "foreign"
    with pytest.raises(HTTPException) as denied:
        usage.capture(body, "you")
    assert denied.value.status_code == 404
    body["workspace_id"] = "workspace"
    body["event"] = "ui.error"
    body["props"] = {"error_order_id": "order", "error_code": "failed"}
    session = database.return_value.return_value.__enter__.return_value
    session.get.return_value = SimpleNamespace(venture_id=local_app, kind="device_work_order_v1")
    assert usage.capture(body, "you")["id"].startswith("usage_")
    session.add.assert_not_called()
    session.commit.assert_not_called()
    session.get.return_value.venture_id = "foreign"
    with pytest.raises(HTTPException) as denied:
        usage.capture(body, "you")
    assert denied.value.status_code == 404
    assert "your tasks" in str(denied.value.detail)


def test_local_errors_never_record_or_alert(local_app, monkeypatch):
    from app.analytics import error_events
    from app.error_reports import service

    database = MagicMock()
    logger = MagicMock()
    monkeypatch.setattr(service, "db", database)
    monkeypatch.setattr(service, "log", logger)
    monkeypatch.setattr(error_events, "log", logger)
    error_events.log_error_event("server", feature="chat", code="failed", venture_id=local_app)
    service.record(source="server", feature="chat", code="failed", venture_id=local_app)
    service.send_alert("new", {"fingerprint": "local", "source": "server", "feature": "chat",
                               "code": "failed", "count": 1, "window": "local"})
    database.assert_not_called()
    logger.error.assert_not_called()
    logger.log.assert_not_called()


def test_local_admin_denies_before_authentication(local_app, monkeypatch):
    from app.admin import deps, internal

    verify = MagicMock(side_effect=AssertionError("authentication must not run"))
    monkeypatch.setattr(deps, "verify_caller", verify)
    monkeypatch.setattr(internal, "verify_caller", verify)
    request = Request({"type": "http", "method": "GET", "path": "/admin",
                       "headers": [], "client": (local_app, 1)})
    for check in (deps.require_admin("audit.view"), internal._require_system):
        with pytest.raises(HTTPException) as denied:
            check(request)
        assert denied.value.status_code == 404
        assert "/local/health" in str(denied.value.detail)
    verify.assert_not_called()


def test_local_shared_learning_is_off(local_app, monkeypatch):
    from app.gtm import email_learning

    database = MagicMock()
    monkeypatch.setattr(email_learning, "db", database)
    assert not email_learning._shared_available()
    assert email_learning.learned_patterns(local_app) == []
    assert email_learning.retired_patterns(local_app) == set()
    session = MagicMock()
    session.info = {"gtm_retired_patterns": {local_app: {"first.last"}}}
    assert email_learning.retired_patterns(local_app, session=session) == set()
    assert not email_learning.globally_suppressed(session, f"you@{local_app}")
    email_learning.refresh(session, "address", local_app)
    database.assert_not_called()
    assert not session.mock_calls


def test_local_shared_library_does_not_read_or_share(local_app, monkeypatch):
    from app.lead_library import service

    database = MagicMock()
    monkeypatch.setattr(service, "db", database)
    session = MagicMock()
    contact = SimpleNamespace(email=f"you@{local_app}", source="")
    service.on_outcome(session, contact, "reply")
    service.on_suppress(session, contact.email)
    assert service.promote(session, contact, "reply") is None
    assert service.sweep() == {"suppressed": 0, "retired": 0, "promoted": 0}
    assert service.ranked(session, "workspace", "venture", {}, limit=5, offset=0) == (0, [])
    assert service._entry_for(session, contact) is None
    database.assert_not_called()
    assert not session.mock_calls


def test_local_product_beacons_never_target_hosted_services(local_app, monkeypatch):
    from app.core import local_secrets
    from app.product_analytics import beacon, service

    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {}, "sources": {}, "secrets_set": {}})
    monkeypatch.delenv("SUPYMEM_PUBLIC_URL", raising=False)
    lookup = MagicMock(return_value=None)
    monkeypatch.setattr(service, "lookup_bearer", lookup)
    app_lookup = MagicMock(return_value=None)
    monkeypatch.setattr(beacon, "_app_for_venture", app_lookup)
    from app.firstlook.waitlist import public_gateway_base
    assert beacon.public_events_url() == public_gateway_base() + "/e"
    assert beacon.script_tag(key=local_app, endpoint=f"https://{local_app}/e") == ""
    assert beacon.script_tag(key=local_app, endpoint="https://gateway.supymem.com/e") == ""
    html = f"<html><body>{local_app}</body></html>"
    assert beacon.inject_into_html(html, key=local_app) == html
    old_tag = '<script data-supymem-analytics="1">send()</script>'
    with pytest.raises(ValueError, match="Settings > Keys and connections > Coding tools"):
        beacon.inject_into_html(html + old_tag, key=None)
    assert not beacon.emit_browser_event(venture_id=local_app, event="signup")
    assert not service.ingest_public({"key": local_app, "event": "signup"})
    lookup.assert_not_called()
    app_lookup.assert_called_once_with(local_app)


def test_local_directory_relocation_preserves_history_and_rejects_escape(local_app, tmp_path):
    import sqlite3
    from app import cli_local

    root = tmp_path / "moved"
    root.mkdir()
    previous = str(tmp_path / local_app)
    assert cli_local._directory_roots(root, create=False) == [str(root)]
    assert not (root / cli_local.DIRECTORY_RECORD).exists()
    cli_local._record_roots(root, [previous])
    roots = cli_local._directory_roots(root)
    assert roots == [str(root), previous]
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE attachments(path TEXT)")
        connection.execute("CREATE TABLE app_usage_records(path TEXT)")
        original = previous + "/uploads/photo.png"
        connection.executemany("INSERT INTO attachments VALUES (?)", [(original,), ("/external/photo.png",)])
        connection.execute("INSERT INTO app_usage_records VALUES (?)", (original,))
        changes = cli_local._path_changes(connection, roots, root)
        assert len(changes) == 1
        for statement, parameters in changes:
            connection.execute(statement, parameters)
        assert connection.execute("SELECT path FROM attachments").fetchall() == [
            (str(root / "uploads/photo.png"),), ("/external/photo.png",)]
        assert connection.execute("SELECT path FROM app_usage_records").fetchone() == (original,)
        connection.execute("INSERT INTO attachments VALUES (?)", (previous + "/../escape",))
        with pytest.raises(cli_local.LocalMaintenanceError, match="leaves your data folder"):
            cli_local._path_changes(connection, roots, root)


def test_local_restore_preserves_your_settings_wrapping_key(local_app, tmp_path, monkeypatch):
    import sqlite3
    import tarfile
    from app import cli_local
    from app.core import local_secrets
    from app.core.local_secrets import get_secret, update_settings

    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {
        "key": None, "legacy": {}, "storage": "keyfile", "needs_reentry": True})
    database = tmp_path / "local.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes(body TEXT)")
        connection.execute("INSERT INTO notes VALUES (?)", (local_app,))
    update_settings("smtp", {}, {"password": local_app + " private"})
    assert (tmp_path / "local-key.bin").is_file()
    archive = cli_local._backup(tmp_path, database, tmp_path.parent / (tmp_path.name + "-backups"))
    with tarfile.open(archive) as contents:
        assert "data/local-key.bin" not in contents.getnames()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE notes SET body='later work'")
    recovery = cli_local._restore(archive, tmp_path, database)
    assert get_secret("smtp", "password") == local_app + " private"
    assert (tmp_path / "local-key.bin").stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchone() == (local_app,)
    with sqlite3.connect(recovery / "local.db") as connection:
        assert connection.execute("SELECT body FROM notes").fetchone() == ("later work",)


def test_local_shared_library_imports_are_disabled(local_app, monkeypatch):
    from app.lead_library import loader

    materialize = MagicMock(side_effect=AssertionError("shared imports must not read files"))
    monkeypatch.setattr(loader, "_materialize", materialize)
    with pytest.raises(loader.Refuse) as denied:
        loader.load(f"s3://{local_app}/contacts.jsonl", None, True)
    assert "your workspace" in str(denied.value)
    materialize.assert_not_called()


def test_local_learning_preserves_workspace_suppression_and_bounces(local_app):
    from app.db.models import GtmContact, GtmSuppression
    from app.gtm import email_learning
    from app.gtm.relationships.suppression import email_suppressed
    from app.gtm.shared_models import (
        SharedAddressValidity,
        SharedEmailOutcome,
        SharedEmailPattern,
    )
    from app.state import db
    from core.clock import utcnow
    from sqlalchemy import select

    recipient = GtmContact(id="local_contact", workspace_id="local_workspace", email=f"you@{local_app}",
                           stage_changed_at=utcnow(), created_at=utcnow(), enrichment_json="{}")
    email, workspace_id = recipient.email, recipient.workspace_id
    with db()() as session:
        session.add(GtmSuppression(id="local_stop", workspace_id=recipient.workspace_id, email=recipient.email,
                                  source="opt_out", created_at=utcnow()))
        email_learning.record(session, recipient, "send", "local")
        email_learning.suppress(session, recipient.email)
        session.commit()
        assert email_suppressed(session, recipient.workspace_id, recipient)
        recipient.bounced_at = utcnow()
        session.add(recipient)
        session.commit()
        for model in (SharedAddressValidity, SharedEmailOutcome, SharedEmailPattern):
            assert not list(session.scalars(select(model)))
    assert email_learning.candidate(email, workspace_id)["blocked"]
    assert not email_learning.candidate(email, "another_workspace")["blocked"]


def test_local_problem_reports_are_disabled(local_app, monkeypatch):
    from app.api import error_reports_api
    from app.error_reports import service

    database = MagicMock()
    monkeypatch.setattr(service, "db", database)
    with pytest.raises(HTTPException) as denied:
        error_reports_api.report_problem(error_reports_api.ProblemBody(message=local_app), x_subject="you")
    assert denied.value.status_code == 404
    assert "/local/health" in str(denied.value.detail)
    database.assert_not_called()


def test_local_health_reports_only_status(local_app, monkeypatch):
    from app import health
    from app.api import local_health
    from app.core.local_secrets import update_settings
    from app.core.providers_local import save_settings
    from app.worker import health as worker_health
    from core.health import Check

    database = MagicMock(return_value=(Check("database", True, True, "ok"),
                                      Check("schema", True, True, "ok")))
    monkeypatch.setattr(health, "_probe_database", database)
    monkeypatch.setattr(worker_health, "healthy", lambda: True)
    save_settings(provider="openai_compatible", base_url=f"https://{local_app}/v1", api_key=local_app + " private",
                  models={"chat": "configured-model"})
    update_settings("smtp", {"host": "mail.invalid", "user": f"owner@{local_app}", "sender_name": "You", "postal_address": "Your office address"}, {"password": local_app})
    update_settings("capabilities", {"send_email": True}, {})
    monkeypatch.setenv("LLM_API_KEY", local_app)
    monkeypatch.setenv("RESEND_API_KEY", local_app)
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", f"http://{local_app}")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    app = FastAPI()
    app.include_router(local_health.router)
    with _owner_client(app) as client:
        from app.core.local_capabilities import blocked_message

        update_settings("capabilities", {"send_email": False}, {})
        disabled = client.get("/local/health").json()
        assert disabled["mail"]["status"] == "disabled"
        assert disabled["mail"]["next_step"] == blocked_message("send_email")
        update_settings("capabilities", {"send_email": True}, {})
        response = client.get("/local/health")
        assert response.status_code == 200
        body = response.json()
        assert body["database"]["ok"] and body["worker"]["ok"]
        assert body["provider_keys"]["model"] is True
        assert body["provider_keys"]["mail"] is True
        assert body["reader"]["status"] == "configured"
        assert body["mail"]["status"] == "configured"
        assert body["provider_keys"]["search"] is True
        assert local_app not in response.text
        assert response.headers["cache-control"] == "no-store"
        monkeypatch.delenv("LLM_API_KEY")
        assert client.get("/local/health").json()["provider_keys"]["model"] is True
        monkeypatch.setenv("EMAIL_PROVIDER", "resend")
        assert client.get("/local/health").json()["mail"]["status"] == "configured"
        monkeypatch.delenv("RESEND_API_KEY")
        assert client.get("/local/health").json()["mail"]["status"] == "configured"
        monkeypatch.setenv("EMAIL_PROVIDER", "ses")
        monkeypatch.setenv("AWS_PROFILE", local_app)
        assert client.get("/local/health").json()["mail"]["status"] == "configured"
        database.return_value = (Check("database", False, True, "connection failed"),
                                 Check("schema", False, True, "not checked"))
        monkeypatch.setattr(worker_health, "healthy", lambda: False)
        update_settings("smtp", {}, {"password": ""})
        update_settings("search", {"provider": "tavily"}, {})
        body = client.get("/local/health").json()
        assert not body["database"]["ok"] and not body["worker"]["ok"]
        assert body["provider_keys"]["model"] is True
        assert body["provider_keys"]["mail"] is False
        assert body["reader"]["status"] == "not_configured"
        assert "Keys and connections" in body["reader"]["next_step"]
        assert body["database"]["next_step"] and body["worker"]["next_step"]
        database.reset_mock()
        monkeypatch.delenv("SUPYMEM_MODE")
        assert client.get("/local/health").status_code == 404
        database.assert_not_called()


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
@pytest.mark.parametrize("signed", ["0", "1"])
def test_activity_socket_requires_registered_owner_and_same_origin(local_app, monkeypatch, domain, signed):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", signed)
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://localhost:3100")
    application = load_company_os_app().create_app()
    from app.core import local_owner
    from app.ops.services import activity
    from app.ops.ws import router
    from app.workspaces.resolve import LOCAL_PERSON_ID

    assert sum(item.cls is local_owner.LocalOwnerMiddleware for item in application.user_middleware) == 1
    owners = []

    def subscribe(callback, workspace_id):
        owners.append(local_owner.current_owner().id)
        return lambda: None

    send_text = WebSocket.send_text

    async def send_and_disconnect(connection, data):
        await send_text(connection, data)
        raise WebSocketDisconnect()

    monkeypatch.setattr(WebSocket, "send_text", send_and_disconnect)
    monkeypatch.setattr(activity, "subscribe", subscribe)
    monkeypatch.setattr(activity, "get_recent_activity", lambda *args, **kwargs: [{"domain": domain}])
    with _owner_client(application, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        for origin in (None, "null", f"https://{domain}", "http://localhost:8000",
                       "http://127.0.0.1", "https://localhost", "http://localhost/path"):
            headers = {"Origin": origin} if origin is not None else {}
            with pytest.raises(WebSocketDisconnect) as rejected, client.websocket_connect(
                "ws://localhost/ws/activity?workspace_id=wsp_owner", headers=headers,
            ):
                pytest.fail("A missing or different origin must not subscribe")
            assert rejected.value.code == 1008
        assert owners == []
        for _reconnect in range(2):
            with client.websocket_connect("ws://localhost/ws/activity?workspace_id=wsp_owner", headers={"Origin": "http://localhost:3100"}) as connection:
                assert connection.receive_json() == {"domain": domain}
        assert owners == [LOCAL_PERSON_ID, LOCAL_PERSON_ID]

    unprotected = FastAPI()
    unprotected.include_router(router)
    with TestClient(unprotected, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        with pytest.raises(WebSocketDisconnect) as rejected, client.websocket_connect(
            "ws://localhost/ws/activity", headers={"Origin": "http://localhost"},
        ):
            pytest.fail("A socket without its local owner must not subscribe")
        assert rejected.value.code == 1008
@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("attack", ["no_secret", "wrong_origin", "remote_no_secret", "remote_public_only"])
def test_local_owner_session_attack_boundary(monkeypatch, tmp_path, domain, attack):
    import secrets
    from types import SimpleNamespace
    from app.core import local_owner

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://127.0.0.1:3100")
    monkeypatch.setenv("SUPYMEM_LOCAL_ALLOW_REMOTE", "1")
    secret = secrets.token_hex(32)
    filename = tmp_path / "owner-session.secret"
    filename.write_text(secret)
    filename.chmod(0o600)
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(filename))
    owner = MagicMock(return_value=SimpleNamespace(id=domain, email=f"you@{domain}"))
    monkeypatch.setattr(local_owner, "ensure_owner", owner)
    application = FastAPI()
    application.add_middleware(local_owner.LocalOwnerMiddleware)

    @application.post("/local/settings/search/test")
    @application.get("/local/settings/export")
    def private():
        return {"owner": local_owner.current_owner().id}

    @application.post("/public/waitlists/{venture_id}")
    def signup(request: Request, venture_id: str):
        return {"venture_id": venture_id, "owner": getattr(request.state, "local_owner", None),
                "subject": request.headers.get("x-subject"), "assertion": request.headers.get("x-cos-assertion")}

    remote = attack.startswith("remote")
    with TestClient(application, base_url="http://127.0.0.1:3300",
                    client=("192.0.2.9" if remote else "127.0.0.1", 12345)) as client:
        client.headers.pop("X-Local-Proxy-Secret", None)
        owner.reset_mock()
        if attack == "wrong_origin":
            response = client.post("/local/settings/search/test", headers={
                "X-Local-Proxy-Secret": secret, "Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-site"})
        else:
            response = client.get("/local/settings/export")
        assert response.status_code == 403, domain
        assert response.json()["detail"]["message"]
        owner.assert_not_called()
        if attack == "remote_public_only":
            venture_id = domain.replace(".", "-")
            response = client.post(f"/public/waitlists/{venture_id}", headers={
                "Origin": f"https://{domain}", "Sec-Fetch-Site": "cross-site",
                "X-Subject": domain, "X-Cos-Assertion": "forged"}, json={"email": f"you@{domain}"})
            assert response.status_code == 200
            assert response.json() == {"venture_id": venture_id, "owner": None, "subject": None, "assertion": None}
            owner.assert_not_called()
            for method, path in [("DELETE", f"/public/waitlists/{domain}"),
                                 ("POST", f"/public/waitlists/{domain}/admin"),
                                 ("POST", "/public/arbitrary")]:
                assert client.request(method, path).status_code == 403
            assert client.get("/local/settings/export", headers={"X-Local-Proxy-Secret": secret}).status_code == 200


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_owner_session_defaults_to_private_persistent_install_file(monkeypatch, tmp_path, domain):
    import secrets
    from app.core.local_owner import _ensure_proxy_secret
    from core.local_security import _proxy_secret, owner_secret_path

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / domain))
    monkeypatch.delenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", raising=False)
    _ensure_proxy_secret()
    filename = owner_secret_path()
    first = _proxy_secret()
    assert filename.parent == tmp_path / domain
    assert filename.stat().st_mode & 0o777 == 0o600
    assert len(first) == 64
    _ensure_proxy_secret()
    unchanged = secrets.compare_digest(first, _proxy_secret())
    assert unchanged
    filename.chmod(0o644)
    with pytest.raises(HTTPException) as rejected:
        _proxy_secret()
    assert rejected.value.status_code == 503


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_public_callbacks_reach_validation_without_owner_access(local_owner_app, monkeypatch, domain):
    from app.core import local_owner
    from starlette.routing import Match

    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        monkeypatch.setattr(local_owner, "ensure_owner", lambda: pytest.fail("A public callback must not acquire your owner identity"))
        headers = {"Origin": f"https://{domain}", "Sec-Fetch-Site": "cross-site", "X-Subject": domain}
        venture_id = domain.replace(".", "-")
        routes = [("GET", f"/public/waitlists/{venture_id}", {404}),
                  ("POST", f"/public/waitlists/{venture_id}", {422}),
                  ("GET", "/public/unsubscribe", {404}),
                  ("POST", "/public/unsubscribe", {422}),
                  ("GET", "/public/email/unsubscribe", {200}),
                  ("POST", "/public/email/unsubscribe", {200}),
                  ("GET", "/api/v1/ai-gtm/gmail/callback", {400}),
                  ("GET", "/api/v1/content/oauth/linkedin/callback", {400}),
                  ("POST", "/e", {204})]
        for method, path, statuses in routes:
            scope = {"type": "http", "method": method, "path": path, "root_path": ""}
            assert any(route.matches(scope)[0] == Match.FULL for route in local_owner_app.routes), (method, path)
            response = client.request(method, path, headers=headers, **({"json": {}} if method == "POST" else {}))
            assert response.status_code in statuses, (method, path, response.status_code)
        assert client.get("/local/settings/search", headers=headers).status_code == 403


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_sse_uses_owner_credentials_without_opening_anonymous_streams(local_owner_app, monkeypatch, domain):
    from app.api import activity
    from core.local_security import OWNER_COOKIE, _proxy_secret

    def unavailable_snapshot(*_args):
        raise HTTPException(404)

    monkeypatch.setattr(activity, "_stream_snapshot", unavailable_snapshot)
    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        secret = _proxy_secret().decode("ascii")
        response = client.post("/ventures", headers={"X-Local-Proxy-Secret": secret}, json={"raw_idea": domain, "start": False})
        assert response.status_code == 201
        path = f"/ventures/{response.json()['id']}/activity/stream"
        client.headers.pop("X-Local-Proxy-Secret", None)
        assert client.get(path).status_code == 403
        for headers in ({"X-Local-Proxy-Secret": secret}, {"Cookie": f"{OWNER_COOKIE}={secret}"}):
            response = client.get(path, headers=headers)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "event: unavailable" in response.text
            assert secret not in response.text
        response = client.get(path, headers={"X-Local-Proxy-Secret": secret, "Origin": f"https://{domain}"})
        assert response.status_code == 403
