"""Your local settings must activate work without hosted accounts or secret leaks."""

import asyncio
import base64
import importlib.util
import io
import json
import secrets
import socket
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient


SCENARIOS = [
    pytest.param(("A maintenance planner for independent machine shops", "Owners of machine shops"), id="b2b"),
    pytest.param(("A nature journal for families exploring city parks", "Families exploring city parks"), id="consumer"),
]
CAPABILITIES = (
    "research", "find_customers", "send_email", "read_replies", "landing_pages", "publish_pages",
    "page_measurement", "background_work", "run_for_me_experiments", "routines", "connected_apps", "content",
)


@pytest.mark.parametrize("slot_id,section,names", [
    ("openai.api_key", "model", ("openai",)),
    ("firecrawl.api_key", "search", ("firecrawl_key",)),
    ("aws.credentials", "publishing", ("access_key_id", "secret_access_key")),
])
def test_legacy_key_forms_use_the_shared_encrypted_store(flow, slot_id, section, names):
    from app.core.local_secrets import get_secret
    from app.secrets.catalog import get_slot
    from app.secrets.service import SecretSlotError, save_secret, slot_status

    spec = get_slot(slot_id)
    values = dict(zip(spec.env_keys, flow.credentials))
    response = save_secret(slot_id, values)
    assert all(get_secret(section, name) == value for name, value in zip(names, values.values()))
    assert response["armed"] and slot_status(slot_id)["armed"]
    assert not (flow.root / ".env").exists()
    assert not any(value in json.dumps(response) for value in values.values())
    saved = (flow.root / "local-settings.json").read_bytes()
    with pytest.raises(SecretSlotError, match="Keys and connections"):
        save_secret(slot_id, {name: "too-long-key-" * 1500 for name in values})
    assert (flow.root / "local-settings.json").read_bytes() == saved
    flow.assert_private()


def test_legacy_forms_keep_unreadable_keys_and_name_the_recovery_screen(flow):
    from app.secrets.service import SecretSlotError, save_secret, slot_status

    flow.save_connections()
    path = flow.root / "local-settings.json"
    saved = json.loads(path.read_bytes())
    token = bytearray(base64.b64decode(saved["secrets"]))
    token[-1] ^= 1
    saved["secrets"] = base64.b64encode(token).decode()
    damaged = json.dumps(saved).encode()
    path.write_bytes(damaged)
    for operation in (lambda: slot_status("firecrawl.api_key"),
                      lambda: save_secret("firecrawl.api_key", {"FIRECRAWL_API_KEY": flow.credentials[0]})):
        with pytest.raises(SecretSlotError) as error:
            operation()
        assert "Keys and connections" in str(error.value) and "Your data and recovery" in str(error.value)
        assert path.read_bytes() == damaged
    flow.assert_private()


def test_native_keychain_promotion_removes_colocated_decryption_material(flow, monkeypatch):
    from app.core import local_secrets

    key = secrets.token_bytes(32)
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {"key": None, "legacy": {}, "storage": "keyfile"})
    local_secrets._atomic_write(flow.root / "local-key.bin", key)
    local_secrets.update_settings("smtp", {}, {"password": flow.credentials[0]})
    encrypted = (flow.root / "local-settings.json").read_bytes()
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {
        "key": base64.b64encode(key).decode(), "legacy": {}, "storage": "keychain"})
    assert local_secrets.get_secret("smtp", "password") == flow.credentials[0]
    assert not (flow.root / "local-key.bin").exists()
    assert (flow.root / "local-settings.json").read_bytes() == encrypted
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {"key": None, "legacy": {}, "storage": "keyfile"})
    assert local_secrets.get_secret("smtp", "password") is None
    assert (flow.root / "local-settings.json").read_bytes() == encrypted


def test_source_run_uses_available_native_wrapping_key_without_shell_pipe(flow, monkeypatch):
    import hashlib
    from app.core import local_secrets

    monkeypatch.delenv("SUPYMEM_LOCAL_KEY_PIPE", raising=False)
    key = base64.b64encode(local_secrets._key(flow.root, create=True)).decode()
    account = hashlib.sha256(str(flow.root.resolve()).encode()).hexdigest()
    monkeypatch.setattr(local_secrets.shutil, "which", lambda name: "/mock/secret-tool")
    monkeypatch.setattr(local_secrets.sys, "platform", "linux")

    def lookup(arguments, **options):
        assert arguments == ["/mock/secret-tool", "lookup", "service", "Supymem Local", "username", account]
        assert options == {"input": None, "capture_output": True, "text": True, "timeout": 2}
        return SimpleNamespace(returncode=0, stdout=key, stderr="")

    monkeypatch.setattr(local_secrets.subprocess, "run", lookup)
    local_secrets.update_settings("smtp", {}, {"password": flow.credentials[0]})
    assert local_secrets.get_secret("smtp", "password") == flow.credentials[0]
    assert not (flow.root / "local-key.bin").exists()


def test_vault_wrapping_key_migration_preserves_decryption_without_plaintext(flow, monkeypatch):
    from app.core import local_secrets, local_windows

    key = secrets.token_bytes(32)
    path = flow.root / "vault/master.key"
    path.parent.mkdir(exist_ok=True)
    local_secrets._atomic_write(path, key.hex().encode())
    wrapping = base64.b64encode(local_secrets._key(flow.root, create=True)).decode()
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {"key": wrapping, "legacy": {}, "storage": "keychain"})
    assert local_windows.credential_key(path) == key
    assert local_windows.credential_key(path) == key
    assert key.hex().encode() not in path.read_bytes()
    assert not (flow.root / "local-key.bin").exists()


@pytest.mark.parametrize("key_file", [False, True])
def test_unsigned_shell_fallback_keeps_file_storage_without_changing_keys(flow, monkeypatch, key_file):
    from app.core import local_secrets

    path = flow.root / "local-key.bin"
    key = path.read_bytes() if path.exists() else secrets.token_bytes(32)
    encoded = base64.b64encode(key).decode()
    legacy = {"vault-master-key": base64.b64encode(secrets.token_bytes(32)).decode()}
    if key_file:
        path.write_bytes(key)
        path.chmod(0o600)
    else:
        path.unlink(missing_ok=True)
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "stdin")
    monkeypatch.setattr(local_secrets, "_material_started", True)
    monkeypatch.setattr(local_secrets, "_material", {"key": encoded, "legacy": legacy, "storage": "keyfile"})
    monkeypatch.setattr(local_secrets.shutil, "which", lambda name: "/mock/secret-tool")
    monkeypatch.setattr(local_secrets.sys, "platform", "linux")
    def keychain(*args, **kwargs):
        pytest.fail("Bundled Python must not retry the shell's keychain decision")

    monkeypatch.setattr(local_secrets.subprocess, "run", keychain)
    local_secrets.update_settings("smtp", {}, {"password": flow.credentials[0]})
    assert path.read_bytes() == key
    assert local_secrets.get_secret("smtp", "password") == flow.credentials[0]
    assert local_secrets.legacy_secret("vault-master-key") == legacy["vault-master-key"]
    flow.assert_private()


def test_research_switch_blocks_customer_readiness_with_the_exact_next_step(flow):
    flow.save_connections()
    flow.request("POST", "/local/providers/sample", {})
    research = flow.request("PUT", "/local/capabilities/research", {"on": False})
    customers = flow.request("PUT", "/local/capabilities/find_customers", {"on": True})
    assert customers["state"] == "needs_setup"
    assert research["next_step"] in customers["next_step"]
    assert not flow.transports.requests


def test_legacy_frozen_receipts_accept_only_equivalent_canonical_cost(flow, scenario):
    from app.credits import gate, local_meter
    from app.credits.errors import CreditsUnavailable

    meter = gate.Meter(hold=None, passthrough=True, purpose="content.draft", local=True,
        workspace_id="owner", venture_id=scenario[0], source="byok", estimate_micro=100,
        actual_micro=30, provider_actual_micro=30, recorded=True)
    local_meter.begin(meter)
    local_meter.finish(meter)
    usage = gate.captured_receipt(meter)
    legacy = dict(usage)
    legacy.pop("provider_actual_micro")
    with local_meter._database() as connection:
        connection.execute("UPDATE calls SET receipt_json = ? WHERE call_id = ?", (json.dumps(legacy), meter.trace_id))
    assert local_meter.reconcile(usage) == 0
    for replacement in ({"actual_micro": 31, "provider_actual_micro": 31},
                        {"venture_id": "another-owner"}, {"actual_micro": True}):
        with pytest.raises(CreditsUnavailable):
            local_meter.reconcile(usage | replacement)


@pytest.mark.parametrize("name", ["CON", "aux.txt", "src/COM1.js", "LPT\u00b2.log", "src/file. ", "src/file.", "src/file ", "src/a?b", "src/file:stream"])
def test_artifact_names_reject_windows_aliases_before_writing(tmp_path, scenario, name):
    from core.artifact_policy import ArtifactRejected, extract_zip, validate_windows_name

    with pytest.raises(ArtifactRejected):
        validate_windows_name(name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, scenario[0])
    with pytest.raises(ArtifactRejected):
        extract_zip(buffer.getvalue(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_windows_artifact_transport_uses_pinned_paths_not_unix_descriptors(tmp_path, scenario, monkeypatch):
    import os
    from contextlib import contextmanager
    from core import artifact_policy

    @contextmanager
    def directory(path, *, create=False):
        if create:
            path.mkdir(parents=True, exist_ok=True)
        assert path.is_dir() and not path.is_symlink()
        yield path

    windows_os = SimpleNamespace(**{**{name: value for name, value in vars(os).items()
        if name not in {"O_DIRECTORY", "O_NOFOLLOW"}}, "name": "nt"})
    monkeypatch.setattr(artifact_policy, "os", windows_os)
    monkeypatch.setattr(artifact_policy, "_windows_directory", directory, raising=False)
    monkeypatch.setattr(artifact_policy, "_windows_file", lambda path, flags: os.open(path, flags, 0o600), raising=False)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/main.txt", scenario[0])
    result = artifact_policy.extract_zip(buffer.getvalue(), tmp_path / "out")
    assert result[0]["path"] == "src/main.txt"
    artifact_policy.replace_file(tmp_path / "out", "src/main.txt", scenario[1].encode())
    assert artifact_policy.read_file(tmp_path / "out", "src/main.txt") == scenario[1].encode()


@pytest.mark.parametrize("directory,attributes,links", [(True, 0x410, 1), (False, 0x400, 1), (False, 0x10, 1), (False, 0, 2)])
def test_windows_artifact_handles_reject_reparse_points_and_aliases(tmp_path, scenario, monkeypatch, directory, attributes, links):
    import ctypes
    import os
    from core import artifact_policy

    calls, closed = [], []

    def create(*args):
        calls.append(args)
        return 8

    def information(handle, details):
        details[0], details[10] = attributes, links
        return 1

    kernel = SimpleNamespace(CreateFileW=create, GetFileInformationByHandle=information,
        CloseHandle=lambda handle: closed.append(handle))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    with pytest.raises(artifact_policy.ArtifactRejected):
        artifact_policy._windows_handle(tmp_path / "source", os.O_RDONLY, directory=directory)
    assert closed == [8]
    assert calls[0][5] & 0x200000 and not calls[0][2] & 4


@pytest.mark.parametrize("acl_fails", [False, True])
def test_windows_restore_protects_every_file_before_publication_and_keeps_rollback(flow, monkeypatch, acl_fails):
    import os
    from app import cli_local
    from app.core import local_windows

    flow.save_connections()
    flow.close()
    from app.state import reset_db_factory

    reset_db_factory()
    archive = cli_local.backup()
    original = (flow.root / "owner.db").read_bytes()
    protected = []

    def protect(path, *, directory=False):
        if acl_fails and not directory:
            raise PermissionError("synthetic ACL failure")
        staging = next(parent for parent in path.parents if parent.name.startswith(".restore-"))
        protected.append((path.relative_to(staging), directory))
        path.chmod(0o700 if directory else 0o600)

    monkeypatch.setattr(cli_local, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(local_windows, "private_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(local_windows, "protect_private", protect, raising=False)
    if acl_fails:
        with pytest.raises(cli_local.LocalMaintenanceError, match="Keys and connections"):
            cli_local.restore(archive)
        assert (flow.root / "owner.db").read_bytes() == original
    else:
        cli_local.restore(archive)
        names = {path.as_posix() for path, _ in protected}
        assert "data/owner.db" in names and "data/local-settings.json" in names
MISSING_SETTINGS = [
    pytest.param("research", "search", "api_key", {"provider": "tavily"}, id="search-key"),
    pytest.param("research", "search", "firecrawl_url", {"provider": "firecrawl"}, id="reader-url"),
    *[pytest.param("send_email", "smtp", field, {}, id=f"send-{field}")
      for field in ("host", "user", "password", "sender_name", "postal_address")],
    *[pytest.param("read_replies", "smtp", field, {}, id=f"replies-{field}")
      for field in ("user", "password", "imap_host")],
    *[pytest.param("publish_pages", "publishing", field, {"provider": "vercel"}, id=f"publish-{field}")
      for field in ("project", "token")],
    *[pytest.param("publish_pages", "publishing", field, {"provider": "s3"}, id=f"s3-{field}")
      for field in ("bucket", "region", "prefix", "site_url", "access_key_id", "secret_access_key")],
    pytest.param("connected_apps", "composio", "api_key", {}, id="connection-key"),
    pytest.param("connected_apps", "composio", "auth_configs", {}, id="app-authorization"),
]


@pytest.fixture(params=SCENARIOS)
def scenario(request):
    return request.param


class Transports:
    def __init__(self, scenario):
        self.idea, self.audience = scenario
        self.requests = []
        self.messages = []
        self.logins = []
        self.mail_reads = []
        self.fail = None
        self.hosted = []
        self.published_files = {}
        self.url = "https://directory.example/contact"

    def record(self, method, url, *, host=None):
        host = host or urlsplit(str(url)).hostname or ""
        self.requests.append((method, host, urlsplit(str(url)).path))
        if host == "supymem.com" or host.endswith(".supymem.com"):
            self.hosted.append(host)
            raise AssertionError("Your local app must not request a hosted Supymem service.")

    def response(self, request):
        host = urlsplit("//" + request.headers.get("host", request.url.host)).hostname
        self.record(request.method, request.url, host=host)
        if self.fail == host:
            raise httpx.ConnectError("Your fake connection is unavailable.", request=request)
        if host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"title": self.audience, "url": self.url,
                "content": self.idea, "score": 0.9}]}, request=request)
        if host == "api.exa.ai":
            return httpx.Response(200, json={"results": [{"title": self.audience, "url": self.url,
                "text": self.idea}]}, request=request)
        if host == "reader.example":
            return httpx.Response(200, json={"success": True, "data": {"web": [
                {"title": self.audience, "url": self.url, "description": self.idea}],
                "markdown": self.idea}}, request=request)
        if host == "api.vercel.com":
            if request.method == "POST" and request.url.path.endswith("/deployments"):
                self.published_files.update({item["file"]: base64.b64decode(item["data"])
                    for item in json.loads(request.content)["files"]})
            return httpx.Response(200, json={"user": {"id": "owner"}, "id": "deployment",
                "url": "owner-site.vercel.app", "readyState": "READY"}, request=request)
        if host == "owner-site.vercel.app":
            return httpx.Response(200, content=self.published_files[request.url.path.lstrip("/") or "index.html"],
                headers={"Content-Type": "text/html"}, request=request)
        if host == "api.netlify.com":
            return httpx.Response(200, json={"id": "site", "url": "https://owner-site.netlify.app"}, request=request)
        if host == "backend.composio.dev":
            return httpx.Response(200, json={"items": [{"id": "owner-authorization"}]}, request=request)
        if host == "cloudflare-dns.com":
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 15, "data": "10 mx.mail.example"}]}, request=request)
        if request.url.path.startswith("/public/waitlists/") or request.url.path == "/e":
            host = request.headers.get("host") or request.url.host
            public = TestClient(self.owner.application, base_url=f"https://{host}", client=(request.url.host, 50000))
            response = public.request(request.method, request.url.path)
            return httpx.Response(response.status_code, content=response.content, headers=response.headers, request=request)
        if request.url.path.endswith("robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
        if host == "directory.example":
            return httpx.Response(200, text=f"<html><body><h1>{self.audience}</h1><p>{self.idea}</p>"
                '<p>Contact: invited@directory.example</p></body></html>',
                headers={"Content-Type": "text/html"}, request=request)
        raise AssertionError("Your flow reached an unexpected external HTTP destination.")

    def search_response(self, method, url, **kwargs):
        self.record(method, url)
        markup = (f'<div class="body"><h2>{self.audience}</h2><a href="{self.url}">{self.idea}</a></div>'
            f'<div data-type="web"><a href="{self.url}"><div class="title">{self.audience}</div></a>'
            f'<div class="snippet"><div class="content">{self.idea}</div></div></div>')
        return SimpleNamespace(status_code=200, content=markup.encode(), text=markup)

    def mail(self, incoming=False):
        boundary = self

        class Mail:
            def __init__(self, host, port, **kwargs):
                self.host = host

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, **kwargs):
                return (220, b"ready")

            def login(self, username, password):
                if boundary.fail == ("imap" if incoming else "smtp"):
                    raise OSError("Your fake mail connection is unavailable.")
                boundary.logins.append((incoming, username, password))
                return ("OK", [b"authenticated"])

            def send_message(self, message, **kwargs):
                boundary.messages.append(message)
                return {}

            def sendmail(self, sender, recipients, message):
                boundary.messages.append(message)
                return {}

            def select(self, *args, **kwargs):
                boundary.mail_reads.append("select")
                return ("OK", [b"0"])

            def uid(self, command, *args):
                boundary.mail_reads.append(command)
                return ("OK", [b""])

            def response(self, code):
                return (code, [b"1"])

        return Mail


class OwnerFlow:
    def __init__(self, application, root, transports):
        self.application = application
        self.root = root
        self.transports = transports
        self.transports.owner = self
        self.responses = []
        self.credentials = [secrets.token_hex(24) for _ in range(8)]
        self.headers = {}
        self.start()

    def start(self):
        from core.local_security import _proxy_secret

        self.client = TestClient(self.application, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
        self.client.__enter__()
        self.client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")

    def close(self):
        self.client.__exit__(None, None, None)

    def restart(self):
        from app import state
        from desktop_runtime.maintenance import _worker

        self.close()
        if state._engine is not None:
            state._engine.dispose()
        state.reset_db_factory()
        _worker()
        state.reset_mem_for_tests()
        self.start()

    def drain(self):
        from core.clock import utcnow
        from sqlalchemy import or_, select
        from app.core.jobs import ACTIVE_JOB_STATUSES
        from app.db.models import Job
        from app.state import db
        from app.worker.main import run_worker

        for _ in range(20):
            with db()() as session:
                now = utcnow()
                pending = list(session.scalars(select(Job).where(Job.status.in_(tuple(ACTIVE_JOB_STATUSES)),
                    Job.cancel_requested.is_(False),
                    or_(Job.run_after.is_(None), Job.run_after <= now),
                    or_(Job.scheduled_for.is_(None), Job.scheduled_for <= now))))
            if not pending:
                return
            asyncio.run(run_worker(once=True, stop=asyncio.Event()))
        pytest.fail("Your requested jobs did not finish in the real local worker.")

    def request(self, method, path, payload=None, *, html=False):
        self.drain()
        response = self.client.request(method, path, json=payload, headers=self.headers)
        self.responses.append(response)
        if html and response.status_code == 404 and hasattr(self, "acceptance_error"):
            raise self.acceptance_error("Open Website, build your page, then retry your export.", status=404)
        assert response.status_code < 400, f"Your {method} {path} failed with HTTP {response.status_code}."
        return response.text if html else response.json()

    def create(self, scenario):
        venture = self.request("POST", "/ventures", {"raw_idea": scenario[0], "start": False})
        self.headers["X-Workspace-Id"] = venture["workspace_id"]
        return venture

    def catalog(self):
        return {item["id"]: item for item in self.request("GET", "/local/settings/catalog")["sections"]}

    def save_connections(self, *, missing=None, overrides=None):
        smtp = {"host": "smtp.example", "port": 587, "user": "owner@mail.example", "password": self.credentials[0],
            "sender_name": "Your team", "postal_address": "410 Orchard Street, Portland, OR 97205",
            "imap_host": "imap.example", "imap_port": 993, "booking_link": "https://calendar.example/owner"}
        publishing = {"provider": "vercel", "project": "owner-project", "token": self.credentials[1],
            "bucket": "owner-bucket", "region": "us-west-2", "prefix": "site/", "site_url": "https://site.example",
            "access_key_id": self.credentials[2], "secret_access_key": self.credentials[3], "session_token": self.credentials[4],
            "waitlist_email": smtp["user"]}
        search = {"provider": "tavily", "api_key": self.credentials[5], "firecrawl_url": "https://reader.example",
            "firecrawl_key": self.credentials[6]}
        composio = {"api_key": self.credentials[7], "auth_configs": {"gmail": "owner-authorization"}}
        for section, values in (("smtp", smtp), ("publishing", publishing), ("search", search), ("composio", composio)):
            if missing and section == missing[0]:
                values.update(overrides or {})
                values.pop(missing[1], None)
            self.request("PUT", f"/local/settings/{section}", values)
        self.request("PUT", "/local/settings/data", {"backup_directory": str(self.root.parent / (self.root.name + "-backups"))})
        self.request("PUT", "/local/settings/spend", {"monthly_spend_cap": "12.50"})

    def assert_private(self):
        assert all(not any(credential in response.text for credential in self.credentials)
                   for response in self.responses), "Your saved secrets must never be returned by an API."
        assert all(not any(credential.encode() in path.read_bytes() for credential in self.credentials)
                   for path in self.root.rglob("*") if path.is_file()), "Your saved secrets must not be plaintext on disk."


@pytest.fixture
def flow(monkeypatch, tmp_path, scenario):
    import imaplib
    import smtplib
    import primp
    from tests.company_os_harness import load_company_os_app

    for name, value in {"SUPYMEM_MODE": "local", "APP_ENV": "local", "LLM_OFFLINE": "1",
        "LLM_CASSETTE_MODE": "replay", "GUARDRAILS_MODE": "deterministic_only", "EMAIL_PROVIDER": "log",
        "SUPYMEM_SINGLE_PROCESS": "0", "COS_TEST_APP_REUSE": "1", "COMPANY_OS_REQUIRE_SIGNED_CALLER": "0",
        "ADMIN_REQUIRE_TOTP": "0", "COMPANY_OS_DB_URL": f"sqlite:///{tmp_path}/owner.db",
        "COMPANY_OS_DATA_DIR": str(tmp_path), "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "SUPYMEM_LOCAL_PROXY_SECRET_FILE": str(tmp_path / "owner-session.secret"),
        "COMPANY_OS_SECRETS_ENV": str(tmp_path / ".env"),
        "CONSOLE_ORIGIN": "http://localhost:3100"}.items():
        monkeypatch.setenv(name, value)
    for name in ("SUPYMEM_DESKTOP_INSTANCE_ID", "SUPYMEM_LOCAL_ALLOW_REMOTE", "DATABASE_URL", "REDIS_URL", "RDS_PROXY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    repository = tmp_path.parent / (tmp_path.name + "-repository")
    repository.mkdir()
    boundary = Transports(scenario)

    def forbidden_socket(*args, **kwargs):
        pytest.fail("Your flow escaped the fake HTTP or mail transport and attempted a real connection.")

    async def async_http(transport, request):
        return boundary.response(request)

    monkeypatch.setattr(socket.socket, "connect", forbidden_socket)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", int(port or 443)))])
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda transport, request: boundary.response(request))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_http)
    monkeypatch.setattr(primp.Client, "request", lambda client, method, url, **kwargs: boundary.search_response(method, url, **kwargs))
    monkeypatch.setattr(smtplib, "SMTP", boundary.mail())
    monkeypatch.setattr(smtplib, "SMTP_SSL", boundary.mail())
    monkeypatch.setattr(imaplib, "IMAP4_SSL", boundary.mail(incoming=True))
    main = load_company_os_app()
    from app.core import local_env_import, providers_local
    from desktop_runtime import agent_status

    monkeypatch.setattr(agent_status, "provider_binary", lambda *_args, **_kwargs: None)

    for name in (set(local_env_import._FIELDS) - {"LLM_OFFLINE", "LLM_CASSETTE_MODE"}) | {"SUPYMEM_LLM_PROVIDER"} | {
        name for names in providers_local.PROVIDER_ENV.values() for name in names
    }:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(local_env_import, "repo_root", lambda: repository)
    owner = OwnerFlow(main.create_app(), tmp_path, boundary)
    owner.repository = repository
    try:
        yield owner
    finally:
        owner.close()
        assert not boundary.hosted, "Your local app requested a supymem.com host."


@pytest.mark.parametrize("domain", ["warehouse-dispatch.invalid", "garden-bulbs.invalid"])
def test_spend_save_preserves_locked_keychain_vault(monkeypatch, tmp_path, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.api import local_settings
    from app.core import local_env_import, local_secrets, providers_local

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(local_env_import, "_environment", dict)
    wrapping_key = base64.b64encode(secrets.token_bytes(32)).decode()
    material = {"key": wrapping_key, "storage": "keychain", "legacy": {}}
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: material)
    providers_local.save_settings(provider="openai_compatible", base_url=f"https://ai.{domain}/v1",
                                  api_key="fixture-vault-model", models={"chat": "saved-model"})
    local_secrets.update_settings("smtp", {"host": f"mail.{domain}"}, {"password": "fixture-vault-mail"})
    path = tmp_path / "local-settings.json"
    ciphertext = json.loads(path.read_text())["secrets"]
    material = {"key": None, "storage": "keyfile", "legacy": {}, "needs_reentry": True}

    application = FastAPI()
    application.include_router(local_settings.router)
    application.include_router(providers_local.router)
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        response = client.put("/local/settings/spend", json={"monthly_spend_cap": "13.00"})
        assert response.is_success
        if json.loads(path.read_text())["secrets"] != ciphertext:
            pytest.fail("Saving your spend limit changed the encrypted keys.")
        for data in ({"password": "fixture-replacement"}, {"host": f"other.{domain}"}):
            blocked = client.put("/local/settings/smtp", json=data)
            assert blocked.status_code == 422 and "Your data and recovery" in blocked.json()["detail"]
        blocked = client.put("/local/providers", json={"provider": "openai_compatible", "base_url": f"https://other.{domain}/v1"})
        assert blocked.status_code == 422 and "Your data and recovery" in blocked.json()["detail"]
        if json.loads(path.read_text())["secrets"] != ciphertext:
            pytest.fail("A blocked settings save changed the encrypted keys.")

    material = {"key": wrapping_key, "storage": "keychain", "legacy": {}}
    assert local_secrets.read_settings("spend")["values"]["monthly_spend_cap"] == "13.00"
    assert local_secrets.get_secret("smtp", "password") == "fixture-vault-mail"
    assert local_secrets.get_secret("model", "openai_compatible") == "fixture-vault-model"


def test_first_run_catalog_and_owner_are_local_without_starting_work(flow, scenario):
    """You can see every setting and your built-in reader before adding any key."""
    session = flow.request("GET", "/auth/session")
    assert session["authenticated"] and session["mode"] == "local"
    assert flow.request("GET", "/setup/status")["mode"] == "local"
    catalog = flow.catalog()
    assert {"providers", "search", "smtp", "publishing", "composio", "spend", "data"} <= catalog.keys()
    assert flow.request("GET", "/local/settings/search")["values"]["provider"] == "builtin"
    assert flow.request("GET", "/local/settings/search")["state"] == "ready"
    assert flow.request("GET", "/local/health")["reader"]["status"] == "configured"
    assert all(field.get("label") and field.get("purpose") and field.get("data_sent")
               for section in catalog.values() for field in section["fields"])
    flow.create(scenario)
    flow.drain()
    assert not flow.transports.requests and not flow.transports.messages and not flow.transports.mail_reads


def test_saved_connections_activate_read_only_probes_without_exposing_secrets(flow, scenario, caplog):
    """Saving your keys and testing sign-in sends no mail and publishes no page."""
    flow.create(scenario)
    flow.save_connections()
    for section in ("smtp", "imap", "search", "publishing", "composio"):
        result = flow.request("POST", f"/local/settings/{section}/test", {})
        assert result["ok"], f"Your saved {section} settings must reach its real transport."
    assert len(flow.transports.logins) == 2
    assert all(login[2] == flow.credentials[0] for login in flow.transports.logins)
    assert not flow.transports.messages and not flow.transports.mail_reads
    assert not any(method in {"PUT", "POST", "PATCH", "DELETE"} and host in {"api.vercel.com", "api.netlify.com"}
                   for method, host, path in flow.transports.requests)
    flow.catalog()
    for section in ("smtp", "search", "publishing", "composio", "spend", "data"):
        flow.request("GET", f"/local/settings/{section}")
    flow.request("POST", "/local/providers", {"provider": "openai", "api_key": flow.credentials[5],
        "models": {"chat": "gpt-4o-mini"}})
    flow.request("GET", "/local/providers")
    flow.assert_private()
    assert all(credential not in caplog.text for credential in flow.credentials), "Your logs exposed a saved secret."


def test_builtin_search_works_before_you_add_any_search_key(flow):
    """Your default keyless search can be tested without Firecrawl or a paid account."""
    assert flow.request("GET", "/local/settings/search")["values"]["provider"] == "builtin"
    assert flow.request("POST", "/local/settings/search/test", {})["ok"]
    assert flow.transports.requests and not flow.transports.messages
    assert flow.request("GET", "/local/spend")["month_cost_usd_micro"] == 0


@pytest.mark.parametrize("section,failed_host,fields", [
    ("smtp", "smtp", ("Outgoing mail host", "Mail app password")),
    ("imap", "imap", ("Incoming mail host", "Mail app password")),
    ("search", "api.tavily.com", ("Search API key",)),
    ("publishing", "api.vercel.com", ("Publishing token",)),
])
def test_failed_connection_probe_names_the_exact_setting_and_never_sends_work(flow, section, failed_host, fields):
    """A failed sign-in tells you which Keys and connections field to fix."""
    flow.save_connections()
    flow.transports.fail = failed_host
    result = flow.request("POST", f"/local/settings/{section}/test", {})
    assert not result["ok"] and "Keys and connections" in result["next_step"]
    assert all(field in result["next_step"] for field in fields)
    assert not flow.transports.messages
    flow.assert_private()


@pytest.mark.parametrize("desktop", [False, True], ids=["source", "desktop"])
def test_env_import_precedence_and_source_labels_preserve_your_app_saves(flow, monkeypatch, desktop):
    """Your app saves win; source environment then data .env then repo .env fill gaps."""
    from app.secrets.dotenv_file import secrets_env_path

    flow.repository.joinpath(".env").write_text("SMTP_HOST=repo-mail.example\nSMTP_PORT=2525\n", encoding="utf-8")
    data_env = secrets_env_path()
    data_env.parent.mkdir(parents=True, exist_ok=True)
    data_env.write_text("SMTP_HOST=data-mail.example\nSMTP_PASSWORD=" + flow.credentials[0] + "\n", encoding="utf-8")
    monkeypatch.setenv("SMTP_HOST", "process-mail.example")
    if desktop:
        monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "flow-test")
    imported = flow.request("GET", "/local/settings/smtp")
    assert imported["values"]["host"] == ("data-mail.example" if desktop else "process-mail.example")
    assert imported["sources"]["host"] == imported["sources"]["password"] == "env"
    assert imported["field_states"]["host"] == imported["field_states"]["password"] == "from .env"
    if not desktop:
        assert imported["values"]["port"] == 2525
    flow.request("PUT", "/local/settings/smtp", {"host": "saved-mail.example", "password": flow.credentials[1]})
    data_env.write_text("SMTP_HOST=changed-mail.example\nSMTP_PASSWORD=" + flow.credentials[2] + "\n", encoding="utf-8")
    monkeypatch.setenv("SMTP_HOST", "changed-process.example")
    saved = flow.request("GET", "/local/settings/smtp")
    assert saved["values"]["host"] == "saved-mail.example" and saved["secrets_set"]["password"]
    assert saved["sources"]["host"] == saved["sources"]["password"] == "app"
    assert saved["field_states"]["host"] == saved["field_states"]["password"] == "set"
    assert all(credential not in saved_response.text for credential in flow.credentials for saved_response in flow.responses)


@pytest.mark.parametrize("origin,path,deployment", [
    ("https://workshop-dispatch.services.ai.azure.com", "/api/projects/dispatch/openai/v1", "dispatch-chat"),
    ("https://outdoor-notes.openai.azure.com", "/openai/deployments/field-notes", "field-notes"),
])
def test_legacy_azure_urls_work_from_env_and_keep_saved_keys(flow, origin, path, deployment):
    from app.core.local_secrets import _update_settings, get_secret, read_settings

    endpoint = origin + path
    canonical = origin + "/openai/v1"
    dotenv = flow.root / ".env"
    dotenv.write_text(f"AZURE_OPENAI_ENDPOINT={endpoint}\nSUPYMEM_MODEL_CHAT={deployment}\n"
                      f"AZURE_OPENAI_API_KEY={flow.credentials[0]}\n", encoding="utf-8")
    dotenv.chmod(0o600)
    imported = flow.request("GET", "/local/providers")
    assert imported["configured"] and imported["values"]["base_url"] == canonical
    assert imported["values"]["model"] == deployment and imported["sources"]["api_key"] == "env"
    dotenv.unlink()

    stored = read_settings("model")["values"]["providers"]
    stored["azure_openai"]["base_url"] = endpoint if endpoint.endswith("/openai/v1") else endpoint + "/openai/v1"
    _update_settings("model", {"providers": stored}, {})
    upgraded = flow.request("GET", "/local/providers")
    assert upgraded["configured"] and upgraded["values"]["base_url"] == canonical
    saved = flow.request("PUT", "/local/providers", {"provider": "azure_openai", "base_url": canonical})
    assert saved["configured"] and saved["secrets_set"]["api_key"]
    kept_key = get_secret("model", "azure_openai") == flow.credentials[0]
    assert kept_key
    flow.assert_private()


@pytest.mark.parametrize("name", CAPABILITIES)
def test_every_shipped_capability_can_be_switched_on_and_off_from_your_app(flow, name, monkeypatch):
    """Your feature switches need no environment flag, policy row or admin approval."""
    flow.save_connections()
    flow.request("POST", "/local/providers/sample", {})
    off = flow.request("PUT", f"/local/capabilities/{name}", {"on": False})
    assert off["state"] == "off"
    assert "Settings > Capabilities" in off["next_step"] and off["title"] in off["next_step"]
    ready = flow.request("PUT", f"/local/capabilities/{name}", {"on": True})
    if name == "landing_pages":
        from tests.company_os_harness import fake_signed_in_codex

        assert ready["state"] == "needs_setup" and ready["missing"]
        assert "Settings > Keys and connections > Coding tools" in ready["next_step"]
        fake_signed_in_codex(flow.root, monkeypatch)
        tools = flow.request("GET", "/local-tools/coding-agents?refresh=true")["tools"]
        candidate = next(tool["candidate"] for tool in tools if tool["name"] == "codex")
        flow.request("POST", "/ui-config/coding-agents/consent", {"provider": "codex", "allow": True, "candidate": candidate})
        ready = flow.request("PUT", f"/local/capabilities/{name}", {"on": True})
    assert ready["state"] == "ready" and not ready["missing"]
    saved = {item["name"]: item for item in flow.request("GET", "/local/capabilities")["capabilities"]}
    assert saved[name] == ready
    assert not flow.transports.requests and not flow.transports.messages


@pytest.mark.parametrize("name,section,field,overrides", MISSING_SETTINGS)
def test_missing_settings_name_the_exact_catalog_screen_and_field(flow, name, section, field, overrides):
    """When a required setting is absent, your reply names the screen and exact field."""
    flow.save_connections(missing=(section, field), overrides=overrides)
    flow.request("POST", "/local/providers/sample", {})
    blocked = flow.request("PUT", f"/local/capabilities/{name}", {"on": True})
    catalog = flow.catalog()[section]
    label = next(item["label"] for item in catalog["fields"] if item["name"] == field)
    assert blocked["state"] == "needs_setup", f"Your {name} must not be ready without {label}."
    assert "Keys and connections" in blocked["next_step"]
    assert catalog["title"] in blocked["next_step"] and label in blocked["next_step"], "Your setup reply names a screen or field that does not exist."
    assert not flow.transports.requests and not flow.transports.messages


@pytest.mark.parametrize("name", ["find_customers", "run_for_me_experiments", "content"])
def test_missing_model_key_names_your_model_provider_screen_and_key_field(flow, name):
    """Your model-dependent feature names the model key you can save in your app."""
    flow.request("POST", "/local/providers", {"provider": "openai", "models": {"chat": "gpt-4o-mini"}})
    blocked = flow.request("PUT", f"/local/capabilities/{name}", {"on": True})
    provider = flow.catalog()["providers"]
    label = next(field["label"] for field in provider["fields"] if field["name"] == "api_key")
    assert blocked["state"] == "needs_setup"
    assert "Keys and connections" in blocked["next_step"] and provider["title"] in blocked["next_step"]
    assert label in blocked["next_step"]
    assert not flow.transports.requests and not flow.transports.messages


def test_monthly_cap_blocks_paid_probe_before_transport_and_can_be_raised_in_app(flow, scenario):
    """Your saved dollar limit stops paid calls before they leave your laptop."""
    flow.create(scenario)
    flow.save_connections()
    flow.request("PUT", "/local/settings/spend", {"monthly_spend_cap": "0"})
    capped = flow.request("POST", "/local/settings/search/test", {})
    assert not capped["ok"] and "Keys and connections" in capped["next_step"]
    assert "Monthly limit (USD)" in capped["next_step"] and "Your monthly spend cap" in capped["next_step"]
    assert not flow.transports.requests
    spend = flow.request("GET", "/local/spend")
    assert spend["monthly_cap_usd_micro"] == 0 and spend["month_cost_usd_micro"] == 0
    flow.request("PUT", "/local/settings/spend", {"monthly_spend_cap": "10.00"})
    assert flow.request("POST", "/local/settings/search/test", {})["ok"]
    assert flow.request("GET", "/local/spend")["monthly_cap_usd_micro"] == 10_000_000
    assert flow.transports.requests and not flow.transports.messages


def test_owner_credential_is_required_and_cannot_be_spoofed_or_returned(flow, scenario):
    """Only your app's owner credential can read keys or alter your settings."""
    venture = flow.create(scenario)
    subject = flow.request("GET", "/auth/session")["subject"]
    credential = flow.client.headers.pop("X-Local-Proxy-Secret")
    try:
        for headers in ({}, {"X-Subject": subject}, {"X-Local-Proxy-Secret": "not-your-owner"}):
            for method, path, payload in (("GET", "/local/settings/catalog", None),
                ("PUT", "/local/settings/spend", {"monthly_spend_cap": "0"}),
                ("GET", f"/ventures/{venture['id']}", None)):
                rejected = flow.client.request(method, path, headers=headers, json=payload)
                assert rejected.status_code in {401, 403}
                assert "Open your desktop app" in rejected.text or "Open your local app" in rejected.text
                assert credential not in rejected.text
    finally:
        flow.client.headers["X-Local-Proxy-Secret"] = credential
    assert flow.request("GET", "/auth/session")["subject"] == subject
    assert all(credential not in response.text for response in flow.responses)
    assert flow.root.joinpath("owner-session.secret").stat().st_mode & 0o077 == 0


def test_export_download_contains_your_venture_but_no_owner_keys(flow, scenario):
    """Your portable export contains your work, not keys or an owner credential."""
    venture = flow.create(scenario)
    flow.save_connections()
    exported = flow.client.get("/local/settings/export")
    assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "manifest.json" in archive.namelist()
        content = b"".join(archive.read(name) for name in archive.namelist())
        assert venture["id"].encode() in content
        assert all(credential.encode() not in content for credential in flow.credentials)
        public_settings = json.loads(archive.read("data/local-settings.json"))
        assert public_settings["secrets"] is None
        from app.core.local_data import _check_public_settings

        _check_public_settings(public_settings)
        assert not any(Path(name).name in {"local-key.bin", "owner-session.secret", "master.key", "secrets.bin"}
                       or Path(name).name.startswith(".env") for name in archive.namelist())
    assert not flow.transports.requests and not flow.transports.messages


def test_import_finishes_on_restart_preserving_newer_keys_and_backing_up_current_work(flow, scenario):
    """Restart restores the archived work and public settings but keeps your current keys."""
    from app.core.local_secrets import get_secret

    venture = flow.create(scenario)
    flow.save_connections()
    archived_sender = flow.request("GET", "/local/settings/smtp")["values"]["sender_name"]
    exported = flow.client.get("/local/settings/export")
    assert exported.status_code == 200
    newer = flow.create((scenario[0] + " with a second saved plan", scenario[1]))
    flow.request("PUT", "/local/settings/smtp", {"sender_name": "Your updated team", "password": flow.credentials[1]})
    imported = flow.client.post("/local/settings/import", content=exported.content, headers={"Content-Type": "application/zip"})
    assert imported.status_code == 200 and "Restart your app" in imported.json()["message"]
    assert flow.request("GET", f"/ventures/{newer['id']}")["id"] == newer["id"]
    flow.restart()
    assert flow.request("GET", f"/ventures/{venture['id']}")["id"] == venture["id"]
    assert flow.client.get(f"/ventures/{newer['id']}").status_code == 404
    restored_mail = flow.request("GET", "/local/settings/smtp")
    assert restored_mail["values"]["sender_name"] == archived_sender
    assert restored_mail["secrets_set"]["password"]
    kept_newer_key = get_secret("smtp", "password") == flow.credentials[1]
    assert kept_newer_key
    assert flow.request("GET", "/local/settings/backups")["backups"]


def test_you_can_create_and_restore_a_private_backup_from_the_data_screen(flow, scenario):
    """Your data screen can create a complete private backup without terminal commands."""
    venture = flow.create(scenario)
    flow.save_connections()
    created = flow.client.post("/local/settings/backup", json={})
    assert created.status_code < 400, "Your Data screen needs a working action to create your backup."
    backups = flow.request("GET", "/local/settings/backups")["backups"]
    assert created.json()["backup"] in backups and created.json()["message"]
    assert not any(credential in created.text for credential in flow.credentials)
    backup_folder = Path(flow.request("GET", "/local/settings/data")["values"]["backup_directory"])
    with tarfile.open(backup_folder / backups[-1]) as archive:
        assert any(member.name.endswith("local-settings.json") for member in archive)
        assert not any(Path(member.name).name == "local-key.bin" for member in archive)
    assert (backup_folder / backups[-1]).stat().st_mode & 0o077 == 0
    newer = flow.create((scenario[0] + " with another plan", scenario[1]))
    restored = flow.client.post("/local/settings/restore", json={"body": backups[-1]})
    assert restored.status_code == 200
    assert flow.request("GET", f"/ventures/{newer['id']}")["id"] == newer["id"]
    flow.restart()
    assert flow.request("GET", f"/ventures/{venture['id']}")["id"] == venture["id"]
    assert flow.client.get(f"/ventures/{newer['id']}").status_code == 404
    from app.core.local_secrets import get_secret

    assert get_secret("smtp", "password") == flow.credentials[0]
    assert get_secret("publishing", "token") == flow.credentials[1]
    assert get_secret("search", "api_key") == flow.credentials[5]
    assert get_secret("composio", "api_key") == flow.credentials[7]
    flow.assert_private()


@pytest.mark.parametrize("attack", ["traversal", "secret-file", "invalid-zip", "wrong-type", "unknown-backup"])
def test_bad_import_or_restore_never_destroys_your_current_work(flow, scenario, attack):
    """Untrusted archives and unlisted backups fail with an exact safe next step."""
    venture = flow.create(scenario)
    if attack == "unknown-backup":
        response = flow.client.post("/local/settings/restore", json={"body": "../unlisted.tar.gz"})
    else:
        content = b"not a ZIP"
        if attack in {"traversal", "secret-file"}:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("../escaped.txt" if attack == "traversal" else "data/.env",
                    "Your data must stay inside its folder and preserve your current keys.")
            content = buffer.getvalue()
        response = flow.client.post("/local/settings/import", content=content,
            headers={"Content-Type": "text/plain" if attack == "wrong-type" else "application/zip",
                     "Origin": "http://localhost:3100"})
    assert response.status_code in {413, 415, 422}
    assert any(word in response.text for word in ("Choose", "choose", "retry"))
    assert flow.request("GET", f"/ventures/{venture['id']}")["id"] == venture["id"]
    assert not flow.root.parent.joinpath("escaped.txt").exists()


def owner_journey(flow, scenario, *, missing=None, sample=True):
    path = Path(__file__).resolve().parents[3] / "scripts/local_acceptance.py"
    spec = importlib.util.spec_from_file_location("keys_flow_acceptance", path)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    flow.acceptance_error = acceptance.AcceptanceFailure
    if sample:
        flow.request("POST", "/local/providers/sample", {})
    else:
        flow.request("POST", "/local/providers", {"provider": "openai", "api_key": flow.credentials[5],
            "models": {"chat": "gpt-4o-mini"}})
    flow.save_connections(missing=missing)
    journey = acceptance.Journey(flow, SimpleNamespace(stub=True, idea=scenario[0], answer=scenario[1],
        timeout=1, output=flow.root / "private-page.html"))
    journey.create()
    if not sample:
        assert flow.request("GET", journey.path(""))["sample"] is False
    journey.answer()
    journey.skip()
    journey.confirm()
    return journey


@pytest.fixture
def journey(flow, scenario):
    return owner_journey(flow, scenario)


def test_requested_work_runs_without_confirmation_and_building_offers_coding_tool_setup(flow, journey):
    """Your customer work runs; building needs your connected coding tool, not an internal generator."""
    journey.customers()
    journey.design()
    journey.invitations()
    reply = flow.request("POST", journey.path("/chat"), {
        "message": "Build a landing page for my saved idea. Keep it private and do not send anything",
        "idempotency_key": secrets.token_hex(16)})
    flow.drain()
    assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
    assert any(action.get("href") == "/settings/local#coding-tools" for action in reply["actions"])
    assert reply.get("kind") != "confirmation" and not any(action.get("confirmation") for action in reply["actions"])
    assert not journey.args.output.exists()
    assert not flow.transports.messages
    assert not any(host in {"api.vercel.com", "api.netlify.com"} for method, host, path in flow.transports.requests)
    flow.assert_private()


def test_your_page_publishes_only_after_you_enable_publishing_and_request_it(flow, scenario):
    """Your saved hosting key publishes only the page you explicitly ask to publish."""
    from app.firstlook.service import ingest_builder_page
    from app.hosting.firstlook_publish import _ensure_app
    from app.pagebuilder.local_publish import _public_html, signup_page, waitlist_destination
    from app.product_analytics.beacon import _app_for_venture, public_events_url, script_tag
    from app.workspaces.resolve import local_person

    journey = owner_journey(flow, scenario, sample=False)
    venture = flow.request("GET", journey.path(""))
    name = venture["name"]
    _ensure_app(journey.venture, venture["workspace_id"], local_person().id)
    binding = _app_for_venture(journey.venture)
    assert binding
    measurement_url = public_events_url()
    destination = waitlist_destination(journey.venture, name)
    source = signup_page(journey.venture, name, destination).replace("</body>",
        script_tag(key=binding[1], endpoint=measurement_url) + "</body>")
    html = _public_html(source, destination["url"], measurement_url=measurement_url)
    assert ingest_builder_page(journey.venture, html)["ok"]
    journey.export()
    assert not flow.transports.published_files
    flow.request("PUT", "/local/capabilities/publish_pages", {"on": True})
    assert not flow.transports.published_files
    published = flow.request("POST", journey.path("/site/local/publish"), {})
    assert published["state"] == "live" and published["url"] == "https://owner-site.vercel.app/"
    assert flow.transports.published_files["index.html"] == html.encode()
    assert not flow.transports.messages
    flow.assert_private()


@pytest.mark.parametrize("name,message", [
    ("find_customers", "Find potential customers for my saved idea"),
    ("landing_pages", "Build a landing page for my saved idea. Keep it private"),
    ("send_email", "Send my saved email invitations now"),
    ("publish_pages", "Publish my landing page now"),
    ("research", "Research the market for my saved idea"),
])
def test_requested_work_when_switched_off_names_your_exact_feature_switch(flow, scenario, name, message):
    """An explicit request never bypasses your switch; Charlie names where to turn it on."""
    journey = owner_journey(flow, scenario, sample=False)
    blocked = flow.request("PUT", f"/local/capabilities/{name}", {"on": False})
    reply = flow.request("POST", journey.path("/chat"), {"message": message, "idempotency_key": secrets.token_hex(16)})
    flow.drain()
    assert "Settings > Capabilities" in reply.get("body_md", "") and blocked["title"] in reply["body_md"]
    assert reply.get("kind") != "confirmation" and not any(item.get("confirmation") for item in reply.get("actions", []))
    assert not flow.transports.messages


def test_saved_invitation_drafts_keep_disclosure_opt_out_and_sender_address(flow, journey):
    """Your imported, permitted contacts get compliant unsent drafts, not guessed addresses."""
    journey.design()
    journey.invitations()
    drafts = [item["draft"] for item in journey.contacts() if item.get("draft")]
    assert drafts
    address = flow.request("GET", "/local/settings/smtp")["values"]["postal_address"]
    for draft in drafts:
        assert "I have your address from my own contacts." in draft["body"]
        assert address in draft["body"]
        assert draft["state"] in {"draft", "manual"} and not draft.get("sent_at")
    assert not flow.transports.messages


@pytest.mark.parametrize("safety", ["off", "missing-password", "suppression", "send-cap"])
def test_asked_email_sending_preserves_switch_credentials_suppression_and_caps(flow, scenario, monkeypatch, safety):
    """Your request sends only eligible mail with your saved key and a bounded send cap."""
    journey = owner_journey(flow, scenario, missing=("smtp", "password") if safety == "missing-password" else None, sample=False)
    journey.design()
    preview = flow.request("POST", journey.path("/gtm/contacts/import/preview"), {
        "text": "email,country\nfirst@directory.example,US\nsecond@directory.example,US", "format": "csv"})
    imported = flow.request("POST", journey.path("/gtm/contacts/import"), {"preview_id": preview["preview_id"],
        "lawful_basis_attested": True, "lawful_basis": "consent"})
    assert len(imported["contact_ids"]) == 2
    journey.chat("Prepare email invitations for my saved customer interview experiment. Save drafts only. Do not send anything")
    assert sum(bool(item.get("draft")) for item in journey.contacts()) >= 2
    flow.request("POST", "/local/providers", {"provider": "openai", "api_key": flow.credentials[5],
        "models": {"chat": "gpt-4o-mini"}})
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    flow.request("PUT", "/local/capabilities/send_email", {"on": safety != "off"})
    contacts = journey.contacts()
    if safety == "suppression":
        for contact_id in (item["id"] for item in contacts if item.get("draft")):
            result = flow.client.patch(f"/api/v1/ai-gtm/contacts/{contact_id}", headers=flow.headers,
                json={"do_not_contact": True})
            assert result.status_code < 400, "You need a working Suppress action for an imported contact."
    if safety == "send-cap":
        settings = flow.client.put(f"/ventures/{journey.venture}/autopilot",
            headers=flow.headers, json={"allow_send": True, "send_max_per_day": 1})
        assert settings.status_code == 200
    reply = flow.request("POST", journey.path("/chat"), {"message": "Send my saved email invitations now",
        "idempotency_key": secrets.token_hex(16)})
    flow.drain()
    if safety == "off":
        assert "Settings > Capabilities" in reply.get("body_md", "") and "Send email" in reply["body_md"]
    elif safety == "missing-password":
        assert "Keys and connections" in reply.get("body_md", "") and "Mail app password" in reply["body_md"]
    elif safety == "send-cap":
        assert len(flow.transports.messages) == 1, "Your enabled, requested outreach should respect your one-email daily cap."
    if safety != "send-cap":
        assert not flow.transports.messages
