from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_cli_local import local_cli, seed
from tests.integration.test_postgres_auth_security import service_modules
from tests.company_os_harness import company_os_test_client


def _files(root):
    return {path.relative_to(root): hashlib.sha256(path.read_bytes()).digest() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def recovery_domain(local_cli):
    return "quartz-dispatch.test" if local_cli[3].startswith("Warehouse") else "puddle-kites.test"


@pytest.mark.parametrize("replacement", ["cli", "zip"])
def test_restore_keeps_new_opt_out(local_cli, recovery_domain, replacement):
    cli, root, database, _ = local_cli
    from app import state
    from app.autopilot.outreach import record_opt_out
    from app.core import local_data
    from app.db.local_sqlite import migrate_sqlite
    from app.db.models import GtmContact, Venture
    from app.gtm.relationships.suppression import email_suppressed
    from app.workspaces.resolve import _adopt_venture
    from core.clock import utcnow

    migrate_sqlite(f"sqlite:///{database}")
    address = f"hello@{recovery_domain}"
    now = utcnow()
    with state.db()() as session:
        venture = Venture(id=recovery_domain, name=recovery_domain, raw_idea=recovery_domain,
                          stage="draft", autonomy_level="L0", created_at=now, updated_at=now)
        session.add(venture)
        workspace_id = _adopt_venture(session, venture).id
        session.add(GtmContact(id="recipient", workspace_id=workspace_id, email=address, do_not_contact=0,
                               lawful_basis="consent", stage_changed_at=now, created_at=now))
        session.commit()
        assert not email_suppressed(session, workspace_id, session.get(GtmContact, "recipient"))

    archive = cli.backup() if replacement == "cli" else local_data.export_data()
    record_opt_out(workspace_id, "recipient", address)
    state._engine.dispose()
    state.reset_db_factory()
    if replacement == "cli":
        cli.restore(archive)
    else:
        local_data.queue_import(archive)
        local_data.apply_pending()
    with state.db()() as session:
        contact = session.get(GtmContact, "recipient")
        contact.email = f" HELLO+dispatch@{recovery_domain.upper()} "
        assert email_suppressed(session, workspace_id, contact)


def test_interrupted_restore_recovers_once(local_cli, recovery_domain, monkeypatch):
    cli, root, database, _ = local_cli
    from app.core import local_data

    seed(database, recovery_domain)
    archive = cli.backup()
    (root / "new-work.txt").write_text(recovery_domain)
    replace = os.replace

    def interrupt_original(source, destination):
        replace(source, destination)
        if source == root:
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_original)
        with pytest.raises(KeyboardInterrupt):
            cli.restore(archive)
    local_data.apply_pending()
    assert (root / "new-work.txt").read_text() == recovery_domain
    assert local_data.apply_pending() is None

    local_data.queue_restore(archive.name)

    def interrupt_publication(source, destination):
        replace(source, destination)
        if destination == root:
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_publication)
        with pytest.raises(KeyboardInterrupt):
            local_data.apply_pending()
    saved = set(root.parent.glob("data-before-restore-*"))
    (root / "work-after-restart.txt").write_text(recovery_domain)
    assert local_data.apply_pending() is None
    assert local_data.apply_pending() is None
    assert (root / "work-after-restart.txt").read_text() == recovery_domain
    assert set(root.parent.glob("data-before-restore-*")) == saved

    local_data.queue_restore(archive.name)
    delete_tree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(local_data._pending(root).name):
            raise OSError("simulated staged copy cleanup failure")
        return delete_tree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_cleanup)
        assert local_data.apply_pending() is not None
    assert local_data.apply_pending() is None
    assert not local_data._pending(root).exists()


def test_explicit_backup_keeps_legacy_wrapping_key_separate(local_cli, recovery_domain):
    cli, root, database, _ = local_cli
    from app.core.providers_local import _cipher

    seed(database, recovery_domain)
    providers = root / "vault/providers"
    providers.mkdir(parents=True)
    key_file = providers / "machine.secret"
    if os.name == "nt":
        pytest.skip("Legacy wrapping material lives in Windows Credential Manager")
    cipher = _cipher(providers, create=True)
    material = key_file.read_bytes()
    encrypted = cipher.encrypt(json.dumps({"provider": "openai", "providers": {
        "openai": {"models": {"chat": "local-legacy"}},
    }}).encode())
    (providers / "settings.enc").write_bytes(encrypted)
    archive = cli.backup(root.parent / "chosen")
    with tarfile.open(archive) as contents:
        assert "data/vault/providers/machine.secret" not in contents.getnames()
        assert contents.extractfile("data/vault/providers/settings.enc").read() == encrypted
    assert key_file.read_bytes() == material
    cli.restore(archive)
    assert key_file.read_bytes() == material


@pytest.mark.parametrize("settings_format", ["legacy", "current"])
def test_cross_install_restore_requires_matching_recovery_key(local_cli, recovery_domain, monkeypatch, settings_format):
    cli, source, database, _ = local_cli
    from app.core import local_secrets, providers_local

    if os.name == "nt":
        pytest.skip("This test exercises file-backed keys")
    monkeypatch.setattr(local_secrets, "_native_material", lambda: None)
    monkeypatch.delenv("SUPYMEM_LOCAL_KEY_PIPE", raising=False)
    destination = source.parent / recovery_domain
    destination.mkdir(mode=0o700)

    def save_settings(root, value):
        if settings_format == "legacy":
            folder = root / "vault/providers"
            folder.mkdir(parents=True)
            cipher = providers_local._cipher(folder, create=True)
            local_secrets._atomic_write(folder / "settings.enc", cipher.encrypt(json.dumps({
                "provider": "openai", "providers": {"openai": {"api_key": value}},
            }).encode()))
            return folder / "machine.secret"
        local_secrets.update_settings("smtp", {}, {"password": value})
        return root / "local-key.bin"

    seed(database, recovery_domain)
    source_key = save_settings(source, f"origin {recovery_domain}")
    archive = cli.backup(source.parent / "chosen")
    with tarfile.open(archive) as contents:
        assert f"data/{source_key.relative_to(source).as_posix()}" not in contents.getnames()
    target_database = destination / database.name
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(destination))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{target_database}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(destination / ".env"))
    seed(target_database, f"new work for {recovery_domain}")
    target_key = save_settings(destination, f"destination {recovery_domain}")
    before = _files(destination)

    with pytest.raises(cli.LocalMaintenanceError, match="matching.*recovery key.*Keys and connections"):
        cli.restore(archive)
    assert _files(destination) == before
    assert not list(source.parent.glob(f"{destination.name}-before-restore-*"))

    shutil.copy2(source_key, target_key)
    cli.restore(archive)
    with closing(sqlite3.connect(target_database)) as connection:
        assert connection.execute("SELECT body FROM notes").fetchone() == (recovery_domain,)
    actual = (providers_local._read_legacy(target_key.parent)["providers"]["openai"]["api_key"]
              if settings_format == "legacy" else local_secrets.get_secret("smtp", "password"))
    assert actual == f"origin {recovery_domain}"


def test_older_app_rejects_same_schema_zip_before_staging(local_cli, recovery_domain, monkeypatch):
    cli, root, database, _ = local_cli
    from app.core import local_data
    from app.db.local_sqlite import migrate_sqlite

    monkeypatch.setenv("SUPYMEM_APP_VERSION", "3.17.2")
    migrate_sqlite(f"sqlite:///{database}")
    archive = local_data.export_data()
    (root / "still-here.txt").write_text(recovery_domain)
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "3.17.1")
    with pytest.raises(cli.LocalMaintenanceError, match="Update your app"):
        local_data.queue_import(archive)
    assert not local_data._pending(root).exists()
    assert (root / "still-here.txt").read_text() == recovery_domain
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "3.17.2")
    local_data.queue_import(archive)
    database.with_name(f".{database.name}-app-version").write_text("3.17.1")
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "3.17.1")
    with pytest.raises(cli.LocalMaintenanceError, match="Update your app"):
        local_data.apply_pending()
    assert (root / "still-here.txt").read_text() == recovery_domain
    assert not list(root.parent.glob("data-before-restore-*"))


def test_stopped_app_without_launcher_refuses_newer_tar(local_cli, recovery_domain, monkeypatch):
    cli, root, database, _ = local_cli
    from app.db.local_sqlite import migrate_sqlite

    monkeypatch.setenv("SUPYMEM_APP_VERSION", "97.1.2")
    migrate_sqlite(f"sqlite:///{database}")
    archive = cli.backup()
    (root / "still-here.txt").write_text(recovery_domain)
    database.with_name(f".{database.name}-app-version").unlink()
    monkeypatch.delenv("SUPYMEM_APP_VERSION", raising=False)
    result = subprocess.run([os.sys.executable, "-m", "app.cli_local", "restore", str(archive)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Update your app" in result.stderr
    assert (root / "still-here.txt").read_text() == recovery_domain
    assert not list(root.parent.glob("data-before-restore-*"))


def test_nonportable_name_is_rejected_before_backup_export_or_staging(local_cli, recovery_domain):
    cli, root, database, _ = local_cli
    from app.core import local_data

    seed(database, recovery_domain)
    name = f"{recovery_domain}:notes.txt"
    if os.name == "nt":
        pytest.skip("Windows cannot create the source filename")
    archive = root.parent / "older-export.zip"
    with zipfile.ZipFile(local_data.export_data()) as exported, zipfile.ZipFile(archive, "w") as contents:
        for member in exported.infolist():
            data = exported.read(member)
            if member.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["files"][name] = hashlib.sha256(recovery_domain.encode()).hexdigest()
                data = json.dumps(manifest).encode()
            contents.writestr(member, data)
        contents.writestr(f"data/{name}", recovery_domain)
    with pytest.raises(cli.LocalMaintenanceError, match=name):
        local_data.queue_import(archive)
    assert not local_data._pending(root).exists()
    pending = local_data._pending(root)
    pending.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as contents:
        contents.extractall(pending)
    (pending / "request.json").write_text(json.dumps({"imported": True}))
    with pytest.raises(cli.LocalMaintenanceError, match=f"{name}.*cancel-restore"):
        local_data.apply_pending()
    assert cli.main(["cancel-restore"]) == 0
    assert not pending.exists()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT body FROM notes").fetchone() == (recovery_domain,)
    (root / name).write_text(recovery_domain)
    for create in (cli.backup, local_data.export_data):
        with pytest.raises(cli.LocalMaintenanceError, match=name):
            create()


def test_portable_names_have_one_spelling_across_filesystems(local_cli):
    cli, _, _, idea = local_cli
    domain = "crane-dispatch.test" if idea.startswith("Warehouse") else "seashell-sketches.test"
    components = {}
    cli._portable_name(f"data/{domain}/Notes.txt", components)
    cli._portable_name(f"data/{domain}/Other.txt", components)
    with pytest.raises(cli.LocalMaintenanceError, match="Keys and connections.*Your data and recovery"):
        cli._portable_name(f"data/{domain}/notes.txt", components)


def test_invalid_backup_folder_has_an_explicit_upgrade_recovery(local_cli, recovery_domain, monkeypatch, capsys):
    cli, root, database, _ = local_cli
    from app.core import local_data, local_secrets
    from app.db.local_sqlite import migrate_sqlite

    seed(database, recovery_domain)
    local_data.queue_restore(local_data.create_backup())
    local_secrets.update_settings("data", {"backup_directory": str(root / recovery_domain)}, {})
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "4.3.2")
    before = _files(root)
    pending = local_data._pending(root)
    queued = _files(pending)
    result = subprocess.run([sys.executable, "-m", "desktop_runtime", "--role", "core", "--maintenance"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "`python -m app.cli_local cancel-restore`, then reopen your app." in result.stderr
    assert _files(root) == before
    assert _files(pending) == queued
    assert cli.main(["cancel-restore"]) == 0

    with pytest.raises(cli.LocalMaintenanceError, match="Full backup folder path.*Your data folder and backup.*upgrade --output"):
        migrate_sqlite(f"sqlite:///{database}")
    default = root.with_name(f"{root.name}-backups")
    backups = set(default.glob("*.tar.gz"))
    assert cli.main(["upgrade", "--output", str(default)]) == 0
    assert len(set(default.glob("*.tar.gz")) - backups) == 1
    capsys.readouterr()


def test_restore_keeps_current_backup_location_and_lists_undo_copy(local_cli, recovery_domain):
    cli, root, database, _ = local_cli
    from app.core import local_data, local_secrets

    seed(database, recovery_domain)
    old = root.parent / "first-destination"
    new = root.parent / "second-destination"
    local_secrets.update_settings("data", {"backup_directory": str(old)}, {})
    local_secrets.update_settings("spend", {"monthly_spend_cap": "17"}, {})
    archive = cli.backup()
    local_secrets.update_settings("data", {"backup_directory": str(new)}, {})
    local_secrets.update_settings("spend", {"monthly_spend_cap": "42"}, {})
    new.mkdir(mode=0o700)
    shutil.move(archive, new / archive.name)
    (root / "before.txt").write_text(recovery_domain)
    local_data.queue_restore(archive.name)
    undo = local_data.apply_pending()
    assert undo.name in local_data.backups()
    assert undo.parent == new
    assert local_secrets.read_settings("spend")["values"]["monthly_spend_cap"] == "17"
    with tarfile.open(undo) as contents:
        assert contents.extractfile("data/before.txt").read().decode() == recovery_domain


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_backup_does_not_change_chosen_directory_mode(local_cli, recovery_domain):
    cli, root, database, _ = local_cli
    seed(database, recovery_domain)
    destination = root.parent / recovery_domain
    destination.mkdir(mode=0o700)
    destination.chmod(0o755)
    archive = cli.backup(destination)
    assert destination.stat().st_mode & 0o777 == 0o755
    assert archive.stat().st_mode & 0o077 == 0


def test_backup_and_export_report_link_omissions(local_cli, recovery_domain, monkeypatch):
    cli, root, database, _ = local_cli
    from app.core import local_data

    seed(database, recovery_domain)
    outside = root.parent / "outside.txt"
    outside.write_text(recovery_domain)
    linked = root / "linked.txt"
    linked.symlink_to(outside)
    open_file = cli._backup_file

    def reject_link_read(path):
        assert path.resolve() != outside
        return open_file(path)

    monkeypatch.setattr(cli, "_backup_file", reject_link_read)
    skipped = set()
    backup = cli.backup(skipped=skipped)
    assert skipped == {linked.name}
    with tarfile.open(backup) as contents:
        assert "data/linked.txt" not in contents.getnames()
    skipped.clear()
    with zipfile.ZipFile(local_data.export_data(skipped=skipped)) as contents:
        assert "data/linked.txt" not in contents.namelist()
    assert skipped == {linked.name}


def test_export_import_fresh_directory_restores_venture_and_starts(local_cli, monkeypatch):
    cli, root, database, domain = local_cli
    from app.core import local_data, local_secrets
    from app.db.models import ChatConversation, ChatMessage, GtmContact, SiteVersion, Venture, VentureDocument
    from app.db.supymem_models import Experiment
    from app import state
    from app.workspaces.resolve import _adopt_venture
    from core.clock import utcnow

    now = utcnow()
    document = root / "uploads" / "plan.txt"
    document.parent.mkdir()
    document.write_text(domain)
    page = root / "pages" / "site" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(f"<html><body>{domain}</body></html>")
    with state.db()() as session:
        venture = Venture(id="venture", name=domain, raw_idea=domain,
                          stage="draft", autonomy_level="L0", created_at=now, updated_at=now)
        session.add(venture)
        workspace = _adopt_venture(session, venture)
        workspace_id = workspace.id
        session.flush()
        session.add(ChatConversation(id="conversation", venture_id="venture", workspace_id=workspace_id,
                                     creator_person_id=workspace.owner_id, visibility="private", title=domain, creation_key="conversation",
                                     created_at=now, updated_at=now))
        session.flush()
        session.add_all([
            ChatMessage(id="message", venture_id="venture", workspace_id=workspace_id,
                        conversation_id="conversation", role="user", body_md=domain, created_at=now),
            GtmContact(id="contact", workspace_id=workspace_id, email="owner@example.test",
                       do_not_contact=1, lawful_basis="consent", stage_changed_at=now, created_at=now),
            Experiment(id="experiment", venture_id="venture", workspace_id=workspace_id,
                       kind="interview", hypothesis=domain, state="proposed"),
            VentureDocument(id="document", venture_id="venture", kind="plan", title=domain,
                            path=document.relative_to(root).as_posix(), body_md=domain,
                            created_at=now, updated_at=now),
            SiteVersion(id="site", workspace_id=workspace_id, venture_id="venture", version=1,
                        stage="first_look", template="landing", archetype="generic",
                        brandkit_hash=hashlib.sha256(domain.encode()).hexdigest(),
                        copy_hash=hashlib.sha256(domain.encode()).hexdigest(), path=str(page),
                        sha256=hashlib.sha256(page.read_bytes()).hexdigest(), created_at=now),
        ])
        session.commit()
    tables = ("ventures", "chat_conversations", "chat_messages", "gtm_contacts", "experiments", "venture_documents")
    with closing(sqlite3.connect(database)) as connection:
        before = {table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
    local_secrets.update_settings("smtp", {"host": "mail.example.test"}, {"password": domain + " saved key"})
    archive = local_data.export_data()
    state._engine.dispose()
    state.reset_db_factory()
    shutil.rmtree(root)
    moved = root.parent / "moved" / "data"
    moved.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(moved))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{moved / database.name}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(moved / ".env"))
    local_data.queue_import(archive)
    local_data.apply_pending()
    with state.db()() as session:
        assert session.get(Venture, "venture").name == domain
        restored_page = session.get(SiteVersion, "site")
        assert Path(restored_page.path).read_bytes() == (moved / page.relative_to(root)).read_bytes()
        assert restored_page.sha256 == hashlib.sha256(Path(restored_page.path).read_bytes()).hexdigest()
    with closing(sqlite3.connect(moved / database.name)) as connection:
        assert {table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables} == before
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert (moved / document.relative_to(root)).read_text() == domain
    assert (moved / page.relative_to(root)).read_text() == f"<html><body>{domain}</body></html>"
    assert local_secrets.get_secret("smtp", "password") is None
    with service_modules("company_os"):
        from app.main import app

        with company_os_test_client(app) as client:
            assert client.get("/health").status_code == 200
            response = client.get("/ventures/venture", headers={"X-Workspace-Id": workspace_id})
            assert response.status_code == 200
            assert response.json()["name"] == domain


def test_broken_current_database_refuses_to_discard_unreadable_opt_outs(local_cli, monkeypatch):
    cli, root, database, domain = local_cli
    from alembic import command
    from app.core import local_data, local_secrets
    from app.db.local_sqlite import migrate_sqlite

    seed(database, domain)
    (root / "plan.txt").write_text(domain)
    local_secrets.update_settings("smtp", {}, {"password": domain + " saved key"})

    def failed_migration(config, target):
        connection = config.attributes["connection"]
        connection.exec_driver_sql("UPDATE notes SET body='partial update'")
        connection.exec_driver_sql("CREATE TABLE partial_update(body TEXT)")
        raise RuntimeError("simulated migration failure")

    original_upgrade = command.upgrade
    monkeypatch.setattr(command, "upgrade", failed_migration)
    with pytest.raises((RuntimeError, cli.LocalMaintenanceError)):
        migrate_sqlite(f"sqlite:///{database}")
    assert len(local_data.backups()) == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='partial_update'").fetchall()
    database.write_bytes(b"broken after failed update")
    (root / "plan.txt").write_text("later data")
    local_secrets.update_settings("smtp", {}, {"password": "later saved key"})
    name = local_data.backups()[0]
    local_data.queue_restore(name)
    with pytest.raises(cli.LocalMaintenanceError, match="email opt-outs could not be carried over"):
        local_data.apply_pending()
    assert cli.main(["cancel-restore"]) == 0
    assert not local_data._pending(root).exists()
    assert database.read_bytes() == b"broken after failed update"
    assert (root / "plan.txt").read_text() == "later data"
    assert local_secrets.get_secret("smtp", "password") == "later saved key"
    assert not list(root.parent.glob("data-before-restore-*"))

    database.rename(root / "damaged.saved")
    local_data.queue_restore(name)
    with pytest.raises(cli.LocalMaintenanceError, match="email opt-outs could not be carried over"):
        local_data.apply_pending()
    assert cli.main(["cancel-restore"]) == 0
    assert (root / "damaged.saved").read_bytes() == b"broken after failed update"
    assert not database.exists()

    recovered = root.parent / "recovered-data"
    archive = cli._backup_directory(root) / name
    recovered_database = recovered / database.name
    recovery_key = root / "local-key.bin"
    if recovery_key.exists():
        recovered.mkdir(mode=0o700)
        shutil.copy2(recovery_key, recovered / recovery_key.name)
    cli._restore(archive, recovered, recovered_database)
    with closing(sqlite3.connect(recovered_database)) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    assert (recovered / "plan.txt").read_text() == domain
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(recovered))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{recovered_database}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(recovered / ".env"))
    assert local_secrets.get_secret("smtp", "password") == domain + " saved key"
    monkeypatch.setattr(command, "upgrade", original_upgrade)
    assert migrate_sqlite(f"sqlite:///{recovered_database}") is not None
    with closing(sqlite3.connect(recovered_database)) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='ventures'").fetchone()


@pytest.mark.parametrize("damage", ["truncated", "changed_file", "older_format", "older_schema"])
def test_invalid_import_changes_nothing(local_cli, damage):
    cli, root, database, domain = local_cli
    from app.core import local_data

    seed(database, domain)
    (root / "work.txt").write_text(domain)
    if damage == "older_schema":
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(Path(cli.__file__).parent / "db/migrations"))
        revision = ScriptDirectory.from_config(config).get_base()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
            connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
            connection.commit()
    archive = local_data.export_data()
    before = _files(root)
    if damage == "truncated":
        archive.write_bytes(archive.read_bytes()[:32])
    else:
        with zipfile.ZipFile(archive) as source:
            entries = [(entry, source.read(entry)) for entry in source.infolist()]
        with zipfile.ZipFile(archive, "w") as output:
            for entry, content in entries:
                if damage == "changed_file" and entry.filename == "data/work.txt":
                    content = b"corrupted document with a valid ZIP checksum"
                elif damage == "older_format" and entry.filename == "manifest.json":
                    content = json.dumps({"version": 1, "database": database.name}).encode()
                output.writestr(entry, content)
    with pytest.raises(cli.LocalMaintenanceError, match="(intact|version|export again|Update|update)"):
        local_data.queue_import(archive)
    assert _files(root) == before
    assert not local_data._pending(root).exists()
    assert not list(root.parent.glob("data-before-restore-*"))


def test_export_snapshots_live_wal_transactions(local_cli):
    cli, root, database, domain = local_cli
    from app.core import local_data

    seed(database, domain)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE pairs(sequence INTEGER, body TEXT)")
    ready = threading.Event()
    stop = threading.Event()
    errors = []

    def write():
        try:
            with closing(sqlite3.connect(database, timeout=10)) as connection:
                connection.execute("PRAGMA wal_autocheckpoint=0")
                sequence = 0
                while not stop.is_set():
                    with connection:
                        connection.executemany("INSERT INTO pairs VALUES (?, ?)", [(sequence, domain)] * 2)
                    sequence += 1
                    ready.set()
        except Exception as error:
            errors.append(error)
            ready.set()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert ready.wait(10)
        archive = local_data.export_data()
        assert writer.is_alive()
    finally:
        stop.set()
        writer.join(10)
    assert not writer.is_alive() and not errors
    with zipfile.ZipFile(archive) as source:
        assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in source.namelist())
        snapshot = root.parent / "snapshot.db"
        snapshot.write_bytes(source.read(f"data/{database.name}"))
    with closing(sqlite3.connect(snapshot)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] >= 2
        assert not connection.execute("SELECT sequence FROM pairs GROUP BY sequence HAVING COUNT(*) != 2").fetchall()


def test_all_archives_keep_saved_keys_encrypted_and_modes_private(local_cli):
    cli, root, database, domain = local_cli
    from app.core import local_data, local_secrets

    seed(database, domain)
    secret = domain + " private credential"
    local_secrets.update_settings("smtp", {}, {"password": secret})
    (root / ".env").write_text("SUPYMEM_SMTP_PASSWORD=" + secret)
    (root / "vault").mkdir(exist_ok=True)
    (root / "vault/secrets.json").write_text(json.dumps({"values": {"legacy": secret}}))
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE venture_resources(secrets_json TEXT)")
        connection.execute("INSERT INTO venture_resources VALUES (?)", (secret,))
        connection.commit()
    destination = root.parent / "backups"
    destination.mkdir(mode=0o700)
    destination.chmod(0o755)
    backup = cli.backup(destination)
    exported = local_data.export_data()
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o755
    for archive in (backup, exported):
        assert archive.stat().st_mode & 0o077 == 0
    with tarfile.open(backup) as source:
        assert "data/local-settings.json" in source.getnames()
        for entry in source:
            assert entry.mode & 0o077 == 0
            if entry.isfile():
                assert secret.encode() not in source.extractfile(entry).read()
    with zipfile.ZipFile(exported) as source:
        for entry in source.infolist():
            assert (entry.external_attr >> 16) & 0o077 == 0
            assert secret.encode() not in source.read(entry)
    assert local_secrets.get_secret("smtp", "password") == secret


def test_damaged_backup_file_is_refused_without_replacing_data(local_cli):
    cli, root, database, domain = local_cli
    seed(database, domain)
    (root / "document.txt").write_text(domain)
    archive = cli.backup()
    corrupted = root.parent / "corrupted.tar.gz"
    with tarfile.open(archive) as source, tarfile.open(corrupted, "w:gz") as output:
        for entry in source:
            stream = source.extractfile(entry) if entry.isfile() else None
            if entry.name == "data/document.txt":
                stream = io.BytesIO(b"broken")
                entry.size = 6
            output.addfile(entry, stream)
    before = _files(root)
    with pytest.raises(cli.LocalMaintenanceError, match="intact"):
        cli.restore(corrupted)
    assert _files(root) == before


@pytest.mark.parametrize("platform,location", [
    ("darwin", "Library/Application Support"),
    ("linux", ".local/share"),
    ("win32", "AppData/Local"),
])
def test_data_directory_matches_os_and_explicit_moves(local_cli, platform, location):
    cli, root, _, _domain = local_cli
    config = json.loads((Path(cli.__file__).parents[3] / "src-tauri/tauri.local.json").read_text())
    name = config["productName"]
    home = root.parent / "home"
    environment = {"HOME": str(home), "USERPROFILE": str(home)}
    assert cli.local_data_directory(name, environment, platform) == home / location / name
    custom = root.parent / "custom"
    for variable in ("COMPANY_OS_DATA_DIR", "SUPYMEM_DATA_DIR"):
        assert cli.local_data_directory(name, {**environment, variable: str(custom)}, platform) == custom
    if platform == "linux":
        assert cli.local_data_directory(name, {**environment, "XDG_DATA_HOME": str(custom)}, platform) == custom / name
        assert cli.local_data_directory(name, {**environment, "XDG_DATA_HOME": "relative"}, platform) == home / location / name
    if platform == "win32":
        assert cli.local_data_directory(name, {**environment, "LOCALAPPDATA": str(custom)}, platform) == custom / name


def test_moving_data_directory_preserves_saved_keys(local_cli, monkeypatch):
    cli, root, database, domain = local_cli
    from app.core import local_secrets

    seed(database, domain)
    local_secrets.update_settings("smtp", {"host": "mail.example.test"}, {"password": domain})
    moved = root.parent / "another-location"
    shutil.move(root, moved)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(moved))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{moved / database.name}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(moved / ".env"))
    assert local_secrets.get_secret("smtp", "password") == domain
    archive = cli.backup()
    assert archive.parent == moved.with_name(f"{moved.name}-backups")
    with closing(sqlite3.connect(moved / database.name)) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]


def test_changed_staged_import_never_backs_up_or_replaces_data(local_cli):
    cli, root, database, domain = local_cli
    from app.core import local_data

    seed(database, domain)
    (root / "document.txt").write_text(domain)
    local_data.queue_import(local_data.export_data())
    before = _files(root)
    (local_data._pending(root) / "data/document.txt").write_text("damaged staged data")
    with pytest.raises(cli.LocalMaintenanceError, match="intact archive.*cancel-restore"):
        local_data.apply_pending()
    assert _files(root) == before
    assert local_data.backups() == []
    assert not list(root.parent.glob("data-before-restore-*"))


def test_export_preserves_non_secret_settings_without_keys(local_cli):
    cli, root, database, domain = local_cli
    from app.core import local_data, local_secrets

    seed(database, domain)
    local_secrets.update_settings("smtp", {"host": "mail.example.test", "sender_name": domain}, {"password": domain})
    local_secrets.update_settings("capabilities", {"send_email": False}, {})
    archive = local_data.export_data()
    shutil.rmtree(root)
    root.mkdir(mode=0o700)
    local_data.queue_import(archive)
    local_data.apply_pending()
    assert local_secrets.read_settings("smtp")["values"] == {"host": "mail.example.test", "sender_name": domain}
    assert local_secrets.read_settings("capabilities")["values"] == {"send_email": False}
    assert local_secrets.get_secret("smtp", "password") is None


def test_zip_replaces_public_settings_but_keeps_destination_keys(local_cli, monkeypatch):
    _, root, database, idea = local_cli
    from app.core import local_data, local_secrets

    domain = "kiln-logistics.test" if idea.startswith("Warehouse") else "lantern-hobbies.test"
    seed(database, domain)
    destination_key = domain + "-mail-key"
    local_secrets.update_settings("spend", {"monthly_spend_cap": "31.50"}, {}, source="env")
    local_secrets.update_settings("smtp", {"sender_name": domain}, {"password": destination_key}, source="env")
    archive = local_data.export_data()
    with zipfile.ZipFile(archive) as contents:
        saved = contents.read("data/local-settings.json")
        assert destination_key.encode() not in saved and json.loads(saved)["secrets"] is None
    local_secrets.update_settings("spend", {"monthly_spend_cap": "82.00"}, {})
    local_secrets.update_settings("smtp", {"sender_name": "Current sender", "booking_link": f"https://{domain}/meet"}, {})

    with monkeypatch.context() as patch:
        patch.setattr(local_secrets, "_shell_material", lambda: None)
        local_data.queue_import(archive)
        local_data.apply_pending()

    assert local_secrets.read_settings("spend")["values"] == {"monthly_spend_cap": "31.50"}
    assert local_secrets.read_settings("spend")["sources"] == {"monthly_spend_cap": "env"}
    assert local_secrets.read_settings("smtp")["values"] == {"sender_name": domain}
    assert local_secrets.read_settings("smtp")["sources"] == {"sender_name": "env", "password": "env"}
    assert local_secrets.get_secret("smtp", "password") == destination_key

    local_secrets.update_settings("smtp", {"host": domain}, {"password": destination_key})
    with pytest.raises(local_data.LocalMaintenanceError, match="Keys and connections.*Your mail account"):
        local_data.queue_import(archive)
    assert not local_data._pending(root).exists()


def test_data_zip_round_trips_model_context_window_without_key(local_cli):
    _, root, database, domain = local_cli
    from app.core import local_data, local_secrets, providers_local

    seed(database, domain)
    providers_local.save_settings(provider="openai_compatible", models={"chat": domain.lower().replace(" ", "-")},
                                  base_url="http://localhost:11434/v1", api_key="fixture-private", context_window=65536)
    archive = local_data.export_data()
    with zipfile.ZipFile(archive) as exported:
        settings = json.loads(exported.read("data/local-settings.json"))
        assert settings["secrets"] is None
        assert settings["values"]["model"]["sources"]["openai_compatible.context_window"] == "app"
        assert "api_key" not in json.dumps(settings)
    shutil.rmtree(root)
    root.mkdir(mode=0o700)
    local_data.queue_import(archive)
    local_data.apply_pending()
    restored = local_secrets.read_settings("model")
    assert restored["values"]["providers"]["openai_compatible"]["context_window"] == 65536
    assert restored["sources"]["openai_compatible.context_window"] == "app"


def test_data_zip_exports_azure_model_id(local_cli, monkeypatch):
    _, root, database, idea = local_cli
    from app.core import local_data, local_secrets, providers_local

    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "zip-test")
    domain = "freight-planning.test" if idea.startswith("Warehouse") else "orchid-notes.invalid"
    seed(database, domain)
    model_id = domain.split(".")[0] + "-model"
    key = domain + "-zip-private"
    providers_local.save_settings(provider="azure_openai", base_url=f"https://{domain}",
                                  models={"chat": "deployment"}, catalog_model=model_id, api_key=key)

    archive = local_data.export_data()
    with zipfile.ZipFile(archive) as exported:
        contains_key = any(key.encode() in exported.read(name) for name in exported.namelist())
        assert not contains_key
        settings = json.loads(exported.read("data/local-settings.json"))
        assert settings["secrets"] is None
        assert "azure_openai.api_key" not in settings["values"]["model"]["sources"]

    shutil.rmtree(root)
    root.mkdir(mode=0o700)
    local_data.queue_import(archive)
    local_data.apply_pending()
    restored = local_secrets.read_settings("model")
    assert restored["values"]["providers"]["azure_openai"]["catalog_model"] == model_id
    assert restored["sources"]["azure_openai.catalog_model"] == "app"
    assert not restored["secrets_set"].get("azure_openai")


def test_data_zip_preserves_ai_key_source(local_cli, monkeypatch):
    _, root, database, idea = local_cli
    from app.core import local_data, local_secrets, providers_local
    from app.core.local_env_import import import_local_env

    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "zip-test")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    domain = "inventory-ledger.invalid" if idea.startswith("Warehouse") else "birdwatch-journal.test"
    seed(database, domain)
    base_url = f"https://{domain}/v1"
    app_key = domain + "-app-private"
    env_key = domain + "-env-private"
    providers_local.save_settings(provider="openai_compatible", base_url=base_url,
                                  models={"chat": "test-model"}, api_key=app_key)

    archive = local_data.export_data()
    with zipfile.ZipFile(archive) as exported:
        settings = json.loads(exported.read("data/local-settings.json"))
        assert "openai_compatible.api_key" not in settings["values"]["model"]["sources"]
    local_data.queue_import(archive)
    local_data.apply_pending()

    env_file = root / ".env"
    env_file.touch(mode=0o600)
    env_file.write_text(f"OPENAI_COMPATIBLE_BASE_URL={base_url}\nOPENAI_COMPATIBLE_API_KEY={env_key}\n")
    import_local_env(strict=True)
    kept_app_key = local_secrets.get_secret("model", "openai_compatible") == app_key
    assert kept_app_key
    assert local_secrets.read_settings("model")["sources"]["openai_compatible.api_key"] == "app"

    providers_local.save_settings(provider="openai_compatible", base_url=f"https://other.{domain}/v1", api_key=app_key)
    with pytest.raises(local_data.LocalMaintenanceError, match="Keys and connections.*Your AI provider"):
        local_data.queue_import(archive)
    assert not local_data._pending(root).exists()


@pytest.mark.parametrize("transfer", ["import", "restore"])
def test_browser_restart_applies_pending_recovery_before_starting_services(local_cli, monkeypatch, transfer):
    _, root, database, domain = local_cli
    from app.core import local_data
    from tests.unit.test_local_startup import load_launcher

    seed(database, domain)
    if transfer == "import":
        local_data.queue_import(local_data.export_data())
    else:
        local_data.queue_restore(local_data.create_backup())
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE notes SET body = 'changed after backup'")

    launcher = load_launcher(root.parent)
    children = []

    def start_child(command, **kwargs):
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM notes").fetchone() == (domain,)
        children.append(command)
        return object()

    monkeypatch.setitem(launcher, "subprocess", SimpleNamespace(run=subprocess.run, Popen=start_child))
    monkeypatch.setitem(launcher, "supervise", lambda children: 0)
    backend = {**os.environ, "CORE_PORT": "3300", "BRIDGE_PORT": "3500", "CONSOLE_PORT": "3100"}
    monkeypatch.setitem(launcher, "local_environment", lambda root: (backend, {}))
    monkeypatch.setattr(launcher["sys"], "argv", ["start", "--web", "pnpm"])
    assert launcher["main"]() == 0
    assert len(children) == 4
    assert not local_data._pending(root).exists()
    assert len(local_data.backups()) >= (2 if transfer == "restore" else 1)


@pytest.mark.parametrize("damage", ["encrypted_keys", "plain_key_in_values", "secret_source", "invalid_source"])
def test_import_refuses_keys_disguised_as_public_settings(local_cli, damage):
    cli, root, database, domain = local_cli
    from app.core import local_data, local_secrets

    seed(database, domain)
    local_secrets.update_settings("smtp", {"host": "mail.example.test"}, {})
    archive = local_data.export_data()
    before = _files(root)
    with zipfile.ZipFile(archive) as source:
        entries = [(entry, source.read(entry)) for entry in source.infolist()]
    content = {entry.filename: body for entry, body in entries}
    payload = json.loads(content["data/local-settings.json"])
    secret = domain + " private key"
    if damage == "encrypted_keys":
        payload["secrets"] = secret
    elif damage == "plain_key_in_values":
        payload["values"]["smtp"]["password"] = secret
    elif damage == "secret_source":
        payload["sources"]["smtp"]["password"] = "app"
    else:
        payload["sources"]["smtp"]["host"] = secret
    content["data/local-settings.json"] = json.dumps(payload).encode()
    manifest = json.loads(content["manifest.json"])
    manifest["files"]["local-settings.json"] = hashlib.sha256(content["data/local-settings.json"]).hexdigest()
    content["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, "w") as output:
        for entry, _body in entries:
            output.writestr(entry, content[entry.filename])
    with pytest.raises(cli.LocalMaintenanceError, match="intact") as error:
        local_data.queue_import(archive)
    assert secret not in str(error.value)
    assert _files(root) == before
    assert not local_data._pending(root).exists()


@pytest.mark.parametrize("transfer", ("import", "restore"))
def test_data_transfer_relocates_stored_page_and_document_paths(local_cli, monkeypatch, transfer):
    cli, root, database, domain = local_cli
    from app.core import local_data

    seed(database, domain)
    page = root / "sites/site/index.html"
    page.parent.mkdir(parents=True)
    page.write_text(f"<html><body>{domain}</body></html>")
    document = root / "uploads/document.txt"
    document.parent.mkdir()
    document.write_text(domain)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE site_versions(path TEXT, screenshot_path TEXT)")
        connection.execute("INSERT INTO site_versions VALUES (?, NULL)", (str(page),))
        connection.execute("CREATE TABLE venture_documents(path TEXT, body_md TEXT)")
        connection.execute("INSERT INTO venture_documents VALUES (?, ?)", (str(document), str(page)))
        connection.commit()
    archive = local_data.export_data() if transfer == "import" else cli.backup(root.parent / "relocation-backups")
    shutil.rmtree(root)
    moved = root.parent / "restored"
    moved.mkdir(mode=0o700)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(moved))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{moved / database.name}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(moved / ".env"))
    if transfer == "import":
        local_data.queue_import(archive)
        local_data.apply_pending()
    else:
        cli.restore(archive)
    with closing(sqlite3.connect(moved / database.name)) as connection:
        restored_page = connection.execute("SELECT path FROM site_versions").fetchone()[0]
        restored_document, body = connection.execute("SELECT path, body_md FROM venture_documents").fetchone()
    assert Path(restored_page).read_text() == f"<html><body>{domain}</body></html>"
    assert Path(restored_document).read_text() == domain
    assert body == str(page)


def test_import_refuses_escaping_stored_file_locations(local_cli):
    cli, root, database, domain = local_cli
    from app.core import local_data

    seed(database, domain)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE site_versions(path TEXT)")
        connection.execute("INSERT INTO site_versions VALUES (?)", (str(root / "../outside.html"),))
    archive = local_data.export_data()
    before = _files(root)
    with pytest.raises(cli.LocalMaintenanceError, match="restore an intact full backup"):
        local_data.queue_import(archive)
    assert _files(root) == before
    assert not local_data._pending(root).exists()


def test_moved_app_data_relocates_paths_before_starting(local_cli, monkeypatch):
    _cli, root, database, domain = local_cli
    from app import state
    from app.db.models import VentureDocument
    from app.db.local_sqlite import migrate_sqlite
    from core.clock import utcnow

    document = root / "uploads/document.txt"
    document.parent.mkdir()
    document.write_text(domain)
    now = utcnow()
    with state.db()() as session:
        session.add(VentureDocument(id="document", venture_id="venture", kind="plan", title=domain,
                                   path=str(document), body_md=domain, created_at=now, updated_at=now))
        session.commit()
    state._engine.dispose()
    state.reset_db_factory()
    moved = root.parent / "moved"
    shutil.move(root, moved)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(moved))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{moved / database.name}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(moved / ".env"))
    assert migrate_sqlite(f"sqlite:///{moved / database.name}") is not None
    with closing(sqlite3.connect(moved / database.name)) as connection:
        saved_path = connection.execute("SELECT path FROM venture_documents").fetchone()[0]
    assert Path(saved_path).read_text() == domain
