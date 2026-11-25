"""Durable local upgrades from historical SQLite schemas."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import shutil
import sqlite3
import sys
import tarfile
import time
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture(params=("Machine inspection scheduling", "Neighborhood recipe journals"))
def installation(tmp_path, monkeypatch, request):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    database = root / "database.sqlite3"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(root))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{database}")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(root / ".env"))
    monkeypatch.delenv("SUPYMEM_APP_VERSION", raising=False)
    with service_modules("company_os"):
        runner = importlib.import_module("app.db.local_sqlite")
        config = Config()
        config.set_main_option("script_location", str(Path(runner.__file__).parent / "migrations"))
        yield runner, config, root, database, request.param


def historical_database(config, database, revision, domain):
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO ventures (id,name,raw_idea,stage,autonomy_level,created_at,updated_at) "
                "VALUES (?,?,?,'idea','assisted',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (domain, domain, domain),
            )
            connection.exec_driver_sql(
                "INSERT INTO event_log (id,topic,venture_id,actor,payload_json,ts) "
                "VALUES (?,'saved',?,'owner',?,CURRENT_TIMESTAMP)",
                (domain, domain, '{"domain": "' + domain + '"}'),
            )
    finally:
        engine.dispose()
        config.attributes.pop("connection", None)
    database.chmod(0o600)


def rows(database, statement):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        return connection.execute(statement).fetchall()


@pytest.mark.parametrize("revision", ["bb8ea93e5a0f", "a1b2c3d4e5f6", "s001supymem", "s027usererrors", "s061codingconsent", pytest.param("s063routines", id="first-local-schema")])
def test_historical_schemas_upgrade_with_saved_work_and_history(installation, monkeypatch, revision):
    runner, config, root, database, domain = installation
    historical_database(config, database, revision, domain)
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "1.0.0")
    (root / "uploads").mkdir()
    (root / "uploads" / "source.txt").write_text(domain)
    before_history = rows(database, "SELECT seq,id,topic,venture_id,actor,payload_json,ts FROM event_log")
    archive = runner.migrate_sqlite(f"sqlite:///{database}")
    assert archive.is_file()
    assert rows(database, "SELECT name,raw_idea FROM ventures") == [(domain, domain)]
    assert rows(database, "SELECT seq,id,topic,venture_id,actor,payload_json,ts FROM event_log") == before_history
    assert rows(database, "SELECT version_num FROM alembic_version") == [(ScriptDirectory.from_config(config).get_current_head(),)]
    assert (root / "uploads" / "source.txt").read_text() == domain
    with tarfile.open(archive) as contents:
        snapshot = root.parent / "before.sqlite3"
        snapshot.write_bytes(contents.extractfile(f"data/{database.name}").read())
    assert rows(snapshot, "SELECT version_num FROM alembic_version") == [(revision,)]
    assert runner.migrate_sqlite(f"sqlite:///{database}") is None


@pytest.mark.parametrize("caller", ["startup", "cli"])
def test_midway_failure_restores_backup_even_after_a_migration_commits(installation, monkeypatch, caller):
    runner, config, root, database, domain = installation
    historical_database(config, database, "bb8ea93e5a0f", domain)
    saved = rows(database, "SELECT * FROM ventures")
    from app.core import local_secrets

    local_secrets.update_settings("smtp", {}, {"password": domain + " saved connection"})

    def fail_midway(config, target):
        archives = list(root.with_name("data-backups").glob("*.tar.gz"))
        assert len(archives) == 1
        connection = config.attributes["connection"]
        connection.exec_driver_sql("UPDATE ventures SET name='partial change'")
        connection.exec_driver_sql("CREATE TABLE partial_upgrade (value TEXT)")
        connection.commit()
        raise RuntimeError("fixture-private-key must not be shown")

    monkeypatch.setattr(command, "upgrade", fail_midway)
    from app.cli_local import LocalMaintenanceError, upgrade

    with pytest.raises(LocalMaintenanceError, match="backup.*restored") as error:
        upgrade() if caller == "cli" else runner.migrate_sqlite(f"sqlite:///{database}")
    assert "fixture-private-key" not in str(error.value)
    assert rows(database, "SELECT * FROM ventures") == saved
    assert rows(database, "SELECT version_num FROM alembic_version") == [("bb8ea93e5a0f",)]
    assert rows(database, "SELECT name FROM sqlite_master WHERE name='partial_upgrade'") == []
    assert local_secrets.get_secret("smtp", "password") == domain + " saved connection"


@pytest.mark.parametrize("future", ["unknown", "known_to_newer_app", "uncheckpointed_wal"])
@pytest.mark.parametrize("nested", [False, True])
def test_newer_database_is_refused_without_any_file_changes(installation, monkeypatch, future, nested):
    runner, config, root, database, domain = installation
    if nested:
        (root / "records").mkdir()
        database = root / "records" / database.name
        monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{database}")
    historical_database(config, database, "head", domain)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    writer = None
    if future == "uncheckpointed_wal":
        writer = sqlite3.connect(database)
        writer.execute("UPDATE alembic_version SET version_num='future_app_revision'")
        writer.commit()
    elif future == "unknown":
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE alembic_version SET version_num='future_app_revision'")
            connection.commit()
    else:
        script = ScriptDirectory.from_config(config)
        old_scripts = root.parent / "older-app"
        (old_scripts / "versions").mkdir(parents=True)
        for revision in script.walk_revisions(base="base", head="s027usererrors"):
            shutil.copyfile(revision.path, old_scripts / "versions" / Path(revision.path).name)
        older_app = ScriptDirectory(str(old_scripts))
        monkeypatch.setattr(ScriptDirectory, "from_config", lambda config: older_app)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    from app.cli_local import LocalMaintenanceError

    try:
        with pytest.raises(LocalMaintenanceError, match="[Uu]pdate your app"):
            runner.migrate_sqlite(f"sqlite:///{database}")
        after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        assert after == before
        assert not root.with_name("data-backups").exists()
    finally:
        if writer is not None:
            writer.close()


def test_sqlite_chain_preserves_append_only_and_custody_triggers(installation):
    runner, config, _root, database, domain = installation
    historical_database(config, database, "s027usererrors", domain)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO credit_reservations (id,account_id,is_pool,remaining_micro,amount_micro,status,purpose,clamped,expires_at,created_at) "
            "VALUES (?,?,1,50,100,'active','device.work_order',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", (domain, domain),
        )
        connection.execute(
            "INSERT INTO credit_ledger (id,seq,account_id,kind,amount_micro,available_after_micro,reason,actor_kind,idempotency_key,meta_json,prev_hash,entry_hash,created_at) "
            "VALUES (?,1,?,'grant',100,100,'saved','owner',?,'{}','',?,CURRENT_TIMESTAMP)", (domain, domain, domain, domain),
        )
        connection.commit()
    columns = [column[1] for column in rows(database, "PRAGMA table_info(credit_reservations)")]
    reservation_query = "SELECT " + ",".join(f'"{column}"' for column in columns) + " FROM credit_reservations"
    reservation = rows(database, reservation_query)
    ledger = rows(database, "SELECT * FROM credit_ledger")
    custody = rows(database, "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='credit_reservations'")
    assert custody
    runner.migrate_sqlite(f"sqlite:///{database}")
    assert rows(database, reservation_query) == reservation
    assert rows(database, "SELECT * FROM credit_ledger") == ledger
    assert rows(database, "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='credit_reservations'") == custody
    from app.db.models import APPEND_ONLY_TABLES

    tables = APPEND_ONLY_TABLES | {"idea_revisions", "build_events", "agent_runtime_steps", "app_usage_records"}
    existing = {name for name, in rows(database, "SELECT name FROM sqlite_master WHERE type='table'")}
    triggers = {name for name, in rows(database, "SELECT name FROM sqlite_master WHERE type='trigger'")}
    for table in tables & existing:
        assert {f"{table}_no_update", f"{table}_no_delete"} <= triggers
    for statement in ("UPDATE event_log SET actor='changed'", "DELETE FROM event_log"):
        with closing(sqlite3.connect(database)) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    assert rows(database, "SELECT actor FROM event_log") == [("owner",)]
    with closing(sqlite3.connect(database)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE credit_reservations SET account_id='changed'")


def test_maintenance_timeout_restores_interrupted_upgrade_and_reports_progress(installation, monkeypatch, capsys):
    runner, config, root, database, domain = installation
    historical_database(config, database, "bb8ea93e5a0f", domain)
    from desktop_runtime import maintenance

    original = maintenance.subprocess.run

    def interrupted_worker(command, **options):
        archive = importlib.import_module("app.cli_local")._backup(root, database)
        from app.core.local_secrets import _atomic_write

        _atomic_write(database.with_name(f".{database.name}-upgrade-backup"), str(archive).encode())
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE ventures SET name='interrupted'")
            connection.commit()
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **options)

    monkeypatch.setattr(maintenance.subprocess, "run", interrupted_worker)
    monkeypatch.setattr(maintenance, "MAINTENANCE_TIMEOUT_SECONDS", 0.1)
    from app.cli_local import LocalMaintenanceError

    started = time.monotonic()
    with pytest.raises(LocalMaintenanceError, match="backup.*restored.*[Rr]eopen your app"):
        maintenance.run()
    assert time.monotonic() - started < 5
    assert "Checking your saved data" in capsys.readouterr().err
    assert rows(database, "SELECT name FROM ventures") == [(domain,)]


def test_second_desktop_instance_refuses_before_touching_data(installation, monkeypatch):
    _runner, _config, root, _database, domain = installation
    from desktop_runtime.maintenance import instance_lock
    from app.cli_local import LocalMaintenanceError

    saved = root / "saved.txt"
    saved.write_text(domain)
    with instance_lock(root):
        result = importlib.import_module("subprocess").run(
            [sys.executable, "-c", "from pathlib import Path; from desktop_runtime.maintenance import instance_lock; "
             "import sys; context=instance_lock(Path(sys.argv[1])); context.__enter__()", str(root)],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode != 0
        assert "Close your other app window" in result.stderr
        with pytest.raises(LocalMaintenanceError, match="Close your other app window"):
            with instance_lock(root):
                pytest.fail("A second instance must not start")
    with instance_lock(root):
        assert saved.read_text() == domain
    from desktop_runtime import __main__ as runtime
    import uvicorn

    monkeypatch.setattr(runtime, "_load_asgi_app", lambda role: object())

    def running_app(*args, **kwargs):
        with pytest.raises(SystemExit, match="Close your other app window"):
            runtime.main(["--role", "core", "--data-dir", str(root)])

    monkeypatch.setattr(uvicorn, "run", running_app)
    runtime.main(["--role", "core", "--data-dir", str(root)])


def test_database_copy_timeout_preserves_both_databases(installation, monkeypatch):
    _runner, _config, root, database, domain = installation
    from app import cli_local

    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(root / "target.sqlite3")) as target:
        for connection in (source, target):
            connection.execute("CREATE TABLE saved (domain TEXT)")
            connection.execute("INSERT INTO saved VALUES (?)", (domain,))
            connection.commit()
        target.execute("BEGIN EXCLUSIVE")
        clock = iter((0, 31))
        monkeypatch.setattr(cli_local, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        with closing(sqlite3.connect(root / "target.sqlite3", timeout=0)) as destination:
            with pytest.raises(cli_local.LocalMaintenanceError, match="Close your other app window"):
                cli_local._copy_database(source, destination)
        assert source.execute("SELECT domain FROM saved").fetchall() == [(domain,)]
        assert target.execute("SELECT domain FROM saved").fetchall() == [(domain,)]


@pytest.mark.parametrize("saved,running", [("1.2.10", "1.2.9"), ("1.2.0", "1.2.0-rc.1"), ("1.2.0-rc.2", "1.2.0-rc.1")])
def test_newer_app_version_is_refused_even_when_the_schema_is_unchanged(installation, monkeypatch, saved, running):
    runner, config, root, database, domain = installation
    historical_database(config, database, "head", domain)
    from app.cli_local import LocalMaintenanceError
    from app.core.local_secrets import _atomic_write

    _atomic_write(database.with_name(f".{database.name}-app-version"), saved.encode())
    monkeypatch.setenv("SUPYMEM_APP_VERSION", running)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(LocalMaintenanceError, match="Update your app"):
        runner.migrate_sqlite(f"sqlite:///{database}")
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not root.with_name("data-backups").exists()


def test_packaged_version_reopens_rc1_data(installation, monkeypatch):
    runner, config, _root, database, domain = installation
    historical_database(config, database, "head", domain)
    from app.core.local_secrets import _atomic_write

    marker = database.with_name(f".{database.name}-app-version")
    _atomic_write(marker, b"1.0.0-rc.1")
    local_config = Path(__file__).resolve().parents[3] / "src-tauri/tauri.local.json"
    version = json.loads(local_config.read_text(encoding="utf-8"))["version"]
    monkeypatch.setenv("SUPYMEM_APP_VERSION", version)

    archive = runner.migrate_sqlite(f"sqlite:///{database}")

    assert archive.is_file()
    assert rows(database, "SELECT name,raw_idea FROM ventures") == [(domain, domain)]
    assert marker.read_text() == version


def test_packaged_maintenance_uses_isolated_bundle_paths(installation, monkeypatch):
    _runner, config, root, database, domain = installation
    historical_database(config, database, "bb8ea93e5a0f", domain)
    from desktop_runtime import maintenance

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(root)
    maintenance.run()
    assert rows(database, "SELECT name FROM ventures") == [(domain,)]
    assert rows(database, "SELECT version_num FROM alembic_version") == [(ScriptDirectory.from_config(config).get_current_head(),)]


def test_api_and_worker_can_share_the_first_upgrade(installation):
    runner, config, root, database, domain = installation
    (root / "saved.txt").write_text(domain)
    with ThreadPoolExecutor(max_workers=2) as roles:
        results = list(roles.map(runner.migrate_sqlite, [f"sqlite:///{database}"] * 2))
    assert sum(result is not None for result in results) == 1
    assert len(list(root.with_name("data-backups").glob("*.tar.gz"))) == 1
    assert rows(database, "SELECT version_num FROM alembic_version") == [(ScriptDirectory.from_config(config).get_current_head(),)]
    assert (root / "saved.txt").read_text() == domain


def test_maintenance_worker_refuses_an_already_open_app(installation):
    _runner, config, _root, database, domain = installation
    historical_database(config, database, "bb8ea93e5a0f", domain)
    from app.core.local_process import process_lock
    from app.cli_local import LocalMaintenanceError
    from desktop_runtime.maintenance import _worker, run

    with process_lock(), pytest.raises(SystemExit, match="Close your other app window"):
        _worker()
    with process_lock(), pytest.raises(LocalMaintenanceError, match="Close your other app window"):
        run()
    assert rows(database, "SELECT name FROM ventures") == [(domain,)]
    assert rows(database, "SELECT version_num FROM alembic_version") == [("bb8ea93e5a0f",)]


def test_backup_keeps_competing_writes_out_until_the_upgrade_finishes(installation, monkeypatch):
    runner, config, _root, database, domain = installation
    historical_database(config, database, "bb8ea93e5a0f", domain)
    from app import cli_local

    original = cli_local._backup

    def checked_backup(*args, **kwargs):
        with closing(sqlite3.connect(database, timeout=0)) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("UPDATE ventures SET name='competing write'")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_local, "_backup", checked_backup)
    assert runner.migrate_sqlite(f"sqlite:///{database}").is_file()
    assert rows(database, "SELECT name FROM ventures") == [(domain,)]


def test_moved_data_paths_are_backed_up_before_rebasing_at_head(installation):
    runner, config, root, database, domain = installation
    historical_database(config, database, "head", domain)
    from app import cli_local

    origin = root.parent / "previous-data"
    previous = str(origin / "uploads" / "source.txt")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE saved_files (artifact_path TEXT)")
        connection.execute("INSERT INTO saved_files VALUES (?)", (previous,))
        connection.commit()
    cli_local._record_roots(root, [str(origin)])
    history = rows(database, "SELECT * FROM event_log")

    archive = runner.migrate_sqlite(f"sqlite:///{database}")

    assert rows(database, "SELECT artifact_path FROM saved_files") == [(str(root / "uploads" / "source.txt"),)]
    assert rows(database, "SELECT * FROM event_log") == history
    assert rows(database, "SELECT name FROM ventures") == [(domain,)]
    with tarfile.open(archive) as contents:
        snapshot = root.parent / "before.sqlite3"
        snapshot.write_bytes(contents.extractfile(f"data/{database.name}").read())
    assert rows(snapshot, "SELECT artifact_path FROM saved_files") == [(previous,)]
    assert runner.migrate_sqlite(f"sqlite:///{database}") is None


@pytest.mark.parametrize("corrupt", [False, True])
def test_checksum_backup_restores_only_verified_data_and_keeps_keys(installation, corrupt):
    runner, config, root, database, domain = installation
    historical_database(config, database, "bb8ea93e5a0f", domain)
    from app import cli_local
    from app.core import local_secrets

    local_secrets.update_settings("smtp", {}, {"password": domain + " saved connection"})
    with tarfile.open(cli_local._backup(root, database)) as original:
        snapshot = original.extractfile(f"data/{database.name}").read()
    archive = root.parent / "checksum-backup.tar.gz"
    manifest = {"version": 2, "database": database.name,
                "files": {database.name: "0" * 64 if corrupt else hashlib.sha256(snapshot).hexdigest()}}
    with tarfile.open(archive, "w:gz") as contents:
        for name, payload in [("manifest.json", json.dumps(manifest).encode()), (f"data/{database.name}", snapshot)]:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            contents.addfile(member, io.BytesIO(payload))
    pending = database.with_name(f".{database.name}-upgrade-backup")
    local_secrets._atomic_write(pending, str(archive).encode())
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE ventures SET name=?", (domain + " interrupted",))
        connection.commit()

    if corrupt:
        with pytest.raises(cli_local.LocalMaintenanceError, match="restor"):
            runner.recover_sqlite(f"sqlite:///{database}")
        assert pending.exists()
        assert rows(database, "SELECT name FROM ventures") == [(domain + " interrupted",)]
    else:
        assert runner.recover_sqlite(f"sqlite:///{database}") == archive
        assert not pending.exists()
        assert rows(database, "SELECT name FROM ventures") == [(domain,)]
    assert local_secrets.get_secret("smtp", "password") == domain + " saved connection"
