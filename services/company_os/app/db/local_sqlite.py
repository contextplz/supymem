"""File-backed database startup and history guards for local mode."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, Connection, make_url

if TYPE_CHECKING:
    from alembic.config import Config
    from alembic.script import ScriptDirectory


def default_database_url(env: Mapping[str, str]) -> str:
    from app.config import data_root

    directory = data_root({**env, "COMPANY_OS_DATA_DIR": (env.get("COMPANY_OS_DATA_DIR") or "").strip()
                          or (env.get("SUPYMEM_DATA_DIR") or "").strip()})
    path = directory / "database.sqlite3"
    return URL.create("sqlite", database=str(path)).render_as_string(hide_password=False)


def ensure_sqlite_parent(url: str | URL) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(mode=0o700, parents=True, exist_ok=True)


@contextmanager
def _schema_reader(database: Path, root: Path):
    """Read committed WAL without creating or updating live SQLite sidecars."""
    from app.cli_local import _connection

    wal = database.with_name(f"{database.name}-wal")
    if not wal.exists():
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True, timeout=5)) as connection:
            yield connection
        return
    with tempfile.TemporaryDirectory(prefix=".upgrade-check-", dir=root.parent) as temporary:
        snapshot = Path(temporary) / database.name
        shutil.copyfile(database, snapshot)
        try:
            shutil.copyfile(wal, snapshot.with_name(f"{snapshot.name}-wal"))
        except FileNotFoundError:
            pass
        with closing(_connection(snapshot)) as connection:
            yield connection


def _check_revision(connection: sqlite3.Connection, script: ScriptDirectory) -> tuple[str, ...]:
    from app.cli_local import LocalMaintenanceError

    current = tuple(row[0] for row in connection.execute("SELECT version_num FROM alembic_version")) if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone() else ()
    if not set(current) <= set(script.get_heads()) and not set(current) <= {revision.revision for revision in script.walk_revisions()}:
        raise LocalMaintenanceError("Update your app before opening this data folder. Your saved work has not been changed.")
    return current


def _version_key(version: str) -> tuple | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", version)
    if not match:
        return None
    prerelease = match[4]
    return tuple(int(part) for part in match.groups()[:3]) + (
        (1,) if prerelease is None else (0, tuple((0, int(part)) if part.isdecimal() else (1, part) for part in prerelease.split("."))),
    )


def _app_version() -> str | None:
    version = os.environ.get("SUPYMEM_APP_VERSION", "")
    if _version_key(version) is not None:
        return version
    try:
        version = json.loads((Path(__file__).resolve().parents[4] / "src-tauri/tauri.local.json").read_text())["version"]
        return version if isinstance(version, str) and _version_key(version) is not None else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _version_marker(database: Path) -> Path:
    return database.with_name(f".{database.name}-app-version")


def _check_app_version(database: Path, *, source_version: str | None = None) -> str | None:
    from app.cli_local import LocalMaintenanceError
    from app.core.local_secrets import _read_private

    try:
        marker = _read_private(_version_marker(database))
        previous = marker.decode() if marker is not None else source_version
        if previous is None:
            return None
        saved = _version_key(previous)
        if saved is None or (source_version is not None and source_version != previous):
            raise ValueError
    except (OSError, ValueError):
        raise LocalMaintenanceError(
            "Your data's app version could not be verified. Keep your files and choose an intact copy in "
            "Keys and connections, under Your data and recovery."
        ) from None
    version = _app_version()
    if version is None:
        raise LocalMaintenanceError(
            "Your recovery app's version could not be verified. Use the recovery command from the same "
            "release that saved this data, or open that release and use Keys and connections, under Your data and recovery."
        )
    if saved > _version_key(version):
        raise LocalMaintenanceError("Update your app before opening this data folder. Your saved work has not been changed.")
    return previous


def recover_sqlite(url: str) -> Path | None:
    from app.cli_local import LocalMaintenanceError, _check_database_files, _database_path, _restore_database
    from app.core.local_secrets import _read_private

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        return None
    database = _database_path(parsed.database)
    _check_database_files(database)
    _check_app_version(database)
    pending = database.with_name(f".{database.name}-upgrade-backup")
    payload = _read_private(pending)
    if payload is None:
        return None
    try:
        archive = Path(payload.decode())
        _restore_database(archive, database)
        pending.unlink()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, tarfile.TarError):
        raise LocalMaintenanceError(
            "Your interrupted upgrade could not be restored. Keep your backup folder, close your app, "
            "and run the restore command with your latest automatic backup before reopening your app."
        ) from None
    return archive


@contextmanager
def sqlite_schema_lock(url: str | URL):
    """Serialize schema changes across processes using the same SQLite file."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        yield None
        return
    from app.cli_local import _database_path
    from app.config import data_root
    from app.core.local_process import check_private, file_lock, open_private

    database = _database_path(parsed.database)
    root = data_root().resolve()
    if not database.is_relative_to(root):
        root = database.parent
    lock_path = root.with_name(f".{root.name}-{database.name}-upgrade.lock")
    descriptor = open_private(lock_path, os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "rb") as lock:
        check_private(lock)
        with file_lock(lock):
            yield root


def migrate_sqlite(url: str, *, backup: Callable[[], Path] | None = None,
                   progress: Callable[[str], None] | None = None) -> Path | None:
    """Back up before writing, and recover even a committed partial migration."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        return
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.cli_local import _database_path, _recover_restore

    database = _database_path(parsed.database)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations").replace("%", "%%"))
    script = ScriptDirectory.from_config(config)
    ensure_sqlite_parent(parsed)
    with sqlite_schema_lock(parsed) as root:
        if root is not None:
            _recover_restore(root)
            return _migrate_locked(root, database, parsed, config, script, backup, progress)


def _migrate_locked(root: Path, database: Path, parsed: URL, config: Config, script: ScriptDirectory,
                    backup: Callable[[], Path] | None, progress: Callable[[str], None] | None) -> Path | None:
    from app.cli_local import BackupFolderError, LocalMaintenanceError, _backup, _check_database, _check_database_files, _directory_roots, _path_changes
    from app.core.local_process import open_private
    from app.core.local_secrets import _atomic_write, _read_private

    _check_database_files(database)
    _check_app_version(database)
    version = _app_version()
    marker = _version_marker(database)
    pending = database.with_name(f".{database.name}-upgrade-backup")
    heads = set(script.get_heads())
    current: tuple[str, ...] = ()
    if database.exists():
        with _schema_reader(database, root) as connection:
            current = _check_revision(connection, script)
            if (backup is None and set(current) == heads and _read_private(pending) is None
                    and (not version or _read_private(marker) == version.encode())):
                if not _path_changes(connection, _directory_roots(root, create=False), root):
                    _directory_roots(root)
                    return None
    recovered = recover_sqlite(str(parsed))
    if recovered:
        if progress:
            progress("Your automatic backup was restored after an interrupted update.")
        with _schema_reader(database, root) as connection:
            current = _check_revision(connection, script)
        _check_app_version(database)
    if not database.exists():
        os.close(open_private(database, os.O_CREAT | os.O_EXCL | os.O_RDWR))
    from app.db.session import _register_sqlite_pragmas, _sqlite_connect_kwargs

    engine = create_engine(parsed.set(drivername="sqlite"), **_sqlite_connect_kwargs(str(parsed)))
    _register_sqlite_pragmas(engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            roots = _directory_roots(root, create=False)
            raw = connection.connection.driver_connection
            changes = _path_changes(raw, roots, root)
            if (backup is None and not changes and set(current) == heads
                    and (not version or _read_private(marker) == version.encode())):
                _directory_roots(root)
                return None
            if progress:
                progress("Saving your automatic backup before updating your data.")
            try:
                archive = backup() if backup else _backup(root, database)
            except BackupFolderError as error:
                default = shlex.quote(str(root.with_name(f"{root.name}-backups")))
                raise BackupFolderError(
                    f"{error} If your app cannot open, stop your app and worker, then use the recovery command "
                    f"`python -m app.cli_local upgrade --output {default}` to back up in the default location and update."
                ) from None
            _atomic_write(pending, str(archive).encode())
            for statement, parameters in changes:
                raw.execute(statement, parameters)
            if progress:
                progress("Updating your saved data. Your automatic backup is ready.")
            from alembic import command
            from alembic.migration import MigrationContext

            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            if set(MigrationContext.configure(connection).get_current_heads()) != heads:
                raise LocalMaintenanceError("Your data update did not finish.")
        _check_database(database)
        if version:
            _atomic_write(marker, version.encode())
        _directory_roots(root)
        pending.unlink()
    except Exception:
        engine.dispose()
        if not pending.exists():
            raise
        recover_sqlite(str(parsed))
        raise LocalMaintenanceError(
            "Your data update could not finish. Your automatic backup was restored. "
            "Check free disk space and your data-folder permissions, then reopen your app to retry."
        ) from None
    finally:
        engine.dispose()
    if progress:
        progress("Your saved data is ready.")
    return archive


def install_append_only_guards(connection: Connection) -> None:
    from app.db.append_only import _install_sqlite
    from app.db.models import APPEND_ONLY_TABLES

    tables = APPEND_ONLY_TABLES | {
        "idea_revisions", "build_events", "agent_runtime_steps", "app_usage_records",
    }
    for table in sorted(tables.intersection(inspect(connection).get_table_names())):
        _install_sqlite(connection, table)
