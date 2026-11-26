import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.company_os_harness import company_os_test_client, load_company_os_app


@pytest.mark.parametrize("damage", [None, "api_key", "key_ref", "secret_source", "credential_url"])
def test_public_model_settings_keep_preferences_without_credentials(local_settings, damage):
    from app.core import local_data, local_secrets
    from app.core.providers_local import save_settings

    _, root, domain = local_settings
    save_settings(provider="openai", base_url="https://api.openai.com/v1", api_key=domain + " private",
                  models={"chat": "your-model"})
    payload = json.loads((root / "local-settings.json").read_text())
    payload["secrets"] = None
    model = payload["values"]["model"]
    model["sources"] = {name: source for name, source in model["sources"].items()
                        if not name.endswith((".api_key", ".key_ref"))}
    if damage in {"api_key", "key_ref"}:
        model["providers"]["openai"][damage] = domain + " private"
    elif damage == "secret_source":
        model["sources"]["openai.api_key"] = "app"
    elif damage == "credential_url":
        model["providers"]["openai"]["base_url"] = "https://owner:private@api.example/v1"
    if damage is not None:
        with pytest.raises(ValueError):
            local_data._check_public_settings(payload)
    else:
        local_data._check_public_settings(payload)
        assert model["providers"]["openai"]["models"]["chat"] == "your-model"
        assert local_secrets.get_secret("model", "openai") == domain + " private"


def test_env_import_maps_settings_without_overriding_app_values(local_settings, monkeypatch, caplog):
    from app.core import local_env_import
    from app.core.local_secrets import get_secret, read_settings, update_settings

    _, root, domain = local_settings
    source = root / "source"
    source.mkdir()
    monkeypatch.setattr(local_env_import, "repo_root", lambda: source)
    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    app_host = domain.lower().replace(" ", "-") + ".app.example"
    update_settings("smtp", {"port": 2525, "user": "you@example.org", "imap_host": "imap.example", "imap_port": 993}, {}, source="env")
    update_settings("smtp", {"host": app_host}, {"password": domain + "-app"})
    (source / ".env").write_text("SMTP_HOST=repo.example\nSMTP_PORT=2525\nSMTP_USER=repo@example.org\nEXA_API_KEY=repo-key\n")
    (root / ".env").write_text(
        "SMTP_HOST=data.example\nSMTP_PASSWORD=env-key\nIMAP_HOST=imap.example\nIMAP_PORT=993\n"
        "SMTP_FROM=you@example.org\nSMTP_SENDER_NAME=" + domain + "\nSMTP_POSTAL_ADDRESS=Owner address\n"
        "TAVILY_API_KEY=tavily-key\nEXA_API_KEY=exa-key\nFIRECRAWL_SELF_HOSTED_URL=https://reader.example:3002\n"
        "FIRECRAWL_SELF_HOSTED_TOKEN=reader-key\nVERCEL_TOKEN=vercel-key\nNETLIFY_AUTH_TOKEN=netlify-key\n"
        "AWS_ACCESS_KEY_ID=access-key\nAWS_SECRET_ACCESS_KEY=secret-key\nAWS_SESSION_TOKEN=session-key\n"
        "AWS_REGION=eu-west-1\nAWS_S3_BUCKET=owner-pages\nSUPYMEM_PUBLIC_URL=https://pages.example\n"
        "SUPYMEM_WAITLIST_EMAIL=you@example.org\nCOMPOSIO_API_KEY=composio-key\nSUPYMEM_LOCAL_MONTHLY_CAP_USD=12.34\n"
        "OWNER_UNKNOWN_SETTING=do-not-log-this-value\nUNASSIGNED_PRIVATE_CREDENTIAL\n"
    )
    local_env_import.import_local_env()
    assert read_settings("smtp")["values"]["host"] == app_host
    assert get_secret("smtp", "password") == domain + "-app"
    assert read_settings("smtp")["values"]["port"] == 2525
    assert read_settings("smtp")["values"]["imap_host"] == "imap.example"
    assert read_settings("smtp")["values"]["user"] == "you@example.org"
    assert read_settings("smtp")["sources"]["host"] == "app"
    assert read_settings("smtp")["sources"]["port"] == "env"
    assert get_secret("search", "api_key") == "tavily-key"
    assert get_secret("search", "firecrawl_key") == "reader-key"
    assert read_settings("search")["values"]["firecrawl_url"] == "https://reader.example:3002"
    assert read_settings("publishing")["values"]["provider"] == "s3"
    assert get_secret("publishing", "token") == "netlify-key"
    assert get_secret("publishing", "session_token") == "session-key"
    assert get_secret("composio", "api_key") == "composio-key"
    assert read_settings("spend")["values"]["monthly_spend_cap"] == "12.34"
    assert "OWNER_UNKNOWN_SETTING" in caplog.text
    assert "do-not-log-this-value" not in caplog.text
    assert "UNASSIGNED_PRIVATE_CREDENTIAL" not in caplog.text
    update_settings("search", {}, {"api_key": domain + "-saved"})
    local_env_import.import_local_env()
    assert get_secret("search", "api_key") == domain + "-saved"
    update_settings("smtp", {"port": 2526}, {"password": domain + "-app"})
    with (root / ".env").open("a") as env_file:
        env_file.write("SMTP_PORT=not-a-port\n")
    local_env_import.import_local_env()
    assert read_settings("smtp")["values"]["port"] == 2526
    for path in root.rglob("*"):
        if path.is_file() and path.name != ".env":
            assert b"composio-key" not in path.read_bytes()


@pytest.mark.parametrize("app_url", ["http://127.0.0.1:3300", "http://127.0.0.42:3300", "http://localhost:3300", "http://[::1]:3300"])
def test_env_import_keeps_internal_app_url_out_of_publishing(local_settings, monkeypatch, caplog, app_url):
    from app.core import local_env_import
    from app.core.local_secrets import read_settings

    client, root, domain = local_settings
    monkeypatch.setattr(local_env_import, "repo_root", lambda: root / "missing-source")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "test-desktop")
    path = root / ".env"
    path.write_text(f"SUPYMEM_PUBLIC_URL={app_url}\n")
    local_env_import.import_local_env(strict=True)
    assert read_settings("publishing")["values"] == {}
    assert not caplog.records
    response = client.get("/local/settings/publishing")
    assert response.status_code == 200
    assert response.json()["values"]["site_url"] == ""
    assert response.json()["import_error"] is None
    public_url = "https://" + domain.lower().replace(" ", "-") + ".example"
    path.write_text(f"SUPYMEM_PUBLIC_URL={app_url}\nSUPYMEM_SITE_URL={public_url}\n")
    local_env_import.import_local_env(strict=True)
    assert read_settings("publishing")["values"]["site_url"] == public_url
    path.write_text(f"SUPYMEM_PUBLIC_URL={public_url}\n")
    local_env_import.import_local_env(strict=True)
    assert read_settings("publishing")["sources"]["site_url"] == "env"
    assert read_settings("publishing")["values"]["site_url"] == public_url

    saved_url = public_url + "/pages"
    assert client.put("/local/settings/publishing", json={"site_url": saved_url}).status_code == 200
    path.write_text(f"SUPYMEM_PUBLIC_URL={app_url}\n")
    response = client.get("/local/settings/publishing")
    assert response.status_code == 200
    assert response.json()["values"]["site_url"] == saved_url
    assert response.json()["sources"]["site_url"] == "app"
    assert response.json()["import_error"] is None


def test_imported_sender_profile_and_mail_edits_feed_compliant_invitations(local_settings, monkeypatch):
    from types import SimpleNamespace

    from app.core import local_env_import, local_mode
    from app.core.local_secrets import read_settings
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, sender_address

    client, root, domain = local_settings
    monkeypatch.setattr(local_env_import, "repo_root", lambda: root / "missing-source")
    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    monkeypatch.setattr(local_mode, "is_sample_mode", lambda: False)
    for name in ("SENDER_NAME", "SENDER_POSTAL_ADDRESS", "SMTP_SENDER_NAME", "SMTP_POSTAL_ADDRESS",
                 "SUPYMEM_SENDER_NAME", "SUPYMEM_SENDER_POSTAL_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    postal = f"{domain} mailing address\n123 Main Street, Boston, MA 02108"
    (root / ".env").write_text(f'SENDER_NAME="{domain}"\nSENDER_POSTAL_ADDRESS={json.dumps(postal)}\n')
    (root / ".env").chmod(0o600)
    session = SimpleNamespace(get=lambda *_args: None)
    contact = SimpleNamespace(id="imported-contact", email="guest@example.org",
                              enrichment_json=json.dumps({"founder_import": True}))

    response = client.get("/local/settings/smtp")
    assert response.status_code == 200
    assert response.json()["field_states"]["sender_name"] == "from .env"
    assert response.json()["field_states"]["postal_address"] == "from .env"
    assert sender_address(session, "local-owner") == postal
    body = complete_body(session, "local-owner", contact, "Would you like to share your experience?", "Old name")
    assert domain in body and postal in body and "Old name" not in body
    assert OPT_OUT in body and "I have your address from my own contacts." in body

    updated = {"sender_name": domain + " owner", "postal_address": postal + "\nSuite 2"}
    assert client.put("/local/settings/smtp", json=updated).status_code == 200
    assert read_settings("smtp")["values"]["sender_name"] == updated["sender_name"]
    assert sender_address(session, "local-owner") == updated["postal_address"]
    body = complete_body(session, "local-owner", contact, "Would you like to share your experience?", "Old name")
    assert updated["sender_name"] in body and updated["postal_address"] in body
    assert OPT_OUT in body and "I have your address from my own contacts." in body
    assert client.put("/local/settings/smtp", json={"postal_address": ""}).status_code == 200
    assert sender_address(session, "local-owner") == ""


@pytest.mark.parametrize("provider", [None, "firecrawl"])
def test_reader_env_import_keeps_builtin_search_unless_explicitly_selected(local_settings, monkeypatch, provider):
    from app.core import local_env_import
    from app.core.local_secrets import get_secret, read_settings

    _, root, domain = local_settings
    monkeypatch.setattr(local_env_import, "repo_root", lambda: root / "missing-source")
    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    settings = "FIRECRAWL_SELF_HOSTED_URL=https://reader.example:3002\nFIRECRAWL_SELF_HOSTED_TOKEN=" + domain + "-reader-key\n"
    if provider:
        settings += "SUPYMEM_SEARCH_PROVIDER=" + provider + "\n"
    (root / ".env").write_text(settings)
    local_env_import.import_local_env(strict=True)
    assert read_settings("search")["values"]["provider"] == (provider or "builtin")
    assert read_settings("search")["values"]["firecrawl_url"] == "https://reader.example:3002"
    assert get_secret("search", "firecrawl_key") == domain + "-reader-key"


@pytest.mark.parametrize("source", ["file", "process"])
def test_env_import_preserves_mail_password_whitespace(local_settings, monkeypatch, source):
    from app.core import local_env_import
    from app.core.local_secrets import get_secret, read_settings

    _, root, domain = local_settings
    monkeypatch.setattr(local_env_import, "repo_root", lambda: root / "missing-source")
    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    password = "  " + domain + "-mail-password  "
    if source == "file":
        (root / ".env").write_text("SMTP_PASSWORD=" + json.dumps(password) + "\n")
    else:
        monkeypatch.setenv("SUPYMEM_SMTP_PASSWORD", password)
    local_env_import.import_local_env(strict=True)
    matches = get_secret("smtp", "password") == password
    assert matches
    assert read_settings("smtp")["sources"]["password"] == "env"


def test_env_import_uses_the_selected_publishing_connection(local_settings, monkeypatch):
    from app.core import local_env_import
    from app.core.local_secrets import get_secret, read_settings, update_settings

    _, root, domain = local_settings
    monkeypatch.setattr(local_env_import, "repo_root", lambda: root / "missing-source")
    update_settings("publishing", {"provider": "vercel"}, {})
    (root / ".env").write_text(
        "VERCEL_TOKEN=" + domain + "-vercel-key\nVERCEL_PROJECT_NAME=your-project\n"
        "NETLIFY_AUTH_TOKEN=" + domain + "-netlify-key\nNETLIFY_SITE_ID=other-project\n"
        "COMPOSIO_API_KEY=" + domain + "-connection-key\n"
    )
    local_env_import.import_local_env()
    publishing = read_settings("publishing")
    assert publishing["values"]["provider"] == "vercel"
    assert publishing["values"]["project"] == "your-project"
    assert publishing["sources"]["provider"] == "app"
    assert publishing["sources"]["project"] == "env"
    assert get_secret("publishing", "token") == domain + "-vercel-key"
    assert get_secret("composio", "api_key") == domain + "-connection-key"
    assert get_secret("composio", "token") is None
    update_settings("publishing", {}, {"token": domain + "-saved-key"})
    assert get_secret("publishing", "token") == domain + "-saved-key"


def test_env_import_runs_in_process_and_app_saves_survive_env_removal(local_settings, monkeypatch):
    from app import main, state
    from app.core import local_env_import, local_owner
    from app.core.local_secrets import get_secret, read_settings
    from core.local_security import _proxy_secret

    _, root, domain = local_settings
    monkeypatch.setattr(local_env_import, "repo_root", lambda: root / "missing-source")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{root}/app.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(root / "memory.db"))
    monkeypatch.delenv("SUPYMEM_SINGLE_PROCESS", raising=False)
    state.reset_db_factory()
    path = root / ".env"
    path.write_text(
        f"OPENAI_API_KEY={domain}-model-key\nSUPYMEM_MODEL_CHAT=owner-model\n"
        f"TAVILY_API_KEY={domain}-search-key\nSMTP_HOST=smtp.example.org\nSMTP_PORT=587\n"
        f"SMTP_USER=owner@example.org\nSMTP_PASSWORD={domain}-mail-key\n"
    )
    path.chmod(0o600)
    with company_os_test_client(main.create_app()) as client:
        local_owner._ensure_proxy_secret()
        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        for endpoint in ("/local/providers", "/local/settings/search", "/local/settings/smtp"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert response.json()["sources"]["api_key" if endpoint != "/local/settings/smtp" else "password"] == "env"
        assert read_settings("model")["sources"]["openai.api_key"] == "env"
        assert get_secret("model", "openai") == domain + "-model-key"
        assert get_secret("search", "api_key") == domain + "-search-key"
        assert read_settings("smtp")["values"]["port"] == 587
        for endpoint, body, field in (
            ("/local/providers", {"provider": "openai", "model": "owner-model", "api_key": domain + "-model-key"}, "api_key"),
            ("/local/settings/search", {"api_key": domain + "-saved-search-key"}, "api_key"),
            ("/local/settings/smtp", {"sender_name": domain, "password": domain + "-saved-mail-key"}, "password"),
        ):
            response = client.put(endpoint, json=body)
            assert response.status_code == 200
            assert response.json()["sources"][field] == "app"
        path.write_text("")
        assert client.get("/local/providers").json()["sources"]["api_key"] == "app"
        assert client.get("/local/settings/search").json()["sources"]["api_key"] == "app"
        assert client.get("/local/settings/smtp").json()["sources"]["password"] == "app"
        assert get_secret("model", "openai") == domain + "-model-key"
        assert get_secret("search", "api_key") == domain + "-saved-search-key"
        assert get_secret("smtp", "password") == domain + "-saved-mail-key"
        assert read_settings("smtp")["values"]["port"] == 587
        for endpoint in ("/local/providers", "/local/settings/search", "/local/settings/smtp"):
            assert "-key" not in client.get(endpoint).text
    for saved in root.rglob("*"):
        if saved.is_file() and saved != path:
            assert (domain + "-model-key").encode() not in saved.read_bytes()


def test_local_model_callers_use_one_offline_resolver(local_settings, monkeypatch):
    import asyncio

    from app.core import local_mode, providers_local
    from app.chat import diagram_reply, llm_router
    from llm_gateway import client, provider_foundry

    _, _, domain = local_settings
    providers_local.save_settings(provider="openai", api_key=domain + "-model-key", models={"chat": "owner-model"})
    monkeypatch.setenv("CHAT_ROUTER_MODE", "auto")
    model = client.LLMClient()
    providers_local.save_settings(provider="openai", api_key=domain + "-replacement-key", models={"chat": "owner-model"})

    def before_transport(*arguments):
        raise RuntimeError("stopped before transport")

    monkeypatch.setattr(client, "apply_purpose_budget", before_transport)
    for offline in (False, True):
        monkeypatch.setenv("LLM_OFFLINE", "0" if offline else "1")
        monkeypatch.setattr(local_mode, "is_offline", lambda: offline)
        monkeypatch.setattr(providers_local, "is_offline", local_mode.is_offline)
        assert llm_router.mode() == ("off" if offline else "live")
        assert provider_foundry.sample_mode_enabled() is offline
        assert providers_local._sample_mode() is offline
        assert client.LLMClient().offline is offline
        assert (diagram_reply.skip_reason(domain, "short") == "offline") is offline
        with pytest.raises(RuntimeError, match="stopped before transport"):
            asyncio.run(model.chat([], purpose="env_chat_completion"))
        assert model.offline is offline
        assert model.config.api_key.get_secret_value() == domain + "-replacement-key"
    assert model._offline_override is None


def test_windows_directory_acl_preserves_data_and_rejects_foreign_owners_and_links(local_settings, monkeypatch):
    from app.core import local_windows

    _, root, domain = local_settings
    saved = root / "notes.txt"
    saved.write_text(domain)
    state = {"attributes": 0x10, "owner": True}
    updates = []

    def details(handle, information):
        information[0] = state["attributes"]
        return True

    def security(handle, kind, requested, owner, group, acl, sacl, descriptor):
        owner._obj.value = 1
        descriptor._obj.value = 2
        return 0

    def expected_owner(descriptor, owner, defaulted):
        owner._obj.value = 1
        return True

    def expected_acl(descriptor, present, acl, defaulted):
        acl._obj.value = 3
        return True

    functions = {"GetFileInformationByHandle": details, "GetSecurityInfo": security,
                 "GetSecurityDescriptorOwner": expected_owner, "GetSecurityDescriptorDacl": expected_acl,
                 "EqualSid": lambda *_: state["owner"],
                 "SetSecurityInfo": lambda *arguments: updates.append(arguments) or 0}
    monkeypatch.setattr(local_windows, "_api", lambda library, name, *args: functions[name])
    monkeypatch.setattr(local_windows, "_free", lambda _: None)
    local_windows._check_private(10, 20, directory=True)
    assert len(updates) == 1
    assert updates[0][2] == 0x80000004
    assert updates[0][5].value == 3
    state["owner"] = False
    with pytest.raises(ValueError, match="another Windows account"):
        local_windows._check_private(10, 20, directory=True)
    state["owner"] = True
    for attributes in (0x410, 0):
        state["attributes"] = attributes
        with pytest.raises(ValueError, match="link|file type"):
            local_windows._check_private(10, 20, directory=True)
    assert len(updates) == 1
    assert saved.read_text() == domain
    import os
    import sys
    from types import SimpleNamespace

    opened = []
    validated = []
    functions["CreateFileW"] = lambda path, access, *args: opened.append(access) or 10
    functions["CreateDirectoryW"] = lambda *_: True
    functions["CloseHandle"] = lambda *_: True
    monkeypatch.setattr(local_windows, "_security", lambda **options: 20)
    monkeypatch.setattr(local_windows, "_check_private", lambda *args, **options: validated.append(options))
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: flags))
    assert local_windows.private_open(saved, os.O_RDONLY) & os.O_BINARY
    local_windows.private_directory(root)
    assert opened[-1] == 0x60080
    assert validated[-1] == {"directory": True}


def test_windows_local_storage_uses_native_permissions_and_locks(local_settings, monkeypatch):
    import builtins
    import os
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    from app import config
    from app.core import local_process, local_secrets, local_windows, providers_local

    _, root, domain = local_settings
    windows_os = SimpleNamespace(**{**{name: value for name, value in vars(os).items()
                                     if name not in {"O_DIRECTORY", "O_NOFOLLOW", "fchmod", "geteuid"}}, "name": "nt"})
    calls = []
    monkeypatch.setattr(config, "os", windows_os)
    monkeypatch.setattr(local_windows, "private_directory", lambda path: calls.append(("directory", path)), raising=False)
    assert config.data_root() == root
    assert calls == [("directory", root)]
    original_import = builtins.__import__

    def windows_import(name, *args, **kwargs):
        if name == "os":
            return windows_os
        if name == "fcntl":
            raise AssertionError("Windows must not import fcntl")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as imports:
        imports.setattr(builtins, "__import__", windows_import)
        runpy.run_path(str(Path(local_process.__file__)))
    for module in (local_process, local_secrets, providers_local):
        monkeypatch.setattr(module, "os", windows_os)
    original_open = os.open
    monkeypatch.setattr(local_windows, "private_open", lambda path, flags: original_open(path, flags, 0o600))
    monkeypatch.setattr(local_windows, "lock_file", lambda stream, **options: calls.append(("lock", options)))
    monkeypatch.setattr(local_windows, "atomic_write", lambda path, content: calls.append(("write", path, content)))
    import base64

    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {
        "key": base64.b64encode(domain.encode().ljust(32, b" ")[:32]).decode(),
        "legacy": {}, "storage": "keychain"})
    with local_process.process_lock():
        pass
    with local_secrets._locked():
        assert len(local_secrets._key(root)) == 32
        local_secrets._atomic_write(root / "settings", domain.encode())
    with providers_local._locked(root):
        providers_local._write(root, {"provider": None, "providers": {}})
    assert sum(kind == "write" for kind, *_ in calls) == 3
    locks = [options for kind, options, *_ in calls if kind == "lock"]
    assert locks[0] == {"exclusive": False, "blocking": False}
    assert sum(options.get("unlock", False) for options in locks) == sum("unlock" not in options for options in locks)
    assert sum(options.get("unlock", False) for options in locks) >= 3


@pytest.mark.parametrize("available", [False, True])
def test_windows_wrapping_reference_preserves_recovery(local_settings, monkeypatch, available):
    import base64
    import os
    from types import SimpleNamespace
    from app.core import local_secrets, local_windows

    _, root, domain = local_settings
    local_secrets.update_settings("smtp", {"host": "mail.example"}, {"password": domain + " private"})
    local_secrets.read_settings("runtime")
    wrapping = (root / "local-key.bin").read_bytes()
    ciphertext = (root / "local-settings.json").read_bytes()
    reference = b"a" * 64
    (root / "local-key.bin").write_bytes(reference)
    monkeypatch.setattr(local_secrets, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    def save_private(path, content):
        path.write_bytes(content)
        path.chmod(0o600)

    monkeypatch.setattr(local_windows, "atomic_write", save_private)
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {
        "key": base64.b64encode(wrapping).decode() if available else None,
        "legacy": {}, "storage": "keyfile", "needs_reentry": not available})
    if available:
        assert local_secrets.get_secret("smtp", "password") == domain + " private"
        assert (root / "local-key.bin").read_bytes() == wrapping
        assert (root / "local-key.keychain-recovery.bin").read_bytes() == reference
    else:
        assert local_secrets.get_secret("smtp", "password") is None
        with pytest.raises(local_secrets.ConnectionSecretRequired, match="Your data and recovery"):
            local_secrets.update_settings("smtp", {}, {"password": domain + " replacement"})
        assert (root / "local-settings.json").read_bytes() == ciphertext
        assert (root / "local-key.bin").read_bytes() == reference


@pytest.mark.parametrize("available", [False, True])
def test_windows_vault_reference_is_sealed_without_losing_its_key(local_settings, monkeypatch, available):
    import base64
    import os
    from types import SimpleNamespace
    from app.core import local_secrets, local_windows
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _, root, domain = local_settings
    local_secrets.update_settings("smtp", {}, {"password": domain + " private"})
    vault = root / "vault"
    vault.mkdir(exist_ok=True)
    path = vault / "master.key"
    reference = b"b" * 64
    path.write_bytes(reference)
    path.chmod(0o600)
    inherited = os.urandom(32)
    monkeypatch.setattr(local_windows, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(local_secrets, "legacy_secret", lambda account: base64.b64encode(inherited).decode() if available else None)
    if not available:
        with pytest.raises(ValueError, match="backup"):
            local_windows.credential_key(path, create=True)
        assert path.read_bytes() == reference
    else:
        assert local_windows.credential_key(path, create=True) == inherited
        assert path.with_name("master.key.keychain-recovery").read_bytes() == reference
        sealed = path.read_bytes()
        assert AESGCM((root / "local-key.bin").read_bytes()).decrypt(sealed[:12], sealed[12:], b"vault/master.key") == inherited
        assert local_windows.credential_key(path) == inherited


def test_native_storage_notice_discloses_the_legacy_vault_key_file(local_settings, monkeypatch):
    import base64
    import os
    from app.core import local_secrets

    _, root, _domain = local_settings
    vault = root / "vault"
    vault.mkdir(exist_ok=True)
    master = os.urandom(32)
    local_secrets._atomic_write(vault / "master.key", master.hex().encode())
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {
        "key": base64.b64encode(os.urandom(32)).decode(), "legacy": {}, "storage": "keychain"})
    notice = local_secrets.storage_notice()
    assert "connected app keys" in notice and "key file in your data folder" in notice
    assert "Keep that folder private" in notice and master.hex() not in notice
    local_secrets._atomic_write(vault / "master.key", os.urandom(60))
    assert local_secrets.storage_notice() == ""


def test_local_data_directory_is_private_without_destroying_files(local_settings, monkeypatch):
    client, root, domain = local_settings
    preserved = root / "notes.txt"
    preserved.write_text(domain)
    root.chmod(0o777)
    assert client.put("/local/settings/smtp", json={"sender_name": domain}).status_code == 200
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert preserved.read_text() == domain
    assert stat.S_IMODE((root / "local-settings.json").stat().st_mode) == 0o600
    monkeypatch.delenv("SUPYMEM_MODE")
    root.chmod(0o755)
    from app.config import data_root

    assert data_root() == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.config import os

    original_fstat = os.fstat

    def foreign_owner(descriptor):
        metadata = list(original_fstat(descriptor))
        metadata[4] = os.geteuid() + 1
        return os.stat_result(metadata)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(PermissionError, match="Choose a folder you own"):
        data_root()
    assert preserved.read_text() == domain
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_local_logs_redact_custom_environment_and_saved_keys(local_settings, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    from core.logging import JsonFormatter, install_sensitive_url_filters

    client, _, domain = local_settings
    environment_key = domain + " environment credential"
    stored_key = domain + " stored credential"
    provider_key = domain + " provider credential"
    vault_key = domain + " vault credential"
    monkeypatch.setenv("LLM_API_KEY", environment_key)
    assert client.put("/local/settings/composio", json={"api_key": stored_key}).status_code == 200
    assert client.get("/local/settings/composio").status_code == 200
    from app.core.providers_local import save_settings
    from app.connectors.secrets import FileSecretStore

    provider_host = "-".join(domain.lower().split()) + ".example"
    save_settings(provider="openai_compatible", base_url=f"https://{provider_host}/v1", api_key=provider_key,
                  models={"chat": "configured-model"})
    vault = FileSecretStore()
    reference = vault.put(workspace_id="wsp_owner", provider="smtp", key="password", value=vault_key)
    assert vault.get(reference, requester="test") == vault_key
    install_sensitive_url_filters()
    caplog.clear()
    logger = logging.getLogger("local-security-review")
    with caplog.at_level(logging.WARNING):
        try:
            raise ValueError(stored_key + provider_key + vault_key)
        except ValueError:
            logger.exception("Your provider failed: %s; %s. Retry your connection.", environment_key, stored_key,
                             extra={"extra": {"credential": stored_key, "failure": ValueError(vault_key),
                                              "object": SimpleNamespace(token=provider_key)},
                                    "provider_failure": ValueError(provider_key)})
    rendered = caplog.text + JsonFormatter().format(caplog.records[-1])
    rendered += logging.Formatter("%(message)s %(extra)s %(provider_failure)s").format(caplog.records[-1])
    assert environment_key not in rendered
    assert stored_key not in rendered
    assert provider_key not in rendered
    assert vault_key not in rendered
    assert "Retry your connection" in rendered


@pytest.fixture(params=["Industrial procurement", "Family cooking"])
def local_settings(request, monkeypatch, tmp_path, isolate_app_imports):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "runtime"))
    load_company_os_app(reset=False)
    from app.core.providers_local import PROVIDER_ENV, ROLES

    for name in ("SUPYMEM_LLM_PROVIDER", *(name for fields in PROVIDER_ENV.values() for name in fields),
                 *(f"SUPYMEM_MODEL_{role.upper()}" for role in ROLES)):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "settings"
    root.mkdir()
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(root))
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(root / ".env"))
    from app.api.local_settings import router

    app = FastAPI()
    app.include_router(router)
    return company_os_test_client(app), root, request.param


@pytest.mark.parametrize("section", ["smtp", "providers"])
def test_unreadable_keys_are_named_only_after_key_storage_finishes_opening(local_settings, monkeypatch, section):
    _client, root, domain = local_settings

    from app.api.local_settings import _field_states, _view
    from app.core import local_secrets
    from app.core.providers_local import _provider_view, save_settings

    material = {"key": None, "legacy": {}, "storage": "keyfile"}
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: material)
    if section == "smtp":
        local_secrets.update_settings("smtp", {"host": "smtp.owner.test"}, {"password": domain + " private"})
        field = "password"
        view = lambda: _view("smtp", local_secrets.read_settings("smtp"))
    else:
        save_settings(provider="openai", base_url="https://api.openai.com/v1", api_key=domain + " private", models={"chat": "your-model"})
        field = "api_key"
        view = _provider_view
    assert _field_states(section, view())[field] == "set"
    saved = (root / "local-settings.json").read_bytes()
    (root / "local-key.bin").unlink()
    local_secrets._atomic_write(root / "local-key.keychain", b"1")
    assert _field_states(section, view())[field] == "unreadable"
    assert (root / "local-settings.json").read_bytes() == saved
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: None)
    assert _field_states(section, view())[field] == "not set"


def test_doctor_rejects_invalid_import_without_ignoring_your_saved_mail_settings(local_settings, monkeypatch):
    from app import cli_local
    from app.core.local_secrets import update_settings
    from app.gtm import local_mail

    _, root, domain = local_settings
    (root / ".env").write_text("SMTP_HOST=mail.owner.example\nSMTP_PORT=bad-port\n"
        "SMTP_USER=owner@owner.example\nSMTP_PASSWORD=" + domain + " private\n")
    monkeypatch.setattr(local_mail, "_smtp", lambda *_args: pytest.fail("Doctor must not send mail"))
    okay, message = cli_local._doctor_email()
    assert not okay
    assert "Settings > Keys and connections" in message and "make doctor" in message
    assert domain not in message
    update_settings("smtp", {"host": "mail.owner.example", "port": 587, "user": "owner@owner.example"},
        {"password": domain + " saved"})
    okay, message = cli_local._doctor_email()
    assert okay and domain not in message


def test_doctor_finds_model_configuration_in_shared_settings(local_settings, monkeypatch):
    import shutil
    from app import cli_local
    from app.core.providers_local import save_settings

    _, root, _domain = local_settings
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
        models={"chat": "offline-fixture"})
    assert not any((root / "vault/providers" / name).exists() for name in ("settings.enc", "settings.json"))
    shutil.rmtree(root / "vault/providers")
    assert not (root / "vault/providers").exists()
    monkeypatch.setattr(cli_local, "_doctor_ports", lambda: (True, "Run make doctor."))
    monkeypatch.setattr(cli_local, "_command_ok", lambda command: False)
    assert cli_local.doctor()["keys"][0]


def test_export_public_settings_preserves_values_without_secret_metadata(local_settings, monkeypatch):
    import sqlite3
    import zipfile
    from app.core import local_data
    from app.core.local_secrets import get_secret, update_settings

    _, root, domain = local_settings
    database = root / "records.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{database}")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes(body TEXT)")
        connection.execute("INSERT INTO notes VALUES (?)", (domain,))
    update_settings("smtp", {"sender_name": domain}, {"password": domain + " private"})
    update_settings("runtime", {"offline": "1", "cassette_mode": "replay"}, {})
    archive = local_data.export_data()
    with zipfile.ZipFile(archive) as contents:
        payload = json.loads(contents.read("data/local-settings.json"))
        assert not any("vault" in name or "local-key" in name or ".env" in name for name in contents.namelist())
    assert set(payload) == {"values", "secrets", "sources"} and payload["secrets"] is None
    assert payload["values"]["smtp"]["sender_name"] == domain
    assert "password" not in payload["values"]["smtp"]
    assert payload["sources"]["smtp"] == {"sender_name": "app"}
    assert payload["values"]["runtime"] == {"offline": "1", "cassette_mode": "replay"}
    local_data._check_public_settings(payload)
    with pytest.raises(ValueError):
        local_data._check_public_settings({**payload, "sources": {**payload["sources"],
            "smtp": {**payload["sources"]["smtp"], "password": "app"}}})
    assert get_secret("smtp", "password") == domain + " private"


def test_settings_round_trip_keeps_secrets_write_only(local_settings):
    client, root, domain = local_settings
    from app.core.local_secrets import get_secret

    sections = {
        "images": {"comfyui_url": "http://127.0.0.1:8188", "bfl_api_key": domain + " image", "gemini_api_key": domain + " generation", "pexels_api_key": domain + " stock", "pixabay_api_key": domain + " photo"},
        "smtp": {"host": "", "port": 587, "user": domain, "password": domain + " private", "sender_name": domain, "postal_address": domain, "imap_host": "mail.invalid", "imap_port": 993, "booking_link": "https://calendar.invalid/book"},
        "publishing": {"provider": "s3", "project": domain, "bucket": "", "region": "", "token": domain + " private", "access_key_id": domain + " access", "secret_access_key": domain + " secret", "session_token": domain + " session", "prefix": "pages/", "site_url": "https://pages.invalid", "waitlist_email": "owner@pages.invalid"},
        "search": {"provider": "tavily", "firecrawl_url": "", "firecrawl_key": domain + " private", "api_key": domain + " search"},
        "composio": {"api_key": domain + " private", "auth_configs": {"gmail": domain}},
        "spend": {"monthly_spend_cap": "12.34"},
        "data": {"backup_directory": str(root.parent / domain)},
    }
    for section, body in sections.items():
        saved = client.put(f"/local/settings/{section}", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        view = client.get(f"/local/settings/{section}")
        assert view.status_code == 200
        for field, value in body.items():
            if field in view.json()["secrets_set"]:
                assert view.json()["secrets_set"][field] is True
                assert value not in view.text
                assert get_secret(section, field) == value
            else:
                assert view.json()["values"][field] == value
    assert client.get("/local/settings/mode").json() == {"local_mode": True, "env_path": str(root / ".env")}
    assert client.get("/local/settings/data").json()["data_folder"] == str(root)
    stored = (root / "local-settings.json").read_text()
    assert domain + " private" not in stored
    assert json.loads(stored)["values"]["smtp"]["sender_name"] == domain
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("source", ["app", "env", "launcher"])
def test_image_settings_reach_media_consumers_without_exposing_keys(local_settings, monkeypatch, source):
    from app.marketing.media import images, probes

    client, root, domain = local_settings
    body = {"comfyui_url": "http://127.0.0.1:8189", **{
        name: domain + " " + name for name in ("bfl_api_key", "gemini_api_key", "pexels_api_key", "pixabay_api_key")
    }}
    if source == "app":
        response = client.put("/local/settings/images", json=body)
        assert response.status_code == 200, response.text
    else:
        variables = ("COMFYUI_URL", "BFL_API_TOKEN", "GOOGLE_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY")
        if source == "env":
            (root / ".env").write_text("\n".join(f"{variable}={value}" for variable, value in zip(variables, body.values())))
        else:
            for variable, value in zip(variables, body.values()):
                monkeypatch.setenv(variable, value)
    catalog = client.get("/local/settings/catalog")
    assert catalog.status_code == 200
    section = next(section for section in catalog.json()["sections"] if section["id"] == "images")
    assert section["state"] == "ready"
    assert all(not field["required"] for field in section["fields"])
    assert images._comfy_base() == body["comfyui_url"]
    assert images._bfl_key() == body["bfl_api_key"]
    assert images._gemini_key() == body["gemini_api_key"]
    assert probes._stock_key("pexels") == body["pexels_api_key"]
    assert probes._stock_key("pixabay") == body["pixabay_api_key"]
    view = client.get("/local/settings/images")
    assert view.status_code == 200
    stored = (root / "local-settings.json").read_text()
    for name, value in body.items():
        if name != "comfyui_url":
            assert view.json()["secrets_set"][name]
            assert value not in view.text + catalog.text + stored
    if source == "launcher":
        assert not (root / ".env").exists()


def test_catalog_describes_every_field_without_secrets(local_settings):
    client, root, domain = local_settings
    from app.api.local_settings import SCHEMAS

    client.put("/local/settings/composio", json={"api_key": domain + " private"})
    response = client.get("/local/settings/catalog")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert domain + " private" not in response.text
    body = response.json()
    assert body["env_path"] == str(root / ".env")
    sections = {section["id"]: section for section in body["sections"]}
    assert set(sections) == {"providers", *SCHEMAS}
    for name, schema in SCHEMAS.items():
        section = sections[name]
        assert section["path"] == f"/api/local/settings/{name}"
        assert section["state"] in {"ready", "needs_setup"}
        assert set(field["name"] for field in section["fields"]) == set(schema.model_fields)
        for field in section["fields"]:
            assert all(key in field for key in ("label", "secret", "purpose", "capabilities", "url", "data_sent", "required", "state", "env"))
            assert isinstance(field["secret"], bool)
            assert field["state"] in {"set", "not set", "from .env"}
            assert field["purpose"] and field["data_sent"] and field["capabilities"]
    assert next(field for field in sections["composio"]["fields"] if field["name"] == "api_key")["state"] == "set"
    assert sections["search"]["state"] == "ready"


def test_images_settings_import_save_and_validate_write_only_keys(local_settings):
    client, root, domain = local_settings
    from app.core.local_secrets import get_secret
    from app.marketing.media import images, probes

    secrets = {name: domain + " private " + name for name in
               ("bfl_api_key", "gemini_api_key", "pexels_api_key", "pixabay_api_key")}
    (root / ".env").write_text("COMFYUI_URL=http://127.0.0.1:8188\n" + "\n".join(
        name.upper() + "=" + value for name, value in secrets.items()))
    (root / ".env").chmod(0o600)
    response = client.get("/local/settings/images")
    assert response.status_code == 200
    assert response.json()["values"] == {"comfyui_url": "http://127.0.0.1:8188"}
    assert all(response.json()["secrets_set"].values())
    assert all(state == "from .env" for state in response.json()["field_states"].values())
    assert all(get_secret("images", name) == value for name, value in secrets.items())
    replacement = domain + " replacement"
    saved = client.put("/local/settings/images", json={"comfyui_url": "http://localhost:8189", "bfl_api_key": replacement})
    assert saved.status_code == 200
    assert get_secret("images", "bfl_api_key") == replacement
    assert get_secret("images", "pexels_api_key") == secrets["pexels_api_key"]
    assert images._comfy_base() == "http://localhost:8189"
    assert images._bfl_key() == replacement
    assert images._gemini_key() == secrets["gemini_api_key"]
    assert probes._stock_key("pexels") == secrets["pexels_api_key"]
    assert probes._stock_key("pixabay") == secrets["pixabay_api_key"]
    before = (root / "local-settings.json").read_bytes()
    for endpoint in ("https://user:password@reader.invalid", "http://reader.invalid"):
        invalid = client.put("/local/settings/images", json={"comfyui_url": endpoint, "bfl_api_key": domain + " rejected"})
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["fields"] == ["comfyui_url"]
        assert "Keys and connections" in invalid.json()["detail"]["message"]
        assert (root / "local-settings.json").read_bytes() == before
    catalog = client.get("/local/settings/catalog")
    assert catalog.status_code == 200
    section = next(section for section in catalog.json()["sections"] if section["id"] == "images")
    assert {field["name"] for field in section["fields"] if field["secret"]} == set(secrets)
    assert not any(field["required"] for field in section["fields"])
    for output in (response.text, saved.text, invalid.text, catalog.text, before.decode()):
        assert replacement not in output and domain + " rejected" not in output
        assert all(value not in output for value in secrets.values())


def test_anthropic_env_import_uses_default_model_without_exposing_key(local_settings):
    client, root, domain = local_settings
    from app.core.providers_local import load_local_config

    (root / ".env").write_text("SUPYMEM_LLM_PROVIDER=anthropic\nANTHROPIC_API_KEY=" + domain + " private\n")
    (root / ".env").chmod(0o600)
    response = client.get("/local/settings/catalog")
    assert response.status_code == 200
    section = next(section for section in response.json()["sections"] if section["id"] == "providers")
    assert section["state"] == "saved_not_tested"
    assert load_local_config()._local_role_models["chat"] == "claude-sonnet-5-5"
    assert load_local_config().api_key.get_secret_value() == domain + " private"
    assert domain + " private" not in response.text


def test_catalog_offers_connection_specific_help_and_conditional_fields(local_settings):
    client, _, domain = local_settings
    sections = {section["id"]: section for section in client.get("/local/settings/catalog").json()["sections"]}
    for section, wording in {"providers": "Get an API key", "smtp": "Create an app password",
                             "publishing": "Create a token", "search": "Get an API key", "composio": "Get an API key"}.items():
        assert sections[section]["help_text"] == wording
        assert domain not in sections[section]["help_text"]
    search = {field["name"]: field for field in sections["search"]["fields"]}
    assert search["api_key"]["providers"] == ["tavily", "exa"]
    assert "providers" not in search["firecrawl_url"] and "providers" not in search["firecrawl_key"]
    publishing = {field["name"]: field for field in sections["publishing"]["fields"]}
    assert publishing["project"]["providers"] == ["vercel", "netlify"]
    assert publishing["bucket"]["providers"] == ["s3"]
    assert publishing["site_url"]["providers"] == ["vercel", "netlify", "s3"]
    assert next(option for option in publishing["provider"]["options"] if option["value"] == "s3")["label"] == "S3"
    assert sections["publishing"]["provider_urls"]["s3"].startswith("https://console.aws.amazon.com/")
    assert client.get("/local/settings/publishing").json()["missing"] == ["provider"]
    assert all(not field["required"] for field in publishing.values() if field.get("providers"))


def _assert_imported_provider_becomes_ready_after_saved_test(client, monkeypatch):
    from types import SimpleNamespace

    from app.core.providers_local import router
    from llm_gateway import sole

    async def reply(_client, config, request):
        assert config.base_url == "https://model.invalid/v1"
        assert request["response_format"] == {"type": "json_object"}
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"connected":true}'))],
            usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        )

    def state():
        response = client.get("/local/settings/catalog")
        assert response.status_code == 200
        return next(section["state"] for section in response.json()["sections"] if section["id"] == "providers")

    client.app.include_router(router)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setattr(sole, "_dial_chat", reply)
    saved = {"provider": "openai_compatible"}
    draft = client.post("/local/providers/test", json={**saved, "model": "draft-model"})
    assert draft.status_code == 200
    assert state() == "saved_not_tested"
    tested = client.post("/local/providers/test", json=saved)
    assert tested.status_code == 200
    assert tested.json()["verified"] is True
    assert state() == "ready"


def test_catalog_imports_and_reports_the_configured_env_file(local_settings, monkeypatch):
    client, root, domain = local_settings
    path = root / "config" / ".env"
    path.parent.mkdir()
    path.write_text("SUPYMEM_LLM_PROVIDER=openai_compatible\nOPENAI_COMPATIBLE_BASE_URL=https://model.invalid/v1\n"
                    "SUPYMEM_MODEL_CHAT=configured-model\nOPENAI_COMPATIBLE_API_KEY=" + domain + " private\n")
    path.chmod(0o600)
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(path))
    response = client.get("/local/settings/catalog")
    assert response.status_code == 200
    assert response.json()["env_path"] == str(path)
    provider = next(section for section in response.json()["sections"] if section["id"] == "providers")
    assert provider["state"] == "saved_not_tested"
    assert next(field for field in provider["fields"] if field["name"] == "api_key")["state"] == "from .env"
    assert domain + " private" not in response.text
    _assert_imported_provider_becomes_ready_after_saved_test(client, monkeypatch)


def test_env_import_preserves_saved_edits_and_marks_sources(local_settings):
    client, root, domain = local_settings
    from app.core.local_secrets import get_secret

    (root / ".env").write_text(f"SUPYMEM_SEARCH_PROVIDER=tavily\nTAVILY_API_KEY={domain}-search-key\nSUPYMEM_IMAP_HOST=mail.invalid\nSUPYMEM_IMAP_PORT=993\nSUPYMEM_COMPOSIO_AUTH_CONFIGS=" + json.dumps({"gmail": "auth-config-mail"}) + "\n")
    (root / ".env").chmod(0o600)
    response = client.get("/local/settings/catalog")
    assert response.status_code == 200
    search = next(section for section in response.json()["sections"] if section["id"] == "search")
    assert next(field for field in search["fields"] if field["name"] == "api_key")["state"] == "from .env"
    assert get_secret("search", "api_key") == domain + "-search-key"
    assert domain not in response.text
    assert client.get("/local/settings/smtp").json()["values"]["imap_port"] == 993
    assert client.get("/local/settings/composio").json()["values"]["auth_configs"] == {"gmail": "auth-config-mail"}
    assert client.get("/local/settings/composio").json()["field_states"]["auth_configs"] == "from .env"
    assert client.put("/local/settings/search", json={"api_key": domain + " replacement"}).status_code == 200
    response = client.get("/local/settings/catalog")
    assert get_secret("search", "api_key") == domain + " replacement"
    search = next(section for section in response.json()["sections"] if section["id"] == "search")
    assert next(field for field in search["fields"] if field["name"] == "api_key")["state"] == "set"


def test_model_env_import_uses_provider_store_and_keeps_keys_private(local_settings, monkeypatch):
    client, root, domain = local_settings
    from app.core.providers_local import load_local_config, save_settings

    (root / ".env").write_text("SUPYMEM_LLM_PROVIDER=openai_compatible\nOPENAI_COMPATIBLE_BASE_URL=https://model.invalid/v1\nSUPYMEM_MODEL_CHAT=configured-model\nOPENAI_COMPATIBLE_API_KEY=" + domain + " private\n")
    (root / ".env").chmod(0o600)
    response = client.get("/local/settings/catalog")
    assert response.status_code == 200
    assert domain + " private" not in response.text
    provider = next(section for section in response.json()["sections"] if section["id"] == "providers")
    assert provider["state"] == "saved_not_tested"
    assert next(field for field in provider["fields"] if field["name"] == "api_key")["state"] == "from .env"
    assert load_local_config().api_key.get_secret_value() == domain + " private"
    _assert_imported_provider_becomes_ready_after_saved_test(client, monkeypatch)
    save_settings(provider="openai_compatible", api_key=domain + " replacement", models={"chat": "configured-model"})
    assert client.get("/local/settings/catalog").status_code == 200
    assert load_local_config().api_key.get_secret_value() == domain + " replacement"
    (root / ".env").write_text("SUPYMEM_LLM_PROVIDER=openai\nSUPYMEM_MODEL_CHAT=another-model\nOPENAI_API_KEY=" + domain + " another private\n")
    assert client.get("/local/settings/catalog").status_code == 200
    assert load_local_config().api_key.get_secret_value() == domain + " replacement"


def test_launcher_env_import_saves_keys_without_plaintext_files(local_settings, monkeypatch):
    client, root, domain = local_settings
    from app.core.local_secrets import get_secret, read_settings

    monkeypatch.setenv("SUPYMEM_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", domain + " private")
    monkeypatch.setenv("SUPYMEM_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SUPYMEM_SMTP_USERNAME", "owner@example.invalid")
    monkeypatch.setenv("SUPYMEM_SMTP_PASSWORD", domain + " password")
    response = client.get("/local/settings/catalog")
    assert response.status_code == 200
    assert get_secret("search", "api_key") == domain + " private"
    assert get_secret("smtp", "password") == domain + " password"
    assert read_settings("smtp")["values"]["user"] == "owner@example.invalid"
    assert domain + " private" not in response.text and domain + " password" not in response.text
    assert not (root / ".env").exists()


def test_invalid_env_does_not_block_editing_or_overwrite_saved_data(local_settings):
    client, root, domain = local_settings
    (root / ".env").write_text(f"SUPYMEM_COMPOSIO_AUTH_CONFIGS={domain}\n")
    (root / ".env").chmod(0o600)
    catalog = client.get("/local/settings/catalog")
    assert catalog.status_code == 200
    assert "App authorization IDs" in catalog.json()["import_error"]
    assert domain not in catalog.text
    response = client.get("/local/settings/composio")
    assert response.status_code == 200
    assert response.json()["values"]["auth_configs"] == {}
    assert client.put("/local/settings/composio", json={"auth_configs": {"gmail": domain}}).status_code == 200
    assert client.get("/local/settings/composio").json()["import_error"] is None


def test_smtp_readiness_matches_send_requirements_without_blocking_login(local_settings, monkeypatch):
    from unittest.mock import MagicMock
    from app.api import local_settings as settings_api
    from app.core.local_capabilities import capability
    from app.core.local_secrets import update_settings

    client, _, domain = local_settings
    smtp = MagicMock()
    monkeypatch.setattr(settings_api.smtplib, "SMTP", smtp)
    assert client.put("/local/settings/smtp", json={"host": "mail.invalid", "user": "owner@mail.invalid",
        "password": domain, "postal_address": "10 Main Street"}).status_code == 200
    update_settings("capabilities", {"send_email": True}, {})
    readiness = client.get("/local/settings/smtp").json()
    assert readiness["state"] == capability("send_email")["state"] == "needs_setup"
    assert readiness["missing"] == ["sender_name"]
    login = client.post("/local/settings/smtp/test").json()
    assert login["ok"] is True and "mail sign-in works" in login["next_step"]
    smtp.return_value.__enter__.return_value.send_message.assert_not_called()
    assert client.put("/local/settings/smtp", json={"sender_name": domain}).status_code == 200
    assert client.get("/local/settings/smtp").json()["state"] == capability("send_email")["state"] == "ready"


def test_connection_probes_are_bounded_read_only_and_redacted(local_settings, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from app.api import local_settings as settings_api
    from app.research import local_providers

    client, _, domain = local_settings
    client.put("/local/settings/smtp", json={"host": "mail.invalid", "user": "owner@mail.invalid", "password": domain, "imap_host": "mail.invalid"})
    smtp = MagicMock()
    monkeypatch.setattr(settings_api.smtplib, "SMTP", smtp)
    imap = MagicMock()
    monkeypatch.setattr(settings_api.imaplib, "IMAP4_SSL", imap)
    assert client.post("/local/settings/smtp/test").json()["ok"] is True
    smtp.assert_called_once_with("mail.invalid", 587, timeout=5)
    smtp.return_value.__enter__.return_value.starttls.assert_called_once()
    smtp.return_value.__enter__.return_value.login.assert_called_once_with("owner@mail.invalid", domain)
    smtp.return_value.__enter__.return_value.sendmail.assert_not_called()
    assert client.post("/local/settings/imap/test").json()["ok"] is True
    imap.return_value.__enter__.return_value.login.assert_called_once_with("owner@mail.invalid", domain)
    imap.return_value.__enter__.return_value.select.assert_not_called()
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: SimpleNamespace(name="builtin"))
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(local_providers, "search", query)
    assert client.post("/local/settings/search/test").json()["ok"] is True
    assert query.await_count == 1
    smtp.return_value.__enter__.return_value.login.side_effect = OSError(domain)
    response = client.post("/local/settings/smtp/test")
    assert response.json()["ok"] is False
    assert "Keys and connections" in response.json()["next_step"]
    assert domain not in response.text


@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3", "composio"])
def test_account_probes_use_saved_credentials_without_writing(local_settings, monkeypatch, provider):
    from unittest.mock import MagicMock
    from app.api import local_settings as settings_api

    client, _, domain = local_settings
    section = "composio" if provider == "composio" else "publishing"
    values = {"api_key": domain} if section == "composio" else {"provider": provider, "project": "site", "bucket": "bucket", "region": "us-east-1", "site_url": "https://pages.invalid", "token": domain, "access_key_id": domain, "secret_access_key": domain, "session_token": domain}
    assert client.put(f"/local/settings/{section}", json=values).status_code == 200
    transport = MagicMock()
    transport.return_value.__enter__.return_value.get.return_value.status_code = 200
    monkeypatch.setattr(settings_api.httpx, "Client", transport)
    s3 = MagicMock()
    import boto3

    monkeypatch.setattr(boto3, "client", s3)
    response = client.post(f"/local/settings/{section}/test")
    assert response.json()["ok"] is True
    assert domain not in response.text
    if provider == "s3":
        assert s3.call_args.kwargs["aws_session_token"] == domain
        s3.return_value.head_bucket.assert_called_once_with(Bucket="bucket")
        s3.return_value.put_object.assert_not_called()
    else:
        transport.assert_called_once_with(timeout=8, follow_redirects=False, trust_env=False)
        transport.return_value.__enter__.return_value.get.assert_called_once()
        transport.return_value.__enter__.return_value.post.assert_not_called()
        transport.return_value.__enter__.return_value.get.return_value.status_code = 401
        response = client.post(f"/local/settings/{section}/test")
        assert response.json()["ok"] is False
        assert "Keys and connections" in response.json()["next_step"]
        assert domain not in response.text


@pytest.mark.parametrize("provider, expected", [("builtin", "internet connection"), ("tavily", "Search API key for Tavily"), ("exa", "Search API key for Exa"), ("firecrawl", "Firecrawl URL"), ("openai", "Model API key")])
def test_search_probe_failure_names_the_selected_connection(local_settings, monkeypatch, provider, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.research import local_providers

    client, _, domain = local_settings
    assert client.put("/local/settings/search", json={"provider": provider, "api_key": domain}).status_code == 200
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: SimpleNamespace(name=provider))
    monkeypatch.setattr(local_providers, "search", AsyncMock(side_effect=OSError(domain)))
    response = client.post("/local/settings/search/test")
    assert response.json()["ok"] is False
    assert expected in response.json()["next_step"]
    assert "Keys and connections" in response.json()["next_step"]
    assert domain not in response.text
    if provider == "builtin":
        assert "API key" not in response.text


def test_search_probe_spend_limit_points_to_the_cap_not_a_key(local_settings, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.credits.errors import CreditsUnavailable
    from app.research import local_providers

    client, _, domain = local_settings
    client.put("/local/settings/search", json={"provider": "tavily", "api_key": domain})
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: SimpleNamespace(name="tavily"))
    monkeypatch.setattr(local_providers, "search", AsyncMock(side_effect=CreditsUnavailable(domain)))
    response = client.post("/local/settings/search/test")
    assert response.json()["ok"] is False
    assert "Monthly limit (USD)" in response.json()["next_step"]
    assert "Keys and connections" in response.json()["next_step"]
    assert domain not in response.text


def test_rejects_backup_folder_inside_your_data_before_saving(local_settings, monkeypatch):
    import sqlite3

    client, root, domain = local_settings
    host = "quartz-dispatch.test" if domain.startswith("Industrial") else "puddle-kites.test"
    good = root.parent / host
    assert client.put("/local/settings/data", json={"backup_directory": str(good)}).status_code == 200
    invalid = client.put("/local/settings/data", json={"backup_directory": str(root / host)})
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    assert detail["fields"] == ["backup_directory"]
    assert "Full backup folder path" in detail["message"]
    assert "Your data folder and backup" in detail["message"]
    assert client.get("/local/settings/data").json()["values"]["backup_directory"] == str(good)
    database = root / "database.sqlite3"
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "7.6.5")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
    from app.db.local_sqlite import migrate_sqlite

    assert migrate_sqlite(f"sqlite:///{database}").is_relative_to(good)


def test_backup_and_export_expose_partial_coverage(local_settings, monkeypatch):
    import io
    import sqlite3
    import tarfile
    import zipfile

    client, root, domain = local_settings
    host = "quartz-dispatch.test" if domain.startswith("Industrial") else "puddle-kites.test"
    database = root / "database.sqlite3"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{database}")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
    outside = root.parent / host
    outside.write_text(host)
    (root / "linked.txt").symlink_to(outside)
    backup = client.post("/local/settings/backup")
    assert backup.status_code == 200
    assert backup.headers["X-Supymem-Skipped-Files"] == "1"
    with tarfile.open(root.with_name(f"{root.name}-backups") / backup.json()["backup"]) as contents:
        assert "data/linked.txt" not in contents.getnames()
    exported = client.get("/local/settings/export")
    assert exported.status_code == 200
    assert exported.headers["X-Supymem-Skipped-Files"] == "1"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as contents:
        assert "data/linked.txt" not in contents.namelist()


def test_settings_data_transfer_is_local_only_and_stages_restore(local_settings, monkeypatch):
    import io
    import sqlite3
    import zipfile

    client, root, domain = local_settings
    database = root / "database.sqlite3"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{database}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(root / ".env"))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.execute("INSERT INTO notes VALUES (?)", (domain,))
    (root / "uploads").mkdir()
    (root / "uploads" / "work.txt").write_text(domain)
    client.put("/local/settings/composio", json={"api_key": domain + " private"})
    exported = client.get("/local/settings/export")
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("data/uploads/work.txt").decode() == domain
        assert "data/local-key.bin" not in archive.namelist()
    assert client.post("/local/settings/import", content=exported.content,
                       headers={"content-type": "application/zip"}).status_code == 200
    assert client.post("/local/settings/restore", json={"body": "../other-backup.tar.gz"}).status_code == 422
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    for path in ("export", "backups"):
        assert client.get(f"/local/settings/{path}").status_code == 404
    assert client.post("/local/settings/import", content=exported.content,
                       headers={"content-type": "application/zip"}).status_code == 404
    assert client.post("/local/settings/restore", json={"body": "backup.tar.gz"}).status_code == 404


def test_omitted_keys_survive_edits_and_concurrent_sections(local_settings):
    client, _, domain = local_settings
    from app.core.local_secrets import get_secret

    assert client.put("/local/settings/smtp", json={"password": domain}).status_code == 200
    assert client.put("/local/settings/smtp", json={"sender_name": domain}).status_code == 200
    assert get_secret("smtp", "password") == domain
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda section: client.put(f"/local/settings/{section}", json={"api_key": domain}), ["composio", "search"]))
    assert all(result.status_code == 200 for result in results)
    assert get_secret("composio", "api_key") == domain
    assert get_secret("search", "api_key") == domain
    assert client.get("/local/settings/smtp").json()["values"]["sender_name"] == domain


def test_invalid_input_never_echoes_secrets_or_changes_files(local_settings):
    client, root, domain = local_settings
    assert client.put("/local/settings/smtp", json={"password": domain}).status_code == 200
    before = (root / "local-settings.json").read_bytes()
    invalid = [
        ("smtp", {"port": 0, "password": domain}),
        ("smtp", {"port": 65536, "password": domain}),
        ("smtp", {"sender_name": domain + "\r\n", "password": domain}),
        ("smtp", {"unknown": domain}),
        ("composio", {"api_key": ""}),
        ("composio", [domain]),
        ("publishing", {"provider": domain, "token": domain}),
        ("search", {"firecrawl_url": "https://user:password@reader.invalid", "api_key": domain}),
        ("search", {"firecrawl_url": "https://reader.invalid?key=" + domain, "api_key": domain}),
        ("images", {"comfyui_url": "http://image-service.invalid", "bfl_api_key": domain}),
        ("images", {"comfyui_url": "https://user:password@image-service.invalid", "bfl_api_key": domain}),
        ("images", {"comfyui_url": "https://image-service.invalid?key=" + domain, "bfl_api_key": domain}),
        ("smtp", {"imap_host": "bad/host", "password": domain}),
        ("smtp", {"imap_port": 0, "password": domain}),
        ("smtp", {"booking_link": "https://user:password@calendar.invalid", "password": domain}),
        ("publishing", {"site_url": "http://pages.invalid", "token": domain}),
        ("publishing", {"prefix": "../private", "token": domain}),
        ("publishing", {"waitlist_email": "not a mailbox", "token": domain}),
        ("publishing", {"waitlist_email": "owner@@pages.invalid", "token": domain}),
        ("composio", {"auth_configs": {"bad slug": domain}}),
        ("spend", {"monthly_spend_cap": "-1"}),
        ("spend", {"monthly_spend_cap": "NaN"}),
        ("data", {"backup_directory": "../" + domain}),
    ]
    for section, body in invalid:
        response = client.put(f"/local/settings/{section}", json=body)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "invalid_local_settings"
        assert "Keys and connections" in response.json()["detail"]["message"]
        assert domain not in response.text
        assert "you" in response.text.lower()
        assert (root / "local-settings.json").read_bytes() == before
    malformed = client.put("/local/settings/composio", content='{"api_key":"' + domain, headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422
    assert domain not in malformed.text


def test_hosted_and_unknown_sections_never_touch_storage(local_settings, monkeypatch):
    import hashlib
    from core.local_security import _proxy_secret

    client, root, domain = local_settings
    _proxy_secret()
    original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir()}
    before = {path.name: (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.iterdir()}
    assert client.get("/local/settings/unknown").status_code == 404
    for mode in [None, "hosted", "production"]:
        if mode is None:
            monkeypatch.delenv("SUPYMEM_MODE", raising=False)
        else:
            monkeypatch.setenv("SUPYMEM_MODE", mode)
        for section in ["mode", "smtp", "publishing", "search", "composio", "spend", "data"]:
            assert client.get(f"/local/settings/{section}").status_code == 404
            assert client.put(f"/local/settings/{section}", json={"password": domain}).status_code == 404
        from app.core.local_secrets import get_secret

        for section, name in (("smtp", "password"), ("model", "openai")):
            with pytest.raises(ValueError):
                get_secret(section, name)
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir()} == original
    assert {path.name: (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.iterdir()} == before


def test_missing_key_or_corrupt_store_fails_without_overwriting(local_settings):
    client, root, domain = local_settings
    assert client.put("/local/settings/composio", json={"api_key": domain}).status_code == 200
    store = root / "local-settings.json"
    before = store.read_bytes()
    key = root / "local-key.bin"
    original_key = key.read_bytes()
    key.unlink()
    for method in ["get", "put"]:
        response = getattr(client, method)("/local/settings/composio", **({"json": {"api_key": "replacement"}} if method == "put" else {}))
        assert response.status_code == 503
        assert domain not in response.text
        assert store.read_bytes() == before
        assert not key.exists()
    key.write_bytes(original_key)
    key.chmod(0o600)
    store.write_text("not valid json")
    assert client.put("/local/settings/composio", json={"api_key": domain}).status_code == 503
    assert store.read_text() == "not valid json"


def test_failed_write_and_symlink_cannot_destroy_or_expose_data(local_settings, monkeypatch):
    client, root, domain = local_settings
    from app.core import local_secrets

    assert client.put("/local/settings/composio", json={"api_key": domain}).status_code == 200
    store = root / "local-settings.json"
    before = store.read_bytes()
    with monkeypatch.context() as patch:
        def fail_replace(*args):
            raise OSError("private storage error " + domain)

        patch.setattr(local_secrets.os, "replace", fail_replace)
        response = client.put("/local/settings/composio", json={"api_key": "replacement"})
        assert response.status_code == 503
        assert domain not in response.text
        assert store.read_bytes() == before
    store.rename(root / "saved.json")
    store.symlink_to(root / "saved.json")
    assert client.get("/local/settings/composio").status_code == 503
    assert (root / "saved.json").read_bytes() == before


def test_changed_connection_requires_secret_reentry_before_any_send(local_settings):
    client, root, domain = local_settings
    from app.core.local_secrets import get_secret, read_settings

    for section, initial, changed, secret_name in (
        ("smtp", {"host": f"mail.{domain.replace(' ', '').lower()}.example", "password": "test-only-password"},
         {"host": "replacement.example"}, "password"),
        ("search", {"firecrawl_url": "http://127.0.0.1:3002", "firecrawl_key": "test-only-reader-key"},
         {"firecrawl_url": "http://127.0.0.1:3003"}, "firecrawl_key"),
        ("publishing", {"provider": "vercel", "project": domain, "token": "test-only-publish-key"},
         {"provider": "netlify"}, "token"),
    ):
        assert client.put(f"/local/settings/{section}", json=initial).status_code == 200
        before = read_settings(section)
        response = client.put(f"/local/settings/{section}", json=changed)
        assert response.status_code == 422
        assert "Keys and connections" in response.text
        assert read_settings(section) == before
        assert client.put(f"/local/settings/{section}", json={**changed, secret_name: initial[secret_name]}).status_code == 200
        assert get_secret(section, secret_name) == initial[secret_name]


def test_firecrawl_reader_can_be_turned_off_without_reentering_key(local_settings, monkeypatch):
    import asyncio
    from app.research.local_providers import local_read, scrape_provider
    from app.signals import local_reader

    client, _, domain = local_settings
    endpoint = "https://" + domain.replace(" ", "-").lower() + ".example"
    path = "/local/settings/search"
    assert client.put(path, json={"provider": "firecrawl", "firecrawl_url": endpoint,
                                  "firecrawl_key": "test-only-reader-key"}).status_code == 200
    switched = client.put(path, json={"provider": "builtin"})
    assert switched.status_code == 200 and switched.json()["values"]["firecrawl_url"] == endpoint
    assert scrape_provider().name == "firecrawl"
    cleared = client.put(path, json={"firecrawl_url": ""})
    assert cleared.status_code == 200 and not cleared.json()["values"]["firecrawl_url"]
    assert scrape_provider().name == "duckduckgo"
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)
    monkeypatch.setattr(local_reader, "_post", lambda *args, **kwargs: pytest.fail("Disabled reader was contacted"))
    monkeypatch.setattr(local_reader, "_basic_read", lambda url, main_only: local_reader._page_response(url, domain))
    assert asyncio.run(local_read(endpoint + "/published")) == domain


@pytest.mark.parametrize("provider,credentials", [
    ("s3", {"access_key_id": "replacement-access", "secret_access_key": "replacement-secret", "session_token": "replacement-session"}),
    ("netlify", {"token": "replacement-token"}),
])
def test_changed_publishing_connection_requires_its_matching_credentials(local_settings, provider, credentials):
    from app.core.local_secrets import get_secret, read_settings

    client, root, domain = local_settings
    initial = {"provider": "vercel" if provider == "s3" else "s3", "project": domain,
        "bucket": domain.lower().replace(" ", "-"), "token": domain + " token",
        "access_key_id": domain + " access", "secret_access_key": domain + " secret",
        "session_token": domain + " session"}
    assert client.put("/local/settings/publishing", json=initial).status_code == 200
    before = read_settings("publishing")
    encrypted = (root / "local-settings.json").read_bytes()
    refused = client.put("/local/settings/publishing", json={"provider": provider})
    assert refused.status_code == 422
    assert "Keys and connections" in refused.text
    assert read_settings("publishing") == before
    assert (root / "local-settings.json").read_bytes() == encrypted
    saved = client.put("/local/settings/publishing", json={"provider": provider, **credentials})
    assert saved.status_code == 200, saved.text
    assert read_settings("publishing")["values"]["provider"] == provider
    for name in ("token", "access_key_id", "secret_access_key", "session_token"):
        expected = credentials.get(name, initial[name])
        assert get_secret("publishing", name) == expected
        assert expected not in saved.text
        assert expected.encode() not in (root / "local-settings.json").read_bytes()
    inactive_field = "project" if provider == "s3" else "bucket"
    inactive_value = domain.lower().replace(" ", "-") + "-spare"
    inactive = client.put("/local/settings/publishing", json={inactive_field: inactive_value})
    assert inactive.status_code == 200, inactive.text
    assert read_settings("publishing")["values"][inactive_field] == inactive_value
    before = read_settings("publishing")
    assert client.put("/local/settings/publishing", json={"provider": initial["provider"]}).status_code == 422
    assert read_settings("publishing") == before


def test_settings_wrapping_key_migrates_to_keychain_without_loss(local_settings, monkeypatch):
    client, root, domain = local_settings
    import base64
    from app.core import local_secrets

    local_secrets.update_settings("smtp", {"host": domain}, {"password": "test-only-password"})
    key = (root / "local-key.bin").read_bytes()
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    monkeypatch.setattr(local_secrets, "_material_started", True)
    monkeypatch.setattr(local_secrets, "_material", {"key": base64.b64encode(key).decode(), "legacy": {}, "storage": "keychain"})
    assert (root / "local-key.bin").exists()
    write = local_secrets._atomic_write

    def fail_marker(path, content):
        if path.name == "local-key.keychain":
            raise OSError("marker could not be saved")
        return write(path, content)

    with monkeypatch.context() as failed:
        failed.setattr(local_secrets, "_atomic_write", fail_marker)
        with pytest.raises(OSError, match="marker could not be saved"):
            local_secrets.get_secret("smtp", "password")
        assert (root / "local-key.bin").read_bytes() == key
    assert local_secrets.get_secret("smtp", "password") == "test-only-password"
    assert not (root / "local-key.bin").exists()
    assert (root / "local-key.keychain").exists()
    assert local_secrets.get_secret("smtp", "password") == "test-only-password"


def test_pending_credential_read_never_blocks_settings_or_overwrites_secrets(local_settings, monkeypatch):
    _, root, domain = local_settings
    from app.core import local_secrets
    local_secrets.update_settings("smtp", {"host": domain}, {"password": "test-only-password"})
    local_secrets.read_settings("runtime")
    before = (root / "local-settings.json").read_bytes()
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: None)
    assert local_secrets.read_settings("smtp")["values"] == {"host": domain}
    assert local_secrets.get_secret("smtp", "password") is None
    with pytest.raises(local_secrets.ConnectionSecretRequired, match="Keys and connections"):
        local_secrets.update_settings("smtp", {}, {"password": "replacement"})
    assert (root / "local-settings.json").read_bytes() == before


def test_denied_legacy_wrapping_key_refuses_to_replace_the_encrypted_vault(local_settings, monkeypatch):
    _, root, domain = local_settings
    from app.core import local_secrets
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "fixture")
    local_secrets.update_settings("smtp", {"host": domain}, {"password": "old-test-password"})
    local_secrets.read_settings("runtime")
    before = (root / "local-settings.json").read_bytes()
    (root / "local-key.bin").unlink()
    local_secrets._atomic_write(root / "local-key.keychain", b"1")
    assert local_secrets.get_secret("smtp", "password") is None
    assert "Unlock your system keychain" in local_secrets.storage_notice()
    with pytest.raises(local_secrets.ConnectionSecretRequired, match="Your data and recovery"):
        local_secrets.update_settings("smtp", {}, {"password": "replacement"})
    assert (root / "local-settings.json").read_bytes() == before
    assert local_secrets.read_settings("smtp")["values"] == {"host": domain}


def test_handoff_completion_is_remembered_without_retry(local_settings, monkeypatch):
    _, root, domain = local_settings
    import threading
    from app.core import local_secrets
    received = threading.Event()
    calls = []
    def receive():
        calls.append(True)
        local_secrets._material = {"key": None, "legacy": {}, "storage": "keyfile", "needs_reentry": True}
        received.set()
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    monkeypatch.setattr(local_secrets, "_material", None)
    monkeypatch.setattr(local_secrets, "_material_started", False)
    monkeypatch.setattr(local_secrets, "_receive_material", receive)
    local_secrets._shell_material()
    assert received.wait(1)
    for _ in range(3):
        assert local_secrets.legacy_secret(domain) is None
    assert len(calls) == 1
    local_secrets.update_settings("smtp", {"host": domain}, {"password": "replacement"})
    assert local_secrets.get_secret("smtp", "password") == "replacement"


@pytest.mark.parametrize("failure", ["invalid", "invalid_key", "timeout"])
def test_invalid_or_failed_shell_pipe_falls_back_once(local_settings, monkeypatch, failure):
    import os
    import types
    from app.core import local_secrets
    reader, writer = os.pipe()
    payload = b"not-json" if failure == "invalid" else b'{"key":"invalid","legacy":{},"storage":"keychain"}'
    os.write(writer, payload)
    os.close(writer)
    calls = []
    def duplicate(descriptor):
        calls.append(descriptor)
        if failure == "timeout":
            raise TimeoutError("pipe read failed")
        return os.dup(reader)
    monkeypatch.setattr(local_secrets, "os", types.SimpleNamespace(**{**vars(os), "dup": duplicate}))
    monkeypatch.setattr(local_secrets, "_material", None)
    monkeypatch.setattr(local_secrets, "_material_started", True)
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    try:
        local_secrets._receive_material()
        for _ in range(3):
            assert local_secrets._shell_material() == {"key": None, "legacy": {}, "storage": "keyfile"}
        assert calls == [0]
    finally:
        os.close(reader)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("key_file", [False, True])
def test_shell_keyfile_fallback_saves_encrypted_keys_without_python_keychain_access(local_settings, monkeypatch, platform, key_file):
    import base64
    import secrets
    from app.core import local_secrets
    from app.core.providers_local import save_settings

    _, root, domain = local_settings
    wrapping_key = secrets.token_bytes(32)
    path = root / "local-key.bin"
    if key_file:
        local_secrets._atomic_write(path, wrapping_key)
    material = {"key": None, "legacy": {}, "storage": "keyfile"}
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    monkeypatch.setattr(local_secrets, "_material", material)
    monkeypatch.setattr(local_secrets, "_material_started", True)
    monkeypatch.setattr(local_secrets.sys, "platform", platform)

    def forbidden_keychain(**kwargs):
        pytest.fail("Bundled Python must not retry the shell's keychain decision")

    monkeypatch.setattr(local_secrets, "_native_material", forbidden_keychain)
    private_key = domain + secrets.token_hex(16)
    save_settings(provider="openai", base_url="https://api.openai.com/v1", api_key=private_key,
                  models={"chat": "your-model"})
    assert local_secrets.get_secret("model", "openai") == private_key
    saved = (root / "local-settings.json").read_bytes()
    assert private_key.encode() not in saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "local-settings.json").stat().st_mode) == 0o600
    if key_file:
        assert path.read_bytes() == wrapping_key
    material["key"] = base64.b64encode(path.read_bytes()).decode()
    assert local_secrets.get_secret("model", "openai") == private_key
    assert (root / "local-settings.json").read_bytes() == saved
    if platform == "linux":
        assert local_secrets.storage_notice() == "Your keys are encrypted in a protected file because no system keychain was found. Keep your data folder private."


@pytest.mark.parametrize("provider", ["openai", "anthropic", "azure_openai", "openai_compatible"])
def test_linux_live_setup_waits_for_editable_provider_fields(local_settings, provider):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    _, _, domain = local_settings
    source = Path(__file__).resolve().parents[2] / "scripts/desktop/e2e_linux.py"
    spec = importlib.util.spec_from_file_location("desktop_setup_test", source)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    address_required = provider in {"azure_openai", "openai_compatible"}
    state = {"ready": False, "address_pending": address_required}
    filled = {}

    class Submitted(Exception):
        pass

    def open_screen(path=None, expected=None):
        state["ready"] = bool(expected)

    def read_screen(click=None):
        if click == "Save and continue":
            raise Submitted
        return {"text": "Connect your AI service" if state["ready"] else "Checking your AI settings. Please wait.", "clicked": False}

    def evaluate(expression):
        if "#base_url" in expression:
            if state["address_pending"]:
                state["address_pending"] = False
                return False
            return address_required
        return True

    journey = object.__new__(runner.Journey)
    journey.entry = "screens"
    journey.live = True
    journey.app = SimpleNamespace(poll=lambda: None)
    journey.model = {"provider": provider, "api_key": domain + " private", "model": domain,
                     "base_url": "https://" + domain.lower().replace(" ", "-") + ".example/v1"}
    journey.wait = journey.verify_resources = journey.passed = lambda: None
    journey.get = lambda path: {"configured": False}
    journey.open_screen = open_screen
    journey.read_screen = read_screen
    journey.desktop = SimpleNamespace(evaluate=evaluate, fill=lambda selector, value: filled.update({selector: value}))
    with pytest.raises(Submitted):
        journey.run_live()
    assert filled["#provider"] == provider
    assert filled["#api_key"] == journey.model["api_key"]
    assert filled["#model"] == journey.model["model"]
    assert ("#base_url" in filled) is address_required
    if address_required:
        assert filled["#base_url"] == journey.model["base_url"]


def test_legacy_key_acknowledgement_requires_the_actual_wrapping_key(local_settings, monkeypatch):
    import base64
    import os
    _, root, domain = local_settings
    from app.core import local_secrets
    local_secrets.update_settings("smtp", {"host": domain}, {"password": "test-password"})
    encoded = base64.b64encode((root / "local-key.bin").read_bytes()).decode()
    material = {"key": encoded, "legacy": {"settings-wrapping-key": base64.b64encode(os.urandom(32)).decode()}, "storage": "keyfile"}
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    monkeypatch.setattr(local_secrets, "_material", material)
    monkeypatch.setattr(local_secrets, "_material_started", True)
    assert local_secrets.get_secret("smtp", "password") == "test-password"
    local_secrets.update_settings("smtp", {}, {"password": "replacement"})
    acknowledgement = root / "local-credentials-migrated.json"
    assert not acknowledgement.exists()
    material["legacy"]["settings-wrapping-key"] = encoded
    assert local_secrets.get_secret("smtp", "password") == "replacement"
    assert json.loads(acknowledgement.read_text()) == ["settings-wrapping-key"]


def test_backups_omit_wrapping_and_owner_keys_but_preserve_your_data(local_settings, monkeypatch):
    import secrets
    import sqlite3
    import tarfile
    from app.cli_local import _backup

    client, root, domain = local_settings
    database = root / "notes.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.execute("INSERT INTO notes VALUES (?)", (domain,))
    key = root / "local-key.bin"
    key.write_bytes(secrets.token_bytes(32))
    key.chmod(0o600)
    owner = root / "custom-owner.secret"
    owner.write_text(secrets.token_hex(32))
    owner.chmod(0o600)
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(owner))
    archive = _backup(root, database, root.parent / "backups")
    with tarfile.open(archive) as contents:
        names = contents.getnames()
        assert "data/local-key.bin" not in names
        assert "data/custom-owner.secret" not in names
        assert "data/notes.sqlite3" in names
