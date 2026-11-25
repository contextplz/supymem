"""Local maintenance keeps data private and leaves hosted deployments untouched."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture(params=("Warehouse replenishment", "Home orchid care"))
def local_cli(tmp_path, monkeypatch, request):
    root = tmp_path / "data"
    root.mkdir()
    database = root / "records.db"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(root))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(root / ".env"))
    monkeypatch.setenv("COS_WORKER_HEALTH_PATH", str(tmp_path / "worker.json"))
    for name in ("LLM_API_KEY", "COMPOSIO_API_KEY", "FIRECRAWL_SELF_HOSTED_URL",
                 "FIRECRAWL_SELF_HOSTED_TOKEN", "COMPANY_OS_VAULT_SECRET", "SESSION_SECRET",
                 "COMPANY_OS_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with service_modules("company_os"):
        module = importlib.import_module("app.cli_local")
        yield module, root, database, request.param


def seed(database, domain):
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes VALUES (?)", (domain,))


@pytest.mark.parametrize("replacement", ["zip", "backup", "newer-backup"])
def test_maintenance_refuses_downgrade_without_touching_queued_recovery(local_cli, monkeypatch, replacement):
    _cli, root, database, domain = local_cli
    from app.core import local_data
    from app.db.local_sqlite import migrate_sqlite

    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.0.0-rc.3")
    migrate_sqlite(f"sqlite:///{database}")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO ventures (id,name,raw_idea,stage,autonomy_level,created_at,updated_at) "
            "VALUES (?, ?, ?, 'idea', 'assisted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (domain, domain, domain),
        )
    if replacement == "zip":
        local_data.queue_import(local_data.export_data())
    else:
        local_data.queue_restore(local_data.create_backup())
    if replacement == "newer-backup":
        database.with_name(f".{database.name}-app-version").write_bytes(b"1.0.0-rc.2")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE ventures SET name = ?", (f"Updated {domain}",))

    def snapshot(folder):
        return {path.relative_to(folder): hashlib.sha256(path.read_bytes()).digest()
                for path in folder.rglob("*") if path.is_file()}

    pending = local_data._pending(root)
    saved, queued = snapshot(root), snapshot(pending)
    assert queued
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.0.0-rc.2")
    result = subprocess.run([sys.executable, "-m", "desktop_runtime", "--role", "core", "--maintenance"],
                            capture_output=True, text=True, timeout=30)

    assert result.returncode != 0
    assert "Update your app" in result.stderr
    assert snapshot(root) == saved
    assert snapshot(pending) == queued
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT name FROM ventures").fetchone() == (f"Updated {domain}",)


def test_automatic_backup_uses_saved_directory_for_creation_listing_and_restore(local_cli, monkeypatch):
    _cli, root, database, domain = local_cli
    from app.core import local_data, local_secrets
    from app.db.local_sqlite import migrate_sqlite

    seed(database, domain)
    destination = root.parent / "chosen-backups"
    local_secrets.update_settings("data", {"backup_directory": str(destination)}, {})
    archive = migrate_sqlite(f"sqlite:///{database}")
    assert archive.parent == destination
    assert archive.name in local_data.backups()
    with tarfile.open(archive) as contents:
        assert contents.getmember(f"data/{database.name}").isfile()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE notes SET body='later work'")
    local_data.queue_restore(archive.name)
    safety_backup = local_data.apply_pending()
    assert safety_backup.parent == destination
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    assert local_secrets.read_settings("data")["values"]["backup_directory"] == str(destination)

    def invalid_settings(section):
        from cryptography.exceptions import InvalidTag

        raise InvalidTag

    before = sorted(destination.glob("backup-*.tar.gz"))
    monkeypatch.setattr(local_secrets, "read_settings", invalid_settings)
    with pytest.raises(_cli.LocalMaintenanceError, match="choose your backup folder again"):
        _cli.backup()
    assert sorted(destination.glob("backup-*.tar.gz")) == before


def test_desktop_startup_backs_up_every_version_and_preserves_files(local_cli, monkeypatch):
    _cli, root, database, domain = local_cli
    seed(database, domain)
    (root / "uploads").mkdir()
    (root / "uploads" / "work.txt").write_text(domain)
    providers = root / "vault/providers"
    providers.mkdir(parents=True)
    (providers / "settings.json").write_text("{}")
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.0.0")
    from app.db.local_sqlite import migrate_sqlite

    first = migrate_sqlite(f"sqlite:///{database}")
    assert first and first.is_file()
    with tarfile.open(first) as contents:
        assert contents.extractfile("data/uploads/work.txt").read().decode() == domain
        before = root.parent / "before.db"
        before.write_bytes(contents.extractfile(f"data/{database.name}").read())
    with sqlite3.connect(before) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchall()
    assert migrate_sqlite(f"sqlite:///{database}") is None
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.0.1")
    from desktop_runtime import __main__ as runtime

    monkeypatch.setattr(runtime, "_load_asgi_app", lambda role: pytest.fail("Maintenance must finish before services start"))
    runtime.main(["--role", "core", "--maintenance", "--data-dir", str(root)])
    second = max(root.with_name(f"{root.name}-backups").glob("*.tar.gz"))
    assert second and second != first
    (root / "uploads" / "work.txt").write_text("later work")
    scope = hashlib.sha256(domain.encode()).hexdigest()
    (providers / "keychain.scope").write_text(scope)
    (providers / "keychain.scope").chmod(0o600)
    from app.core import local_data

    local_data.queue_restore(first.name)
    assert (root / "uploads" / "work.txt").read_text() == "later work"
    local_data.apply_pending()
    recovery = max(root.parent.glob(f"{root.name}-before-restore-*"))
    assert (root / "uploads" / "work.txt").read_text() == domain
    assert (providers / "keychain.scope").read_text() == scope
    assert (recovery / "uploads" / "work.txt").read_text() == "later work"


def test_restore_preserves_local_encryption_key_without_archiving_it(local_cli):
    cli, _root, database, domain = local_cli
    from app.core import local_secrets

    seed(database, domain)
    password = domain + " test-only password"
    local_secrets.update_settings("smtp", {}, {"password": password})
    archive = cli.backup()
    with tarfile.open(archive) as contents:
        assert "data/local-key.bin" not in contents.getnames()
        assert "data/local-settings.json" in contents.getnames()
    cli.restore(archive)
    assert local_secrets.get_secret("smtp", "password") == password


def test_desktop_export_import_round_trip_excludes_keys_and_preserves_recovery(local_cli, monkeypatch):
    import zipfile

    cli, root, database, domain = local_cli
    seed(database, domain)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE venture_resources (secrets_json TEXT)")
        connection.execute("INSERT INTO venture_resources VALUES (?)", (domain + " private key",))
    (root / "uploads").mkdir()
    (root / "uploads" / "work.txt").write_text(domain)
    (root / "uploads" / "work.txt").chmod(0o700)
    (root / ".env").write_text("fixture key")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(root / "provider.keys"))
    (root / "provider.keys").write_text(domain + " private provider key")
    from app.core import local_secrets

    local_secrets.update_settings("smtp", {}, {"password": domain + " private key"})
    (root / "vault").mkdir(exist_ok=True)
    (root / "vault" / "secrets.bin").write_bytes(b"fixture encrypted vault")
    from app.core import local_data

    archive = local_data.export_data()
    with zipfile.ZipFile(archive) as contents:
        assert {"manifest.json", f"data/{database.name}", "data/uploads/work.txt"} <= set(contents.namelist())
        assert not any("vault" in name or "local-key" in name or "local-settings.lock" in name or ".env" in name for name in contents.namelist())
        assert json.loads(contents.read("data/local-settings.json"))["secrets"] is None
        assert "data/provider.keys" not in contents.namelist()
        exported_database = root.parent / "exported.db"
        exported_database.write_bytes(contents.read(f"data/{database.name}"))
    with sqlite3.connect(exported_database) as connection:
        assert connection.execute("SELECT secrets_json FROM venture_resources").fetchall() == [(None,)]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT secrets_json FROM venture_resources").fetchall() == [(domain + " private key",)]
    (root / "uploads" / "work.txt").write_text("later work")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE notes SET body='later work'")
    local_data.queue_import(archive)
    assert (root / "uploads" / "work.txt").read_text() == "later work"
    local_data.apply_pending()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    assert (root / "uploads" / "work.txt").read_text() == domain
    assert (root / "uploads" / "work.txt").stat().st_mode & 0o777 == 0o700
    assert not (root / ".env").exists()
    assert not (root / "provider.keys").exists()
    assert (root / "vault" / "secrets.bin").read_bytes() == b"fixture encrypted vault"
    backups = list(root.with_name(f"{root.name}-backups").glob("*.tar.gz"))
    assert backups
    cli.restore(backups[-1])
    assert (root / "uploads" / "work.txt").read_text() == "later work"
    assert local_secrets.get_secret("smtp", "password") == domain + " private key"


@pytest.mark.parametrize("damage", ["traversal", "duplicate", "key", "link", "future", "wal"])
def test_desktop_import_rejects_unsafe_data_before_replacement(local_cli, damage):
    import zipfile

    from app.core import local_data

    cli, root, database, domain = local_cli
    seed(database, domain)
    (root / "work.txt").write_text(domain)
    if damage == "future":
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
            connection.execute("INSERT INTO alembic_version VALUES ('unknown_future_revision')")
    archive = local_data.export_data()
    if damage != "future":
        with zipfile.ZipFile(archive, "a") as contents:
            if damage == "link":
                member = zipfile.ZipInfo("data/link")
                member.external_attr = 0o120777 << 16
                contents.writestr(member, "../outside")
            else:
                path = {"traversal": "data/../../outside", "duplicate": "data/work.txt",
                        "key": "data/local-key.bin", "wal": f"data/{database.name}-wal"}[damage]
                contents.writestr(path, "unsafe fixture")
    with pytest.raises(cli.LocalMaintenanceError):
        local_data.queue_import(archive)
    assert (root / "work.txt").read_text() == domain
    assert not local_data._pending(root).exists()
    assert not (root.parent / "outside").exists()


def test_desktop_maintenance_refuses_a_running_writer(local_cli):
    _cli, root, database, domain = local_cli
    seed(database, domain)
    from app.core.local_process import process_lock
    from desktop_runtime import __main__ as runtime

    with process_lock(), pytest.raises(SystemExit, match="already open.*Close your other app window"):
        runtime.main(["--role", "core", "--maintenance", "--data-dir", str(root)])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchall()


@pytest.mark.parametrize("role", ("api", "worker"))
@pytest.mark.parametrize("command", ("restore", "upgrade"))
def test_maintenance_refuses_live_local_process_without_health(local_cli, monkeypatch, role, command):
    cli, root, database, domain = local_cli
    seed(database, domain)
    archive = cli.backup()
    from app import bootstrap, main, state
    from app.chat import rephrase
    from app.worker import main as worker

    monkeypatch.setattr(bootstrap, "bootstrap", list)
    monkeypatch.delenv("SUPYMEM_SINGLE_PROCESS", raising=False)

    async def drain():
        pass

    monkeypatch.setattr(rephrase, "drain", drain)

    async def exercise():
        ready, stop = asyncio.Event(), asyncio.Event()

        @contextmanager
        def session():
            yield SimpleNamespace(commit=lambda: None)

        def lease(*args):
            ready.set()
            return []

        async def scheduler(event):
            await event.wait()

        monkeypatch.setattr(state, "db", lambda: session)
        monkeypatch.setattr(worker, "install_default_handlers", lambda: None)
        monkeypatch.setattr(worker, "_recover_orphaned_builds", lambda: None)
        monkeypatch.setattr(worker, "enabled_kinds", list)
        monkeypatch.setattr(worker, "_lease_available", lease)
        monkeypatch.setattr(worker, "_heartbeat_loop", lambda *args: None)
        monkeypatch.setattr(worker, "_scheduler_loop", scheduler)
        monkeypatch.setattr(worker, "POLL_S", 0.01)

        async def api():
            async with main.lifespan(main.app):
                ready.set()
                await stop.wait()

        task = asyncio.create_task(api() if role == "api" else worker.run_worker(stop=stop))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            with pytest.raises(cli.LocalMaintenanceError, match="Stop your app"):
                cli.restore(archive) if command == "restore" else cli.upgrade()
            assert (root / database.name).exists()
            with sqlite3.connect(database) as connection:
                assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        cli.restore(archive)

    asyncio.run(exercise())


def test_backup_snapshots_secondary_sqlite_before_wal_checkpoint(local_cli, monkeypatch, tmp_path):
    cli, root, database, domain = local_cli
    seed(database, domain)
    secondary = root / "working-memory"
    writer = sqlite3.connect(secondary)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE notes (body TEXT NOT NULL)")
        writer.execute("INSERT INTO notes VALUES (?)", (domain,))
        writer.commit()
        add = tarfile.TarFile.add

        def checkpoint_after_copy(contents, path, arcname=None, **kwargs):
            result = add(contents, path, arcname=arcname, **kwargs)
            if arcname == "data/working-memory":
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return result

        monkeypatch.setattr(tarfile.TarFile, "add", checkpoint_after_copy)
        archive = cli.backup()
        with tarfile.open(archive) as contents:
            assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in contents.getnames())
            contents.extractall(tmp_path / "snapshot", filter="data")
        with sqlite3.connect(tmp_path / "snapshot/data/working-memory") as connection:
            assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    finally:
        writer.close()


def test_default_database_uses_common_data_root(local_cli, monkeypatch):
    cli, root, _, domain = local_cli
    monkeypatch.delenv("COMPANY_OS_DB_URL")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("SUPYMEM_DATA_DIR", raising=False)
    from app.db.local_sqlite import default_database_url
    from sqlalchemy.engine import make_url

    assert Path(make_url(default_database_url({"COMPANY_OS_DATA_DIR": str(root)})).database) == root / "database.sqlite3"
    resolved_root, database = cli._paths()
    assert resolved_root == root and database.parent == root
    seed(database, domain)
    assert cli.backup().is_file()


def test_backup_restore_round_trip_includes_wal_settings_and_uploads(local_cli):
    cli, root, database, domain = local_cli
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE notes (body TEXT NOT NULL)")
        writer.execute("INSERT INTO notes VALUES (?)", (domain,))
        writer.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
        from app.core import local_secrets

        local_secrets.update_settings("smtp", {"sender_name": domain}, {})
        (root / "uploads").mkdir()
        (root / "uploads" / "source.bin").write_bytes(domain.encode() + b"\x00\xff")
        (root / "empty").mkdir()
        archive = cli.backup()
        assert archive.parent != root
        assert archive.stat().st_mode & 0o077 == 0
        with tarfile.open(archive) as contents:
            assert f"data/{database.name}-wal" not in contents.getnames()
            assert f"data/{database.name}-shm" not in contents.getnames()
    finally:
        writer.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM notes")
    local_secrets.update_settings("smtp", {"sender_name": "changed"}, {})
    (root / "uploads" / "source.bin").write_bytes(b"changed")
    (root / "new.txt").write_text("saved after backup")
    recovery = cli.restore(archive)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    assert local_secrets.read_settings("smtp")["values"]["sender_name"] == domain
    assert (root / "uploads" / "source.bin").read_bytes() == domain.encode() + b"\x00\xff"
    assert (root / "empty").is_dir()
    assert not (root / "new.txt").exists()
    assert (recovery / "new.txt").read_text() == "saved after backup"


def test_upgrade_fresh_sqlite_backs_up_then_migrates_and_keeps_history(local_cli, capsys):
    cli, root, database, domain = local_cli
    database.touch()
    archive = cli.upgrade()
    assert archive.is_file()
    with tarfile.open(archive) as contents:
        snapshot = contents.extractfile(f"data/{database.name}").read()
    before = root.parent / "before.db"
    before.write_bytes(snapshot)
    with sqlite3.connect(before) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(Path(cli.__file__).parent / "db" / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [(head,)]
    seed(database, domain)
    second_archive = cli.upgrade()
    assert second_archive != archive
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    assert "backup" in capsys.readouterr().out.lower()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_startup_backup_skips_workspace_links_and_special_files(local_cli, monkeypatch):
    cli, root, database, domain = local_cli
    seed(database, domain)
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(root / "working_memory.sqlite3"))
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "not-in-backup.txt").write_text(domain)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    workspace = root / "workspaces" / "saved-project"
    (workspace / "node_modules/.bin").mkdir(parents=True)
    (workspace / "index.html").write_text(domain)
    (workspace / "node_modules/.bin/tool").symlink_to(outside / "not-in-backup.txt")
    (workspace / "linked-directory").symlink_to(outside, target_is_directory=True)
    os.mkfifo(workspace / "tool-pipe")

    from sqlalchemy import text

    from app import state

    with state.db()() as session:
        assert session.execute(text("SELECT body FROM notes")).scalar_one() == domain
    archive, = root.with_name("data-backups").glob("*.tar.gz")
    with tarfile.open(archive) as contents:
        manifest = json.load(contents.extractfile("manifest.json"))
        assert manifest["skipped"] == [
            "linked",
            "workspaces/saved-project/linked-directory",
            "workspaces/saved-project/node_modules/.bin/tool",
            "workspaces/saved-project/tool-pipe",
        ]
        names = contents.getnames()
        assert all(f"data/{name}" not in names for name in manifest["skipped"])
        assert not any("not-in-backup" in name for name in names)
        assert contents.extractfile("data/workspaces/saved-project/index.html").read().decode() == domain
    assert (workspace / "linked-directory").is_symlink()
    recovery = cli.restore(archive)
    assert (workspace / "index.html").read_text() == domain
    assert not (workspace / "linked-directory").is_symlink()
    assert (recovery / workspace.relative_to(root) / "linked-directory").is_symlink()


@pytest.mark.parametrize("suffix", ("", "-wal", "-shm"))
def test_startup_refuses_links_in_the_database_files(local_cli, suffix):
    cli, root, database, domain = local_cli
    linked = database.with_name(f"{database.name}{suffix}")
    target = root / "other.db"
    seed(target, domain)
    if suffix:
        seed(database, domain)
    linked.symlink_to(target)
    original = target.read_bytes()

    from app.db.local_sqlite import migrate_sqlite

    with pytest.raises(cli.LocalMaintenanceError, match="Your database or one of its companion files is a link or special file"):
        migrate_sqlite(f"sqlite:///{database}")
    with pytest.raises(cli.LocalMaintenanceError, match="Your database or one of its companion files is a link or special file"):
        cli.backup()
    assert target.read_bytes() == original
    assert not root.with_name("data-backups").exists()


@pytest.mark.parametrize("command", ("backup", "restore", "upgrade", "doctor"))
def test_hosted_commands_refuse_without_touching_data(local_cli, monkeypatch, capsys, command):
    cli, root, database, _domain = local_cli
    monkeypatch.delenv("SUPYMEM_MODE")
    arguments = [command, str(root / "missing.tar.gz")] if command == "restore" else [command]
    assert cli.main(arguments) == 1
    assert not database.exists()
    assert list(root.iterdir()) == []
    assert "local" in capsys.readouterr().err


@pytest.mark.parametrize("damage", ("traversal", "symlink", "hardlink", "duplicate", "alias_duplicate", "corrupt_database"))
def test_restore_rejects_unsafe_archive_before_touching_data(local_cli, damage):
    cli, root, database, domain = local_cli
    seed(database, domain)
    archive = cli.backup()
    malicious = root.parent / "unsafe.tar.gz"
    with tarfile.open(archive) as source, tarfile.open(malicious, "w:gz") as target:
        for member in source.getmembers():
            content = source.extractfile(member) if member.isfile() else None
            if damage == "corrupt_database" and member.name == f"data/{database.name}":
                content = io.BytesIO(b"not a database")
                member.size = len(content.getvalue())
            target.addfile(member, content)
        if damage != "corrupt_database":
            member = tarfile.TarInfo("data/../../escape" if damage == "traversal" else f"data/{database.name}")
            if damage == "alias_duplicate":
                member.name = f"data/./{database.name}"
            if damage in {"symlink", "hardlink"}:
                member.type = tarfile.SYMTYPE if damage == "symlink" else tarfile.LNKTYPE
                member.linkname = str(root.parent / "escape")
            target.addfile(member)
    with pytest.raises(cli.LocalMaintenanceError):
        cli.restore(malicious)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
    assert not (root.parent / "escape").exists()


@pytest.mark.parametrize("unsafe", ("symlink_output", "nested_output", "external_database", "external_settings"))
def test_backup_rejects_incomplete_or_exposing_layouts(local_cli, monkeypatch, unsafe):
    cli, root, database, domain = local_cli
    seed(database, domain)
    output = None
    if unsafe == "symlink_output":
        output = root.parent / "linked-backups"
        output.symlink_to(root.parent, target_is_directory=True)
    elif unsafe == "nested_output":
        output = root / "backups"
    elif unsafe == "external_database":
        monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{root.parent / 'outside.db'}")
    else:
        monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(root.parent / "outside.env"))
    with pytest.raises(cli.LocalMaintenanceError):
        cli.backup(output)
    assert list(root.parent.rglob("*.tar.gz")) == []


@pytest.mark.parametrize("rollback_fails", (False, True))
def test_restore_publish_failure_rolls_back_original_directory(local_cli, monkeypatch, rollback_fails):
    cli, root, database, domain = local_cli
    seed(database, domain)
    archive = cli.backup()
    (root / "later.txt").write_text(domain)
    replace = os.replace

    def fail_publish(source, destination):
        if Path(destination) == root and (Path(source).name == "data" or rollback_fails):
            raise OSError("fixture private detail")
        return replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", fail_publish)
    with pytest.raises(cli.LocalMaintenanceError) as error:
        cli.restore(archive)
    preserved = root
    if rollback_fails:
        preserved, = root.parent.glob("data-before-restore-*")
        assert str(preserved) in str(error.value)
        assert "Move" in str(error.value)
    assert (preserved / "later.txt").read_text() == domain
    with sqlite3.connect(preserved / database.name) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]


def test_upgrade_stops_when_backup_fails_and_cli_does_not_print_secrets(local_cli, monkeypatch, capsys):
    cli, _root, database, domain = local_cli
    seed(database, domain)

    def fail_backup(*args, **kwargs):
        raise OSError("fixture-private-key")

    monkeypatch.setattr(cli, "backup", fail_backup)
    assert cli.main(["upgrade"]) == 1
    captured = capsys.readouterr()
    assert "fixture-private-key" not in captured.out + captured.err
    assert "backup" in captured.err.lower()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchall() == []
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]


def test_doctor_checks_database_keys_reader_mail_and_worker_without_secret_output(local_cli, monkeypatch, capsys):
    cli, root, database, domain = local_cli
    seed(database, domain)
    monkeypatch.setattr(cli, "_doctor_ports", lambda: (True, "Your ports are free. Run make doctor again."), raising=False)
    monkeypatch.setattr(cli, "_command_ok", lambda command: False, raising=False)
    monkeypatch.setattr(cli, "_doctor_reader", lambda: (False, "Configure your page reader, then run make doctor."), raising=False)
    checks = cli.doctor()
    assert set(checks) == {"database", "keys", "reader", "mail", "worker", "docker", "compose", "ports", "env", "guessed_emails", "disk"}
    assert not checks["keys"][0]
    assert not checks["env"][0]
    assert checks["docker"][0] and checks["compose"][0]
    from app.health import expected_schema_revision
    from app.worker.health import write

    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (expected_schema_revision(),))
        connection.execute("CREATE TABLE gtm_email_accounts (status TEXT, connected_account_id TEXT)")
        connection.execute("INSERT INTO gtm_email_accounts VALUES ('active', ?)", (domain,))
    (root / ".env").write_text("LLM_API_KEY=fixture-private-key\nCOMPANY_OS_VAULT_SECRET=fixture-private-key\n")
    from app.core.providers_local import save_settings

    save_settings(provider="openai", base_url="https://api.openai.com/v1", api_key="fixture-private-key", models={"chat": "test-chat"})
    monkeypatch.setenv("COMPOSIO_API_KEY", "fixture-private-key")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://reader.example.invalid")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "fixture-private-key")
    monkeypatch.setattr(cli, "_doctor_reader", lambda: (True, "Your page reader is reachable. Run make doctor again."))
    write("running")
    assert cli.main(["doctor"]) == 0
    captured = capsys.readouterr()
    assert "fixture-private-key" not in captured.out + captured.err
    assert "Your model connection" in captured.out
    assert "Your saved settings" in captured.out
    assert "make doctor" in captured.out
    write("stopped")
    assert cli.main(["doctor"]) == 1


@pytest.mark.parametrize("available", (False, True))
def test_doctor_commands_discard_output_and_handle_missing_tools(local_cli, monkeypatch, available):
    cli, _root, _database, _domain = local_cli
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    import subprocess

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/" + name if available else None)
    assert cli._command_ok(["docker", "compose", "version"]) is available
    if available:
        assert calls[0][1]["stdout"] == subprocess.DEVNULL
        assert calls[0][1]["stderr"] == subprocess.DEVNULL
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("docker", 5)))
        assert not cli._command_ok(["docker", "compose", "version"])
    else:
        assert not calls


@pytest.mark.parametrize("owner", ("free", "desktop", "compose", "unrelated", "generic-health", "oversized", "malformed", "mismatched"))
def test_doctor_ports_only_accept_free_or_owned_listeners(local_cli, monkeypatch, owner):
    cli, _root, _database, domain = local_cli
    monkeypatch.setattr(cli, "_port_free", lambda port: owner == "free", raising=False)
    instance = domain

    @contextmanager
    def stream(client, method, url, **kwargs):
        assert method == "GET"
        assert client.trust_env is False
        body = {"status": "ok", "service": "company_os"}
        if owner in {"desktop", "mismatched"}:
            body["desktop_instance"] = instance
        if url.endswith("/api/health"):
            body = {"ok": True, **({"desktop_instance": instance} if owner == "desktop" else {})}
        if owner == "unrelated":
            body = {"status": "ok", "service": "other"}
        payload = b"invalid" if owner == "malformed" else json.dumps(body).encode()
        if owner == "oversized":
            payload += b" " * 16384
        yield SimpleNamespace(status_code=200, iter_bytes=lambda **kwargs: iter((payload,)))

    import httpx
    import subprocess

    monkeypatch.setattr(httpx.Client, "stream", stream)
    monkeypatch.setattr(cli, "_command_ok", lambda command: owner == "compose", raising=False)
    publishers = [{"PublishedPort": port, "Protocol": "tcp"} for port in (3100, 3300, 55432)]
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([{"Publishers": publishers}])))
    okay, message = cli._doctor_ports()
    assert okay is (owner in {"free", "desktop", "compose"})
    if owner == "desktop":
        assert "3100: used by your app" in message
        assert "3300: used by your app" in message
        assert "55432: used by another program" in message
    assert all(str(port) in message for port in (3100, 3300, 55432))
    assert "make doctor" in message


@pytest.mark.parametrize("reachable", (False, True))
def test_doctor_reader_defaults_to_builtin_and_probes_saved_firecrawl(local_cli, monkeypatch, reachable):
    cli, _root, _database, _domain = local_cli
    import httpx
    from app.core.local_secrets import update_settings

    calls = []

    @contextmanager
    def stream(client, method, url, **kwargs):
        assert method == "GET"
        calls.append(url)
        assert client.trust_env is False
        yield SimpleNamespace(status_code=200 if reachable else 503)

    monkeypatch.setattr(httpx.Client, "stream", stream)
    assert cli._doctor_reader()[0]
    assert not calls
    update_settings("search", {"provider": "firecrawl", "firecrawl_url": "http://127.0.0.1:3002"},
        {"firecrawl_key": "fixture-private-key"})
    okay, message = cli._doctor_reader()
    assert okay is reachable
    assert calls == ["http://127.0.0.1:3002/"]
    assert "fixture-private-key" not in message
    assert "fallback" not in message.lower()
    update_settings("capabilities", {"research": False}, {})
    okay, message = cli._doctor_reader()
    assert not okay and "Settings > Capabilities" in message
    assert calls == ["http://127.0.0.1:3002/"]


@pytest.mark.parametrize("setting", ("off", "invalid_url"))
def test_doctor_reader_keeps_owner_switches_and_url_validation(local_cli, monkeypatch, setting):
    cli, _root, _database, _domain = local_cli
    import httpx
    from app.core.local_secrets import update_settings

    monkeypatch.setattr(httpx.Client, "stream", lambda *args, **kwargs: pytest.fail("Do not probe a blocked reader"))
    if setting == "off":
        update_settings("capabilities", {"research": False}, {})
    else:
        update_settings("search", {"firecrawl_url": "http://reader.example.invalid"}, {})
    okay, message = cli._doctor_reader()
    assert not okay and "make doctor" in message
    assert ("Settings > Capabilities" if setting == "off" else "Keys and connections") in message


@pytest.mark.parametrize("mode", ("log", "smtp", "partial", "invalid"))
def test_doctor_email_reports_mode_without_sending(local_cli, monkeypatch, mode):
    cli, _root, _database, _domain = local_cli
    from app.gtm import local_mail
    from app.core.local_secrets import update_settings

    for name in ("SUPYMEM_SMTP_HOST", "SUPYMEM_SMTP_USERNAME", "SUPYMEM_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    update_settings("capabilities", {"send_email": True}, {})
    update_settings("smtp", {"host": "smtp.example.invalid" if mode != "log" else "",
        "user": "owner@example.invalid" if mode in {"smtp", "invalid"} else "",
        "port": "bad-port" if mode == "invalid" else 587},
        {"password": "fixture-private-key" if mode in {"smtp", "invalid"} else ""})
    monkeypatch.setattr(local_mail, "_smtp", lambda *args: pytest.fail("Doctor must not send mail"))
    okay, message = cli._doctor_email()
    assert okay is (mode in {"log", "smtp"})
    assert "fixture-private-key" not in message
    assert "make doctor" in message
    if mode in {"partial", "invalid"}:
        assert "Keys and connections" in message
        assert ("Mail app password" if mode == "partial" else "ports") in message


def test_doctor_unconfigured_mailbox_names_the_keys_screen(local_cli, monkeypatch):
    cli, root, _database, _domain = local_cli
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr("app.gtm.local_mail._smtp", lambda *args: pytest.fail("Doctor must not send mail"))
    ready, message = cli._doctor_email()
    assert not ready and "Keys and connections > Your mail account" in message
    assert all(field in message for field in ("Outgoing mail host", "Mailbox address", "Mail app password"))
    assert "No mail was sent" in message and "make doctor" in message
    assert not (root / "local-settings.json").exists()


@pytest.mark.parametrize("free_bytes,guessed", ((2 * 1024**3, "off"), (1024, "on")))
def test_doctor_first_run_checks_disk_settings_and_guessing_without_creating_data(local_cli, monkeypatch, free_bytes, guessed):
    cli, root, database, _domain = local_cli
    import shutil

    monkeypatch.setattr(cli, "_doctor_ports", lambda: (True, "Run make doctor."), raising=False)
    monkeypatch.setattr(cli, "_command_ok", lambda command: False, raising=False)
    monkeypatch.setattr(cli, "_doctor_reader", lambda: (False, "Run make doctor."), raising=False)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=free_bytes))
    monkeypatch.setenv("GUESSED_EMAILS", guessed)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "fixture-private-key")
    checks = cli.doctor()
    assert checks["disk"][0] is (free_bytes >= 1024**3)
    assert checks["guessed_emails"][0] is (guessed == "off")
    assert not checks["env"][0]
    assert not checks["database"][0]
    assert not database.exists()
    assert not list(root.iterdir())


@pytest.mark.parametrize("busy_family", (None, "ipv4", "ipv6"))
def test_doctor_port_bind_checks_ipv4_and_ipv6(local_cli, monkeypatch, busy_family):
    cli, _root, _database, _domain = local_cli
    import socket

    families = []

    @contextmanager
    def listener(family, kind):
        families.append(family)

        def bind(address):
            if busy_family == "ipv4" and family == socket.AF_INET or busy_family == "ipv6" and family == socket.AF_INET6:
                raise OSError("fixture-private-key")

        yield SimpleNamespace(bind=bind)

    monkeypatch.setattr(socket, "socket", listener)
    assert cli._port_free(3100) is (busy_family is None)
    assert socket.AF_INET in families
    if busy_family != "ipv4":
        assert socket.AF_INET6 in families


def test_doctor_script_delegates_without_exposing_configuration(local_cli, monkeypatch, capsys):
    cli, root, _database, domain = local_cli
    script = importlib.import_module("scripts.doctor")
    from scripts import dev

    calls = []

    def local_main(arguments):
        calls.append(arguments)
        return 1

    monkeypatch.setattr(script, "gather", lambda: pytest.fail("Your script must use the guarded local CLI"), raising=False)
    monkeypatch.setattr(dev, "_load_dotenv_into", lambda environment: None)
    monkeypatch.setattr(cli, "main", local_main)
    monkeypatch.setattr("sys.argv", ["doctor.py", "--data-dir", str(root / domain)])
    monkeypatch.setenv("LLM_API_KEY", "fixture-private-key")
    assert script.main() == 1
    assert calls == [["doctor", "--data-dir", str(root / domain)]]
    captured = capsys.readouterr()
    assert "fixture-private-key" not in captured.out + captured.err


def test_doctor_next_command_keeps_your_selected_data_folder(local_cli, monkeypatch, capsys):
    cli, root, _database, domain = local_cli
    selected = root / domain
    monkeypatch.setattr(cli, "doctor", lambda: {"env": (True, "Run make doctor.")})
    assert cli.main(["doctor", "--data-dir", str(selected)]) == 0
    assert str(selected) in capsys.readouterr().out
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    before = os.environ["COMPANY_OS_DATA_DIR"]
    assert cli.main(["doctor", "--data-dir", str(root / "other")]) == 1
    assert os.environ["COMPANY_OS_DATA_DIR"] == before


def test_doctor_defaults_to_builtin_reader_without_creating_settings(local_cli, monkeypatch):
    cli, root, _database, _domain = local_cli
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "fixture-private-key")
    monkeypatch.setattr(cli, "_doctor_ports", lambda: (True, "Run make doctor."))
    monkeypatch.setattr(cli, "_command_ok", lambda command: False)
    ready, message = cli.doctor()["reader"]
    assert ready and "built-in page reader" in message
    assert list(root.iterdir()) == []


def test_doctor_missing_database_does_not_create_it(local_cli, monkeypatch):
    cli, _root, database, _domain = local_cli
    monkeypatch.setattr(cli, "_doctor_ports", lambda: (True, "Run make doctor."))
    monkeypatch.setattr(cli, "_command_ok", lambda command: False)
    monkeypatch.setattr(cli, "_doctor_reader", lambda: (False, "Run make doctor."))
    assert not cli.doctor()["database"][0]
    assert not database.exists()


@pytest.mark.parametrize("command", ("restore", "upgrade"))
def test_changes_refuse_while_worker_is_running(local_cli, command):
    cli, _root, database, domain = local_cli
    seed(database, domain)
    archive = cli.backup()
    from app.worker.health import write

    write("running")
    with pytest.raises(cli.LocalMaintenanceError, match="Stop your app"):
        cli.restore(archive) if command == "restore" else cli.upgrade()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]



def test_seed_demo_then_delete_without_keys_or_network(local_cli, monkeypatch, capsys):
    import importlib
    import json
    import socket
    import sqlite3

    cli, _root, database, domain = local_cli

    def no_network(*args, **kwargs):
        raise AssertionError("Your demo must not contact a provider.")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    assert not database.exists()
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert cli.main(["seed-demo"]) == 1
    assert not database.exists()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    demo = importlib.import_module("app.demo_seed")
    sample = dict(demo.SAMPLE)
    sample.update(name=domain, idea=f"You help people with {domain.lower()}.",
                  summary=f"You offer a simple tool for {domain.lower()}.",
                  hypothesis=f"People who need {domain.lower()} will try your tool.",
                  action=f"You show a paper sketch to people who need {domain.lower()}.")
    monkeypatch.setattr(demo, "SAMPLE", sample)
    assert cli.main(["seed-demo"]) == 0
    output = capsys.readouterr().out
    assert "Demo" in output and "Delete venture" in output
    with sqlite3.connect(database) as connection:
        venture_id, workspace_id, name, idea, autonomy = connection.execute(
            "SELECT id, workspace_id, name, raw_idea, autonomy_level FROM ventures"
        ).fetchone()
        assert name == f"Demo: {domain}" and sample["idea"] in idea
        assert autonomy == "L0"
        owner_id, settings = connection.execute(
            "SELECT owner_id, settings_json FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        assert json.loads(settings)["demo"] is True
        assert connection.execute(
            "SELECT role FROM workspace_members WHERE workspace_id = ? AND person_id = ?",
            (workspace_id, owner_id),
        ).fetchall() == [("owner",)]
        assert connection.execute("SELECT summary FROM idea_revisions").fetchall() == [
            (f"Demo example: {sample['summary']}",)
        ]
        action, state, context, started = connection.execute(
            "SELECT action, state, context, started_at FROM experiments"
        ).fetchone()
        assert action == sample["action"] and state == "proposed" and started is None
        assert json.loads(context)["source"] == "demo"
        for table in ("jobs", "planned_tasks"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        connection.execute("CREATE TABLE notes (body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes VALUES (?)", (domain,))
    from app.experiments.ledger import list_for_venture
    from app.ventures import lifecycle

    assert len(list_for_venture(venture_id)) == 1
    deleted = lifecycle.soft_delete(venture_id, confirmation=lifecycle.confirmation_phrase(), actor_id=owner_id)
    assert deleted["recoverable"] is True
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM ventures WHERE archived_at IS NULL").fetchall() == []
        assert connection.execute(
            "SELECT id FROM workspaces WHERE id = ? AND archived_at IS NULL", (workspace_id,)
        ).fetchall() == []
        assert connection.execute("SELECT body FROM notes").fetchall() == [(domain,)]
