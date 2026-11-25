"""Open a populated 0.1.93 installation with the current desktop services."""

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import tarfile
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import MetaData, create_engine

from tests.company_os_harness import local_owner_headers
from tests.integration.test_local_coding_cli import chat, wait_for
from tests.integration.test_postgres_auth_security import service_modules


CORE = "http://127.0.0.1:3300"
BRIDGE = "http://127.0.0.1:3500"
OWNER = "per_local000000"
VENTURE = "ven_upgrade123456"
WORKSPACE = "wsp_upgrade123456"
CONVERSATION = "chc_upgrade123456"
IDEA = "ids_upgrade123456"
REVISION = "idv_upgrade123456"
PAGE_JOB = "job_upgrade123456"
ORDERS = {"finished": "dwo_finished", "stopped": "dwo_stopped", "approved": "dwo_approved"}
RELEASE_HEAD = "s063routines"
DOMAINS = [
    ("Cold Room Ledger", "Temperature handoff records for food warehouse operators"),
    ("Kite Day", "A flying journal for families who fly kites on weekends"),
]

FAKE_CODEX = '''
import json
from pathlib import Path
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 9.0.0")
elif args == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    assert args[0] == "exec" and "--json" in args and "export.py" in args[-1]
    assert Path("index.html").is_file()
    Path("export.py").write_text("print('Export is ready')\\n")
    print(json.dumps({"type": "thread.started", "thread_id": "thread_upgrade"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}))
'''


def private_file(path, contents):
    path.touch(mode=0o600)
    path.write_bytes(contents)


def old_settings(root):
    # f797c702d's local-settings envelope and Unix file-backed wrapping key.
    values = {
        "model": {"provider": "openai", "providers": {"openai": {
            "base_url": "https://api.openai.com/v1", "models": {"chat": "old-model"}}}},
        "search": {"provider": "tavily"},
        "smtp": {"host": "smtp.example.test", "port": 587, "user": "owner@example.test",
                 "sender_name": "Founder", "postal_address": "10 Test Lane"},
    }
    saved = {section: {field: secrets.token_hex(20)} for section, field in
             (("model", "openai"), ("search", "api_key"), ("smtp", "password"))}
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    ciphertext = nonce + AESGCM(key).encrypt(nonce, json.dumps(saved).encode(), b"local-settings")
    private_file(root / "local-key.bin", key)
    private_file(root / "local-settings.json", json.dumps({"values": values,
        "secrets": base64.b64encode(ciphertext).decode("ascii")}).encode())
    return saved


def old_database(database, migrations, schema, name, idea, device):
    # This revision is f797c702d's head; its predecessor also exercises an actual DDL upgrade.
    engine = create_engine(f"sqlite:///{database}")
    tables = MetaData()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    old = now - timedelta(days=2)
    stamp = old.replace(tzinfo=timezone.utc).isoformat()
    scope = {"venture_id": VENTURE, "workspace_id": WORKSPACE}
    slug = name.lower().replace(" ", "-") + "-123456"
    instruction = "Build my app with Codex."
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            migrations.attributes["connection"] = connection
            command.upgrade(migrations, schema)
        tables.reflect(engine, only=("workspaces", "workspace_members", "ventures", "idea_sessions",
            "idea_revisions", "chat_conversations", "chat_messages", "jobs", "venture_documents",
            "coding_agent_consent"))
        with engine.begin() as connection:
            def insert(table, **fields):
                connection.execute(tables.tables[table].insert().values(**fields))

            insert("workspaces", id=WORKSPACE, kind="venture", name=name, entry_mode="idea",
                   owner_id=OWNER, tenancy="local", autonomy_level="L1", observe_level="O0",
                   settings_json="{}", created_at=old, updated_at=old)
            insert("workspace_members", id="wsm_upgrade", workspace_id=WORKSPACE, person_id=OWNER,
                   role="owner", can_approve=1, created_at=old)
            insert("ventures", id=VENTURE, workspace_id=WORKSPACE, name=name, raw_idea=idea,
                   stage="idea", autonomy_level="L1", created_at=old, updated_at=old)
            insert("idea_sessions", id=IDEA, **scope, case="know", state="confirmed", question_budget=5,
                   questions_asked=0, recon_summary="{}", current_revision_id=REVISION,
                   confirmed_revision_id=REVISION, confirmed_at=old, confirmed_by_person_id=OWNER,
                   created_at=old, updated_at=old)
            insert("idea_revisions", id=REVISION, session_id=IDEA, **scope, rev=1, source="founder",
                   summary=idea, structured=json.dumps({"idea": idea}), diff_from_prev="{}", created_at=old)
            insert("chat_conversations", id=CONVERSATION, **scope, title=name, visibility="legacy_shared",
                   creation_key=f"legacy:{VENTURE}", created_at=old, updated_at=old)
            build_key = json.dumps([VENTURE, "user", f"mac-review:{ORDERS['approved']}:user"], separators=(",", ":"))
            messages = [("msg_oldidea", "user", idea, None), ("msg_oldreply", "assistant", f"Saved {name}.", None),
                        ("msg_oldbuild", "user", instruction, "chat:" + hashlib.sha256(build_key.encode()).hexdigest())]
            for index, (message_id, role, body, key) in enumerate(messages):
                insert("chat_messages", id=message_id, **scope, conversation_id=CONVERSATION,
                       author_person_id=OWNER if role == "user" else None, role=role, kind="text",
                       body_md=body, idempotency_key=key, created_at=old + timedelta(seconds=index))
            insert("coding_agent_consent", id="old-consent", founder_id=OWNER, provider="codex", consented_at=old)
            insert("jobs", id=PAGE_JOB, **scope, kind="pagebuilder.build", status="failed_dead", priority=5,
                   attempts=1, max_attempts=3, timeout_s=900, created_at=old, finished_at=old,
                   payload_json=json.dumps({"variant_count": 1, "_actor_subject": OWNER, "_runtime": {
                       "state": "failed", "stage": "failed", "progress": 0, "operations": {},
                       "calls_used": 0, "quote": {"ceiling_micro": 0, "payer": "byok"},
                       "error": "pagebuilder_interrupted"}}))
            caps = {"max_iterations": 1, "max_wall_seconds": 900, "per_call_seconds": 900, "max_turns": None}
            for label, order_id in ORDERS.items():
                manifest = {"task_id": f"product_build_{VENTURE}", "title": "Plan your product with Codex",
                    "instruction": instruction, "applications": ["com.companyos.app"], "origins": [],
                    "actions": ["editor.edit", "terminal.execute"], "credit_ceiling_micro": 0,
                    "duration_seconds": 900, "coding_loop_verification": {"operation": "workspace_loop_v1",
                        "provider": "codex", "workspace_slug": slug, "profile": "web_app_vercel_v1",
                        "check_recipe": "web_app_vercel_checks_v1", "phase": "plan", "network": "none",
                        "read_scope": "venture_workspace", "write_scope": "venture_workspace", **caps}}
                review = {"id": order_id, **scope, "actor_id": OWNER, "device_id": device, "manifest": manifest,
                    "task_snapshot": {"idea_summary": idea}, "requested_build": {
                        "request_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                        "caps": {"max_iterations": 6, "max_wall_seconds": 2700, "per_call_seconds": 720, "max_turns": None}},
                    "expires_at": (old + timedelta(days=1)).replace(tzinfo=timezone.utc).isoformat()}
                body = {**review, "review_digest": hashlib.sha256(json.dumps(review, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest(), "approved_at": stamp,
                    "state": "approved" if label == "approved" else "stopped", "budget_source": "local"}
                if label != "approved":
                    body.update(claimed_at=stamp, closed_at=stamp, execution_id=f"dex_{label}", native_stop={
                        "execution_id": f"dex_{label}", "processes_stopped": True, "evidence_digest": "a" * 64})
                    if label == "finished":
                        body["native_stop"].update(task_verified=True, verification_kind="workspace_loop_v1")
                insert("venture_documents", id=order_id, venture_id=VENTURE, kind="device_work_order_v1",
                       title=manifest["title"], provenance_json=json.dumps(body), created_at=old, updated_at=old)
    finally:
        engine.dispose()
        migrations.attributes.pop("connection", None)
    database.chmod(0o600)
    return slug


def old_bridge_receipts(idea, device):
    # The old bridge already used scoped send receipts and a separate native journal table.
    from desktop_runtime import device_journal, offline

    scope = {"gateway": CORE, "account_id": OWNER}
    for key in ("delivered", "interrupted"):
        offline.enqueue("POST", "/api/events", {"note": idea}, idempotency_key=key, **scope)
        assert offline.claim(key, **scope)
    offline.record_result("delivered", state="delivered", status=202, **scope)
    scope = (CORE, OWNER, device)
    device_journal.create(scope, VENTURE, ORDERS["finished"])
    device_journal.save(scope, VENTURE, ORDERS["finished"], {"order_id": ORDERS["finished"],
        "execution_id": "dex_finished", "state": "stopped", "task_verified": True, "credits_reconciled": True,
        "evidence": {"processes_stopped": True, "outcome": "plan_ready"}})


def rows(database, query, parameters=()):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        return connection.execute(query, parameters).fetchall()


def read(client, path, **kwargs):
    response = client.get(path, **kwargs)
    assert response.status_code == 200, f"GET {path}: HTTP {response.status_code}"
    return response.json()


@pytest.fixture
def historical_schema(request):
    return getattr(request, "param", RELEASE_HEAD)


@pytest.fixture(params=DOMAINS, ids=["b2b", "consumer"])
def installation(tmp_path, monkeypatch, request, historical_schema):
    name, idea = request.param
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    home = tmp_path / "home"
    home.mkdir()
    database = root / "database.sqlite3"
    marker = root / f".{database.name}-app-version"
    private_file(marker, b"0.1.93")
    saved = old_settings(root)
    owner_key = secrets.token_hex(32).encode()
    private_file(root / "owner-session.secret", owner_key)
    device = "local-" + hashlib.sha256(OWNER.encode() + b"\0" + owner_key).hexdigest()[:24]
    native_token, bridge_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    binary = home / "codex"
    binary.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
    binary.chmod(0o700)
    for key, value in {"SUPYMEM_MODE": "local", "APP_ROLE": "api", "SUPYMEM_SINGLE_PROCESS": "1",
        "SUPYMEM_DESKTOP_INSTANCE_ID": "upgrade-test", "COMPANY_OS_WORKING_MEMORY_PATH": str(root / "memory.sqlite3"),
        "COMPANY_OS_DATA_DIR": str(root), "COMPANY_OS_DB_URL": f"sqlite:///{database}", "HOME": str(home),
        "COMPANY_OS_SECRETS_ENV": str(root / ".env"), "COMPANY_OS_URL": CORE, "CONSOLE_ORIGIN": CORE,
        "SUPYMEM_LOCAL_PROXY_SECRET_FILE": str(root / "owner-session.secret"), "BRIDGE_AUTH_OWNER": "native",
        "BRIDGE_NATIVE_TOKEN": native_token, "BRIDGE_TOKEN": bridge_token, "SUPYMEM_APP_VERSION": "1.0.0",
        "PATH": f"{home}{os.pathsep}{os.environ['PATH']}"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SUPYMEM_LOCAL_KEY_PIPE", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("The desktop upgrade contacted the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    with service_modules("company_os"):
        from app.core import local_env_import
        from app.db import local_sqlite
        from desktop_runtime import agent_status, bridge, maintenance

        monkeypatch.setattr(local_env_import, "repo_root", lambda: root)
        monkeypatch.setattr(agent_status, "provider_binary", lambda provider, path_env=None:
                            str(binary) if provider == "codex" else None)
        migrations = Config()
        migrations.set_main_option("script_location", str(Path(local_sqlite.__file__).parent / "migrations"))
        schema = historical_schema
        slug = old_database(database, migrations, schema, name, idea, device)
        old_bridge_receipts(idea, device)
        built = root / "workspaces" / slug / "index.html"
        built.parent.mkdir(parents=True)
        built.write_text(f"<title>{name}</title><h1>{idea}</h1>")
        log = root / "logs" / "core.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("Saved venture successfully.\n")
        assert not (root / "coding-tools").exists()
        preserved = {path: path.read_bytes() for path in (built, log, root / "local-settings.json", root / "local-key.bin")}
        maintenance._worker()
        from app.main import app

        with TestClient(app, base_url=CORE, client=("127.0.0.1", 0)) as core:
            def to_core(upstream):
                assert upstream.url.host == "127.0.0.1" and upstream.url.port == 3300
                forwarded = core.build_request(upstream.method, str(upstream.url), content=upstream.content)
                forwarded.headers.clear()
                forwarded.headers.update(upstream.headers.multi_items())
                reply = core.send(forwarded, auth=None, follow_redirects=False)
                return httpx.Response(reply.status_code, headers=reply.headers.multi_items(), content=reply.content)

            native_app = bridge.create_bridge_app(transport=httpx.MockTransport(to_core))
            with TestClient(native_app, base_url=BRIDGE, client=("127.0.0.1", 0),
                            headers={"X-Native-Token": native_token, "X-Bridge-Token": bridge_token}) as native:
                core.headers.update(local_owner_headers())
                yield SimpleNamespace(core=core, native=native, name=name, idea=idea, root=root, saved=saved,
                    built=built, database=database, marker=marker, migrations=migrations, schema=schema, preserved=preserved)


@pytest.mark.parametrize("historical_schema", [RELEASE_HEAD, "s062spend"], indirect=True)
def test_upgraded_installation_opens_without_losing_saved_work(installation, tmp_path):
    from app.core.local_secrets import get_secret
    from app.core.local_worker import is_running
    from app.db.local_sqlite import migrate_sqlite

    data = installation
    assert is_running()
    assert read(data.core, "/ready")["ready"] is True
    assert read(data.native, "/ready")["connected"] is True
    venture = f"/ventures/{VENTURE}"
    assert read(data.core, venture)["name"] == data.name
    read(data.core, "/ventures")
    read(data.core, venture + "/journey")
    idea = read(data.core, venture + f"/idea/sessions/{IDEA}")
    assert (idea["state"], idea["current_revision"]["summary"]) == ("confirmed", data.idea)
    read(data.core, venture + "/chat/conversations")
    history = read(data.core, venture + "/chat", params={"conversation_id": CONVERSATION})
    assert [item["body_md"] for item in history["items"]][:2] == [data.idea, f"Saved {data.name}."]
    assert read(data.core, "/ui-config")["coding_agents"]["consent"]["codex"] is True
    provider = read(data.core, "/local/providers")
    assert provider["values"]["provider"] == "openai" and provider["secrets_set"]["api_key"] is True
    for section in ("model", "search", "smtp"):
        response = provider if section == "model" else read(data.core, f"/local/settings/{section}")
        for field, value in data.saved[section].items():
            if get_secret(section, field) != value:
                pytest.fail(f"The old {section} credential no longer decrypts")
            if value in json.dumps(response):
                pytest.fail(f"The {section} settings API returned a credential")
        if section != "model":
            assert response["secrets_set"][field] is True
    for path in ("/local/settings/catalog", "/local/settings/backups", "/local/health", venture + "/pagebuilder/profile"):
        read(data.core, path)
    page = read(data.core, venture + f"/pagebuilder/jobs/{PAGE_JOB}")
    assert page["state"] == "failed" and page["retry"]["method"] == "POST"

    assert data.marker.read_text() == "1.0.0"
    assert rows(data.database, "SELECT version_num FROM alembic_version") == [
        (ScriptDirectory.from_config(data.migrations).get_current_head(),)]
    archives = list(data.root.with_name("data-backups").glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        snapshot = tmp_path / "before.sqlite3"
        snapshot.write_bytes(archive.extractfile(f"data/{data.database.name}").read())
        assert rows(snapshot, "SELECT version_num FROM alembic_version") == [(data.schema,)]
        assert rows(snapshot, "SELECT id FROM coding_agent_consent") == [("old-consent",)]
        for table in ("ventures", "idea_sessions", "idea_revisions", "chat_messages", "jobs", "venture_documents"):
            query = f"SELECT id FROM {table} WHERE " + ("id = ?" if table == "ventures" else "venture_id = ?")
            assert set(rows(snapshot, query, (VENTURE,))) <= set(rows(data.database, query, (VENTURE,)))
        assert {"data/bridge_outbox.sqlite", "data/local-settings.json", "data/logs/core.log",
                "data/" + data.built.relative_to(data.root).as_posix()} <= set(archive.getnames())
        assert "data/local-key.bin" not in archive.getnames()
    assert migrate_sqlite(f"sqlite:///{data.database}") is None
    assert list(data.root.with_name("data-backups").glob("*.tar.gz")) == archives
    for path, contents in data.preserved.items():
        if path.read_bytes() != contents:
            pytest.fail(f"The upgrade rewrote {path.name}")


def test_upgraded_orders_and_bridge_receipts_are_safe_to_open(installation):
    data = installation
    order_path = f"/ventures/{VENTURE}/device-work-orders"
    native_path = f"/native/device-work-orders/{VENTURE}"
    orders = {item["id"]: item for item in read(data.core, order_path)["orders"]}
    assert {key: value["state"] for key, value in orders.items()} == {
        ORDERS["finished"]: "stopped", ORDERS["stopped"]: "stopped", ORDERS["approved"]: "expired"}
    for label, order_id in ORDERS.items():
        assert orders[order_id]["review_path"].endswith(order_id)
        saved = read(data.core, f"{order_path}/{order_id}")
        assert bool(saved.get("claimed_at")) == (label != "approved")
        for detail in ("plan", "iterations", "events"):
            read(data.core, f"{order_path}/{order_id}/{detail}")
        status = read(data.native, f"{native_path}/{order_id}/status")
        assert status["state"] == {"finished": "stopped", "stopped": "stopped", "approved": "not_started"}[label]
        assert status["task_verified"] == (label == "finished")
        if label == "stopped":
            assert status["evidence"] == {"processes_stopped": True, "outcome": "stopped"}
    receipts = rows(data.root / "bridge_outbox.sqlite",
        "SELECT idempotency_key, state, response_status, last_error FROM scoped_outbox ORDER BY idempotency_key")
    assert receipts == [("delivered", "delivered", 202, None), ("interrupted", "uncertain", None, "interrupted_delivery")]
    assert len(read(data.native, "/outbox")["items"]) == 1
    for label in ("approved", "stopped"):
        response = data.core.post(f"{order_path}/{ORDERS[label]}/renew")
        assert response.status_code == 200, response.text
        retry = response.json()
        assert retry["id"] not in ORDERS.values() and retry["state"] == "approved" and not retry.get("claimed_at")


def test_upgrade_build_uses_prior_consent_without_connecting_again(installation):
    data = installation
    result = chat(data.core, VENTURE, f"Build an export.py utility script with Codex for {data.name}.")
    assert result["kind"] == "build_tool_started", result["kind"]
    order = result["actions"][0]["payload"]["order_id"]
    control = f"/native/device-work-orders/{VENTURE}/{order}"
    response = data.native.post(control + "/start")
    assert response.status_code == 200, response.text
    status = wait_for(lambda: read(data.native, control + "/status"),
                      lambda item: item.get("failure_code") or item["state"] in {"stopped", "reconciliation_required"})
    if status.get("failure_code") == "coding_provider_not_installed" and not (data.root / "coding-tools").exists():
        pytest.xfail(reason="needs pinmigrate")
    assert status["task_verified"], status
    assert (data.built.parent / "export.py").is_file()
    assert data.built.read_bytes() == data.preserved[data.built]
    assert rows(data.database, "SELECT id FROM coding_agent_consent") == [("old-consent",)]
