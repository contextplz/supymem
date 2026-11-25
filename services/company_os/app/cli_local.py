"""Run local maintenance with ``python -m app.cli_local``.

Backups include your encrypted saved settings, vault and uploads, not plaintext
settings files or embedded database credentials. Keep archives private. Stop your
app and worker before restoring or upgrading. Save your keys in Keys and connections
before backing up. Process-only credentials are not exported.
Doctor checks your local setup without model calls or sending mail.
Seed-demo saves a fictional example without contacting providers or starting work.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from cryptography.exceptions import InvalidTag

from app.core.local_mode import is_local


class LocalMaintenanceError(ValueError):
    """A safe, actionable maintenance error that contains no credentials."""


class BackupFolderError(LocalMaintenanceError):
    pass


_BACKUP_FIELD = "Full backup folder path in Keys and connections, under Your data folder and backup"
_LEGACY_PROVIDER_KEY = Path("vault/providers/machine.secret")
_RESTORE_RECEIPT = ".restore-published"


def local_data_directory(application_name: str, env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    source = os.environ if env is None else env
    override = source.get("COMPANY_OS_DATA_DIR", "").strip() or source.get("SUPYMEM_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    platform = sys.platform if platform is None else platform
    home = Path(source.get("USERPROFILE" if platform == "win32" else "HOME") or Path.home())
    if platform == "darwin":
        directory = home / "Library/Application Support"
    elif platform == "win32":
        directory = Path(source.get("LOCALAPPDATA") or home / "AppData/Local")
    else:
        directory = Path(source.get("XDG_DATA_HOME") or home / ".local/share")
        if not directory.is_absolute():
            directory = home / ".local/share"
    return (directory / application_name).resolve()


def _private_directory(path: Path) -> None:
    if os.name == "nt":
        from app.core.local_windows import private_directory

        private_directory(path)
        return
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise LocalMaintenanceError("Your backup folder belongs to another account. Choose a folder you own in Keys and connections, under Your data and recovery, then retry.")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def _plaintext_settings(path: PurePosixPath, root: Path) -> bool:
    from app.secrets.dotenv_file import secrets_env_path

    return ((root / path).resolve() == secrets_env_path().resolve()
            or any(part.startswith(".env") or part in {"secrets.json", ".aws", ".ssh", ".codex"} for part in path.parts))


def _remove_database_keys(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA secure_delete=ON")
        for table, column in (("venture_resources", "secrets_json"), ("admin_users", "totp_secret_enc")):
            if column in {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}:
                connection.execute(f'UPDATE "{table}" SET "{column}"=NULL')
        connection.commit()
        connection.execute("VACUUM")


DIRECTORY_RECORD = ".data-directory.json"


def _portable_name(name: str, components: dict[str, str]) -> None:
    from core.artifact_policy import _record_components, validate_windows_name

    try:
        if (any(part in {"", ".", ".."} for part in name.split("/")) or "\\" in name
                or any(ord(char) < 32 for char in name)):
            raise ValueError
        validate_windows_name(name)
        _record_components(name, components)
    except ValueError:
        raise LocalMaintenanceError(
            f"This file name cannot be restored on every computer: {ascii(name.removeprefix('data/'))}. "
            "Rename it in your data folder, then retry in Keys and connections, under Your data and recovery."
        ) from None


def _validate_roots(roots: list[str]) -> None:
    if (not isinstance(roots, list) or not roots or any(not isinstance(value, str) for value in roots)
            or len(set(roots)) != len(roots)):
        raise ValueError
    for value in roots:
        path = PurePosixPath(value) if value.startswith("/") else PureWindowsPath(value)
        if not path.is_absolute() or path.parent == path or ".." in path.parts:
            raise ValueError


def _stored_roots(root: Path) -> list[str]:
    from app.core.providers_local import _read_private

    record = json.loads(_read_private(root / DIRECTORY_RECORD))
    if not isinstance(record, dict) or set(record) != {"source_roots"}:
        raise ValueError
    _validate_roots(record["source_roots"])
    return record["source_roots"]


def _record_roots(root: Path, roots: list[str]) -> None:
    from app.core.local_secrets import _atomic_write

    _validate_roots(roots)
    _atomic_write(root / DIRECTORY_RECORD, json.dumps({"source_roots": roots}).encode())


def _directory_roots(root: Path, *, create: bool = True) -> list[str]:
    try:
        stored = _stored_roots(root) if (root / DIRECTORY_RECORD).exists() else []
        current = str(root.resolve())
        roots = [current, *[value for value in stored if value != current]]
        if create and stored != roots:
            _record_roots(root, roots)
        return roots
    except (OSError, ValueError, TypeError):
        raise LocalMaintenanceError("Your data-folder record could not be verified. Stop your app, then restore an intact full backup with python -m app.cli_local restore <backup-file>.") from None


def _path_changes(connection: sqlite3.Connection, roots: list[str], root: Path) -> list[tuple[str, tuple[str, str]]]:
    from app.db.models import APPEND_ONLY_TABLES

    _validate_roots(roots)
    current = str(root.resolve())
    origins = sorted(set([current, *roots]), key=len, reverse=True)
    immutable = APPEND_ONLY_TABLES | {"idea_revisions", "build_events", "agent_runtime_steps", "app_usage_records"}
    changes = []
    for table, in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        if table in immutable:
            continue
        quoted_table = '"' + table.replace('"', '""') + '"'
        for column in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall():
            name = column[1]
            if name != "path" and not name.endswith("_path"):
                continue
            quoted_column = '"' + name.replace('"', '""') + '"'
            values = connection.execute(f"SELECT DISTINCT {quoted_column} FROM {quoted_table} WHERE typeof({quoted_column})='text'").fetchall()
            for value, in values:
                for origin in origins:
                    path_type = PurePosixPath if origin.startswith("/") else PureWindowsPath
                    try:
                        relative = path_type(value).relative_to(path_type(origin))
                    except ValueError:
                        continue
                    if ".." in relative.parts:
                        raise LocalMaintenanceError("Your saved file location leaves your data folder. Stop your app and worker, then restore an intact full backup with python -m app.cli_local restore <backup-file>.")
                    replacement = str(root.resolve().joinpath(*relative.parts))
                    if replacement != value:
                        changes.append((f"UPDATE {quoted_table} SET {quoted_column}=? WHERE {quoted_column}=?", (replacement, value)))
                    break
    return changes


def _relocate_database(database: Path, roots: list[str], root: Path) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        for statement, parameters in _path_changes(connection, roots, root):
            connection.execute(statement, parameters)
    _check_database(database)


def _archive_manifest(root: Path, database: Path, stores: list[str] | None = None) -> dict:
    files = {}
    detected = {database.relative_to(root).as_posix()}
    components: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError
        relative = path.relative_to(root).as_posix()
        _portable_name(f"data/{relative}", components)
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    detected.add(path.relative_to(root).as_posix())
                stream.seek(0)
                files[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
    if stores is not None and set(stores) != detected:
        raise ValueError
    return {"version": 2, "database": database.relative_to(root).as_posix(), "databases": sorted(detected),
            "source_roots": _stored_roots(root), "files": files}


def _verify_archive(staging: Path, database: Path, *, require_app_version: bool = False) -> dict:
    from app.db.local_sqlite import _check_app_version

    root = staging / "data"
    manifest = json.loads((staging / "manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError
    if manifest.get("version") != 2:
        raise LocalMaintenanceError("Your archive uses a different backup version. Open it with the app version that created it, update that app, then export again in Keys and connections, under Your data and recovery.")
    skipped = manifest.pop("skipped", [])
    app_version = manifest.pop("app_version", None)
    if app_version is not None and not isinstance(app_version, str):
        raise ValueError
    if not isinstance(skipped, list) or any(not isinstance(path, str) for path in skipped):
        raise ValueError
    stores = manifest.get("databases")
    if (not isinstance(stores, list) or any(not isinstance(store, str) for store in stores)
            or len(stores) != len(set(stores)) or database.relative_to(root).as_posix() not in stores):
        raise ValueError
    if not root.is_dir() or manifest != _archive_manifest(root, database, stores):
        raise ValueError
    version = _check_app_version(database, source_version=app_version)
    if require_app_version and version is None:
        raise LocalMaintenanceError(
            "Your export does not identify the app version that saved it. Update the app with your original data, "
            "then export again in Keys and connections, under Your data and recovery."
        )
    for store in stores:
        path = PurePosixPath(store)
        if path.is_absolute() or ".." in path.parts or "\\" in store or store not in manifest["files"]:
            raise ValueError
        _check_database(root / path)
        with closing(_connection(root / path)) as connection:
            _path_changes(connection, manifest["source_roots"], root)
        if any(Path(f"{root / path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError
    return {**manifest, "app_version": version} if version is not None else manifest


def _check_revision(database: Path, *, imported: bool = False) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError

    with closing(_connection(database)) as connection:
        if not connection.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchone():
            return
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "db/migrations").replace("%", "%%"))
        chain = ScriptDirectory.from_config(config)
        revisions = {revision for revision, in connection.execute("SELECT version_num FROM alembic_version")}
        for revision in revisions:
            try:
                chain.get_revision(revision)
            except CommandError:
                raise LocalMaintenanceError("Your archive needs a newer app. Update your app, then retry in Keys and connections, under Your data and recovery.") from None
        if imported and revisions != set(chain.get_heads()):
            raise LocalMaintenanceError("Your export uses an older data version. Open your original data, update that app, then export again in Keys and connections, under Your data and recovery.")


def _paths() -> tuple[Path, Path]:
    if not is_local():
        raise LocalMaintenanceError("Use these commands only in your local installation with SUPYMEM_MODE=local.")
    from sqlalchemy.engine import make_url

    from app.config import data_root, settings
    from app.db.session import resolve_database_url
    from app.secrets.dotenv_file import secrets_env_path

    root = data_root().resolve()
    try:
        url = make_url(resolve_database_url() or settings.db_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:" or url.query:
            raise ValueError
        database = _database_path(url.database)
        database.relative_to(root)
        secrets_env_path().resolve().relative_to(root)
        if database == root or root == root.parent or Path(__file__).resolve().is_relative_to(root):
            raise ValueError
    except (ValueError, TypeError):
        raise LocalMaintenanceError(
            "Your database and saved settings must stay inside your data folder. Set COMPANY_OS_DB_URL and COMPANY_OS_SECRETS_ENV to files inside COMPANY_OS_DATA_DIR, then reopen your app."
        ) from None
    return root, database


@contextmanager
def _offline():
    from app.core.local_process import LocalProcessBusy, process_lock
    from app.worker.health import healthy

    if healthy():
        raise LocalMaintenanceError("Stop your app and worker, then retry your restore or upgrade.")
    try:
        with process_lock(exclusive=True):
            yield
    except LocalProcessBusy as exc:
        raise LocalMaintenanceError(str(exc)) from None


def _connection(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)


_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _database_path(path: str) -> Path:
    database = Path(path).expanduser()
    return database.parent.resolve() / database.name


def _link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _check_database_files(database: Path) -> None:
    for path in (database, *(Path(f"{database}{suffix}") for suffix in _SQLITE_SIDECARS)):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if _link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise LocalMaintenanceError(
                "Your database or one of its companion files is a link or special file. Close your app, "
                "replace it with the original regular file, then reopen your app."
            )


def _check_database(database: Path) -> None:
    _check_database_files(database)
    try:
        with closing(_connection(database)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError
    except sqlite3.Error:
        raise LocalMaintenanceError("Your database could not be verified. Stop your app and worker, then restore an intact full backup with python -m app.cli_local restore <backup-file>.") from None


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _copy_database(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
            raise LocalMaintenanceError("Copying your saved data took too long. Close your other app window, check free disk space, then retry.")

    source.backup(target, pages=256, progress=progress)


def backup(output: Path | str | None = None, *, skipped: set[str] | None = None) -> Path:
    root, database = _paths()
    return _backup(root, database, output, skipped=skipped)


def _backup_directory(root: Path, output: Path | str | None = None) -> Path:
    if output is None and is_local():
        from app.core.local_secrets import read_settings

        try:
            output = read_settings("data")["values"].get("backup_directory")
            if output is not None and not isinstance(output, str):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, InvalidTag):
            raise BackupFolderError(f"Your backup settings could not be opened. Restore your saved settings or choose your backup folder again using {_BACKUP_FIELD}, then retry.") from None
    default = root.with_name(f"{root.name}-backups")
    try:
        destination = Path(output).expanduser() if output else default
    except (TypeError, ValueError, RuntimeError):
        raise BackupFolderError(f"Enter the full folder path in {_BACKUP_FIELD}, then retry.") from None
    return _validate_backup_directory(root, destination)


def _validate_backup_directory(root: Path, destination: Path) -> Path:
    if not destination.is_absolute():
        raise BackupFolderError(f"Enter the absolute folder path in {_BACKUP_FIELD}, then retry.")
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise BackupFolderError(f"Choose a folder you can open using {_BACKUP_FIELD}, then retry.") from None
    else:
        if _link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise BackupFolderError(f"Choose a regular folder, not a link or special file, using {_BACKUP_FIELD}, then retry.")
        if os.name != "nt" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
            raise BackupFolderError(f"Choose a folder you own that only you can write to using {_BACKUP_FIELD}, then retry.")
    try:
        destination = destination.resolve()
    except (OSError, RuntimeError):
        raise BackupFolderError(f"Choose a folder you can open using {_BACKUP_FIELD}, then retry.") from None
    if destination.is_relative_to(root):
        raise BackupFolderError(f"Choose a backup folder outside your data folder using {_BACKUP_FIELD}, then retry.")
    parent = next((part for part in (destination, *destination.parents) if part.exists()), destination)
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise BackupFolderError(f"Choose a folder you can write to using {_BACKUP_FIELD}, then retry.")
    return destination


def _backup_paths(root: Path, skipped: set[str]):
    directories = [root]
    while directories:
        for path in directories.pop().iterdir():
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            regular = stat.S_ISREG(metadata.st_mode)
            directory = stat.S_ISDIR(metadata.st_mode)
            if _link(metadata) or not (regular or directory):
                skipped.add(path.relative_to(root).as_posix())
                continue
            if directory:
                directories.append(path)
            yield path, regular


@contextmanager
def _backup_file(path: Path):
    from core.artifact_policy import ArtifactRejected, _open_at, directory_fd

    try:
        with directory_fd(path.parent) as directory:
            if os.name == "nt":
                descriptor = os.open(path, os.O_RDONLY | os.O_BINARY)
            else:
                descriptor = _open_at(directory, path.name, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ArtifactRejected("backup_source_not_regular")
                if os.name == "nt":
                    named = path.lstat()
                    if _link(named) or not os.path.samestat(metadata, named):
                        raise ArtifactRejected("backup_source_changed")
                yield stream
    except (OSError, ArtifactRejected):
        raise LocalMaintenanceError("One of your files could not be copied. Close other tools using your data folder and check free disk space, then retry.") from None


def _backup(root: Path, database: Path, output: Path | str | None = None, *, skipped: set[str] | None = None) -> Path:
    from app.core.local_process import open_private
    from app.core.local_secrets import _atomic_write, _locked
    from app.db.local_sqlite import _app_version, _check_app_version

    destination = _backup_directory(root, output)
    _check_database(database)
    try:
        if os.name == "nt":
            _private_directory(destination)
        else:
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            _validate_backup_directory(root, destination)
    except (OSError, ValueError) as error:
        if isinstance(error, BackupFolderError):
            raise
        raise BackupFolderError(f"Choose a folder you can write to using {_BACKUP_FIELD}, then retry.") from None
    _directory_roots(root)
    archive = destination / f"backup-{_stamp()}.tar.gz"
    relative_database = database.relative_to(root)
    skipped = set() if skipped is None else skipped
    databases = {database}
    for source_path, regular in _backup_paths(root, skipped):
        if regular and source_path.relative_to(root) != _LEGACY_PROVIDER_KEY:
            with _backup_file(source_path) as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    databases.add(source_path)
    sidecars = {Path(f"{store}{suffix}") for store in databases for suffix in _SQLITE_SIDECARS}
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=destination) as temporary:
        staging = Path(temporary)
        _private_directory(staging)
        for store in sorted(databases):
            _check_database_files(store)
            snapshot = staging / "snapshots" / store.relative_to(root)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(snapshot, b"")
            with closing(_connection(store)) as source, closing(sqlite3.connect(snapshot)) as target:
                _copy_database(source, target)
                target.execute("PRAGMA journal_mode=DELETE")
            _remove_database_keys(snapshot)
            _check_database(snapshot)
        with _locked():
            for source_path, regular in _backup_paths(root, skipped):
                relative = source_path.relative_to(root)
                if (source_path in sidecars or _plaintext_settings(PurePosixPath(relative.as_posix()), root)
                        or source_path.name in {"local-key.bin", _RESTORE_RECEIPT} or relative == _LEGACY_PROVIDER_KEY
                        or source_path.resolve() == Path(os.environ.get("SUPYMEM_LOCAL_PROXY_SECRET_FILE") or root / "owner-session.secret").resolve()):
                    continue
                snapshot = staging / "snapshots" / relative
                if not regular:
                    snapshot.mkdir(parents=True, exist_ok=True, mode=0o700)
                elif source_path not in databases:
                    snapshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with _backup_file(source_path) as source, os.fdopen(open_private(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as target:
                        if source.read(16) == b"SQLite format 3\x00":
                            raise LocalMaintenanceError("Your data changed while preparing your backup. Retry your backup in Keys and connections, under Your data and recovery.")
                        source.seek(0)
                        shutil.copyfileobj(source, target)
                        snapshot.chmod((os.fstat(source.fileno()).st_mode & 0o700) | 0o600)
        pending = staging / "archive.tar.gz"
        with os.fdopen(open_private(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as contents:
                manifest = staging / "manifest.json"
                snapshot_root = staging / "snapshots"
                payload = _archive_manifest(snapshot_root, snapshot_root / relative_database,
                                          [store.relative_to(root).as_posix() for store in databases])
                version = _check_app_version(snapshot_root / relative_database) or _app_version()
                if version is not None:
                    payload["app_version"] = version
                if skipped:
                    payload["skipped"] = sorted(skipped)
                _atomic_write(manifest, json.dumps(payload).encode())

                def private_member(member):
                    member.mode = 0o700 if member.isdir() else (member.mode & 0o700) | 0o600
                    return member

                contents.add(manifest, arcname="manifest.json", filter=private_member)
                contents.add(snapshot_root, arcname="data", recursive=False, filter=private_member)
                for path in sorted(snapshot_root.rglob("*")):
                    contents.add(path, arcname=f"data/{path.relative_to(snapshot_root).as_posix()}",
                                 recursive=False, filter=private_member)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, archive)
    return archive


def restore(archive: Path | str) -> Path | None:
    from app.db.local_sqlite import _check_app_version

    root, database = _paths()
    with _offline():
        _recover_restore(root)
        _check_app_version(database)
        return _restore(archive, root, database)


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _restore_record(root: Path) -> Path:
    return root.with_name(f".{root.name}-restore-in-progress")


def _recover_restore(root: Path) -> bool:
    from app.core.local_secrets import _read_private

    record = _restore_record(root)
    recovery = None
    try:
        encoded = _read_private(record)
        if encoded is None:
            return False
        request = json.loads(encoded)
        stamp = request["stamp"]
        if (datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").strftime("%Y%m%dT%H%M%S%fZ") != stamp
                or not isinstance(request["pending"], bool)):
            raise ValueError
        recovery = root.with_name(f"{root.name}-before-restore-{stamp}")
        receipt = root / _RESTORE_RECEIPT
        committed = _read_private(receipt) == stamp.encode()
        if committed:
            if request["pending"]:
                pending = root.with_name(f".{root.name}-pending-restore")
                retired = pending.with_name(f"{pending.name}-finished-{stamp}")
                if pending.exists():
                    os.replace(pending, retired)
                    _sync_directory(root.parent)
                with suppress(OSError):
                    shutil.rmtree(retired, ignore_errors=True)
        elif recovery.exists():
            if root.exists():
                if not any(root.iterdir()):
                    root.rmdir()
                else:
                    os.replace(root, root.with_name(f"{root.name}-before-recovery-{_stamp()}"))
            os.replace(recovery, root)
            _sync_directory(root.parent)
        record.unlink()
        _sync_directory(root.parent)
        if committed:
            receipt.unlink(missing_ok=True)
        return committed
    except (OSError, ValueError, KeyError, TypeError):
        if recovery is not None and recovery.exists() and not root.exists():
            raise LocalMaintenanceError(
                f"Your previous data is saved at {recovery}. Move that folder back to {root} with your app "
                "and worker stopped, then reopen your app."
            ) from None
        raise LocalMaintenanceError(
            "Your restore was interrupted. Keep your data and backups. Check permissions on your data folder "
            "and its parent, then reopen your app to finish recovery."
        ) from None


def _publish_restore(restored: Path, root: Path, *, pending: bool) -> Path | None:
    from app.core.local_secrets import _atomic_write

    stamp = _stamp()
    recovery = root.with_name(f"{root.name}-before-restore-{stamp}") if root.exists() else None
    try:
        if os.name != "nt":
            for directory, _, files in os.walk(restored, topdown=False):
                for filename in files:
                    descriptor = os.open(Path(directory) / filename, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                _sync_directory(Path(directory))
        _atomic_write(restored / _RESTORE_RECEIPT, stamp.encode())
        _atomic_write(_restore_record(root), json.dumps({"stamp": stamp, "pending": pending}).encode())
        if recovery is not None:
            os.replace(root, recovery)
            _sync_directory(root.parent)
        os.replace(restored, root)
        _sync_directory(root.parent)
        _sync_directory(restored.parent)
    except OSError:
        if _recover_restore(root):
            return recovery
        raise LocalMaintenanceError(
            "Your restore could not finish. Keep your backup and check your data-folder permissions, "
            "then retry in Keys and connections, under Your data and recovery."
        ) from None
    _recover_restore(root)
    return recovery


def _carry_suppressions(root: Path, database: Path, restored: Path) -> None:
    from app.db.local_sqlite import _version_marker
    from app.gtm.relationships.suppression import _mailbox_email

    try:
        if not database.exists() and not database.is_symlink():
            if os.path.lexists(root / DIRECTORY_RECORD) or os.path.lexists(_version_marker(database)):
                raise sqlite3.DatabaseError
            return
        _check_database_files(database)
        with closing(_connection(database)) as current, closing(sqlite3.connect(restored)) as target, target:
            tables = {name for name, in current.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            stops = current.execute("SELECT workspace_id,email,linkedin_url,reason,source,created_at FROM gtm_suppressions").fetchall() if "gtm_suppressions" in tables else []
            if "gtm_contacts" in tables:
                columns = {row[1] for row in current.execute("PRAGMA table_info(gtm_contacts)")}
                flags = [column for column in ("do_not_contact", "unsubscribed_at", "bounced_at") if column in columns]
                if flags:
                    unsubscribed = "unsubscribed_at IS NOT NULL" if "unsubscribed_at" in flags else "0"
                    where = " OR ".join("do_not_contact != 0" if flag == "do_not_contact" else f"{flag} IS NOT NULL" for flag in flags)
                    stops.extend(current.execute(
                        f"SELECT workspace_id,email,NULL,CASE WHEN {unsubscribed} THEN 'opted_out' ELSE 'do_not_contact' END,"
                        f"CASE WHEN {unsubscribed} THEN 'opt_out' ELSE 'manual' END,created_at FROM gtm_contacts WHERE {where}"
                    ))
            if stops:
                workspaces = {name for name, in target.execute("SELECT id FROM workspaces")}
                existing = {(workspace, _mailbox_email(email), linkedin, reason, source)
                            for workspace, email, linkedin, reason, source in target.execute(
                                "SELECT workspace_id,email,linkedin_url,reason,source FROM gtm_suppressions")}
                for workspace, email, linkedin, reason, source, created in stops:
                    mailbox = _mailbox_email(email)
                    for scope in workspaces | {workspace}:
                        key = (scope, mailbox, linkedin, reason, source)
                        if key not in existing and (mailbox or linkedin):
                            target.execute("INSERT INTO gtm_suppressions (id,workspace_id,email,linkedin_url,reason,source,created_at) "
                                           "VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex, scope, mailbox or None, linkedin, reason, source, created))
                            existing.add(key)
            if "email_suppressions" in tables:
                for email, reason, created in current.execute("SELECT email,reason,created_at FROM email_suppressions"):
                    target.execute("INSERT INTO email_suppressions (email,reason,created_at) VALUES (?,?,?) "
                                   "ON CONFLICT(email) DO NOTHING", (email.strip().lower(), reason, created))
    except (LocalMaintenanceError, OSError, sqlite3.Error, TypeError, AttributeError):
        raise LocalMaintenanceError(
            "Your email opt-outs could not be carried over. Your workspace was not replaced. Keep your app and "
            "worker stopped and repair your current database or use a backup that supports your email opt-outs."
        ) from None


def _restore(archive: Path | str, root: Path, database: Path, *, relocate: bool = True) -> Path | None:
    from app.core.local_secrets import _atomic_write
    from app.db.local_sqlite import _version_marker

    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=root.parent) as temporary:
        staging = Path(temporary)
        _private_directory(staging)
        try:
            with tarfile.open(archive, "r:gz") as contents:
                members = contents.getmembers()
                names = set()
                components = {}
                for member in members:
                    path = PurePosixPath(member.name)
                    if (path.is_absolute() or ".." in path.parts or path in names
                            or not (member.isfile() or member.isdir())
                            or (member.name != "manifest.json" and (not path.parts or path.parts[0] != "data"))):
                        raise ValueError
                    _portable_name(member.name, components)
                    names.add(path)
                if sum(member.size for member in members) > shutil.disk_usage(staging).free:
                    raise LocalMaintenanceError("Your restore needs more free disk space. Free some space, then retry in Keys and connections, under Your data and recovery.")
                manifest_member = contents.getmember("manifest.json")
                if not manifest_member.isfile():
                    raise ValueError
                contents.extractall(staging, members=members, filter="data")
                for member in sorted(members, key=lambda item: (not item.isdir(), len(PurePosixPath(item.name).parts))):
                    extracted = staging / member.name
                    if os.name == "nt":
                        from app.core.local_windows import protect_private

                        protect_private(extracted, directory=member.isdir())
                    else:
                        extracted.chmod(0o700 if member.isdir() else (member.mode & 0o700) | 0o600)
            restored = staging / "data"
            replacement_database = restored / database.relative_to(root)
            manifest = _verify_archive(staging, replacement_database)
            _check_revision(replacement_database)
            if (restored / _LEGACY_PROVIDER_KEY).exists() or (restored / "local-key.bin").exists():
                raise LocalMaintenanceError(
                    "This backup contains an old recovery key. Keep it private, open it with the app that created it, "
                    "then create a new backup in Keys and connections, under Your data and recovery."
                )
            if relocate:
                return _finish_restore(restored, root, database, manifest)
            os.replace(restored, root)
            if manifest.get("app_version") and not _version_marker(database).exists():
                _atomic_write(_version_marker(database), manifest["app_version"].encode())
            return None
        except LocalMaintenanceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, tarfile.TarError):
            raise LocalMaintenanceError(
                "Your archive could not be verified. Open Keys and connections, under Your data and recovery, choose an intact backup for your configured database, then retry."
            ) from None


def _finish_restore(restored: Path, root: Path, database: Path, manifest: dict, *,
                    pending: bool = False, imported: bool = False) -> Path | None:
    from app.core.local_secrets import _atomic_write, _read_private
    from app.db.local_sqlite import _version_marker

    for store in manifest["databases"]:
        _relocate_database(restored / store, manifest["source_roots"], root)
    current = str(root.resolve())
    _record_roots(restored, [current, *[value for value in manifest["source_roots"] if value != current]])
    replacement_database = restored / database.relative_to(root)
    _carry_suppressions(root, database, replacement_database)
    if not _version_marker(replacement_database).exists() and manifest.get("app_version"):
        _atomic_write(_version_marker(replacement_database), manifest["app_version"].encode())
    for key, settings in ((Path("local-key.bin"), Path("local-settings.json")),
                          (_LEGACY_PROVIDER_KEY, _LEGACY_PROVIDER_KEY.with_name("settings.enc")),
                          (Path("vault/providers/keychain.scope"), Path("vault/providers/settings.json"))):
        if (restored / settings).is_file() and not (restored / key).exists():
            source = root / key
            if not source.parent.resolve().is_relative_to(root):
                raise LocalMaintenanceError(
                    "Your saved recovery file is linked outside your data folder. Remove the link, put your matching "
                    "private recovery file back in your data folder, then retry in Keys and connections, under Your data and recovery."
                )
            material = _read_private(source)
            if material is not None:
                _atomic_write(restored / key, material)
    if not imported:
        from cryptography.fernet import InvalidToken

        from app.core.local_secrets import _load
        from app.core.providers_local import _cipher, _read_private as _read_provider_settings

        try:
            if (restored / "local-settings.json").is_file() and _load(restored)[2] is None:
                raise ValueError
            legacy_settings = restored / _LEGACY_PROVIDER_KEY.with_name("settings.enc")
            if legacy_settings.is_file():
                _cipher(legacy_settings.parent).decrypt(_read_provider_settings(legacy_settings))
        except (OSError, ValueError, TypeError, InvalidTag, InvalidToken):
            raise LocalMaintenanceError(
                "Your backup settings could not be unlocked. Your workspace was not replaced. Unlock your original "
                "system keychain or use the matching, separately saved recovery key in a separate data folder. If you "
                "cannot recover the key, import a data-only ZIP from your original app in Keys and connections, under "
                "Your data and recovery."
            ) from None
    return _publish_restore(restored, root, pending=pending)


def _restore_database(archive: Path, database: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".upgrade-restore-", dir=database.parent) as temporary:
        snapshot = Path(temporary) / "database.sqlite3"
        with tarfile.open(archive, "r:gz") as contents:
            with contents.extractfile("manifest.json") as stream:
                manifest = json.load(stream)
            relative = PurePosixPath(manifest["database"])
            if manifest.get("version") not in (1, 2) or relative.is_absolute() or ".." in relative.parts or relative.name != database.name:
                raise LocalMaintenanceError("Choose your automatic backup for this data folder, then retry your restore.")
            member = contents.getmember(f"data/{relative.as_posix()}")
            if not member.isfile():
                raise LocalMaintenanceError("Choose an intact automatic backup, then retry your restore.")
            with contents.extractfile(member) as source, snapshot.open("xb") as target:
                shutil.copyfileobj(source, target)
        if manifest["version"] == 2:
            hashes = manifest.get("files")
            with snapshot.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if not isinstance(hashes, dict) or hashes.get(relative.as_posix()) != digest:
                raise LocalMaintenanceError("Your automatic backup could not be verified. Keep your data folder and choose an intact backup, then retry your restore.")
        _check_database(snapshot)
        with closing(_connection(snapshot)) as source, closing(sqlite3.connect(database, timeout=5)) as target:
            _copy_database(source, target)
        _check_database(database)


def upgrade(output: Path | str | None = None) -> Path:
    _root, database = _paths()
    with _offline():
        return _upgrade(output, database)


def _upgrade(output: Path | str | None, database: Path) -> Path:
    from app.db.local_sqlite import migrate_sqlite
    from sqlalchemy.engine import URL

    archive = migrate_sqlite(URL.create("sqlite", database=str(database)).render_as_string(),
                             backup=lambda: backup(output), progress=lambda message: print(message, flush=True))
    _check_database(database)
    return archive


def _command_ok(command: list[str]) -> bool:
    if not shutil.which(command[0]):
        return False
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _port_free(port: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as listener:
                listener.bind((host, port))
        except OSError as error:
            if family == socket.AF_INET6 and error.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL}:
                continue
            return False
    return True


def _doctor_ports() -> tuple[bool, str]:
    import httpx

    from app.config import REPO_ROOT
    from app.health import SERVICE

    owned = set()
    if _command_ok(["docker", "compose", "version"]):
        try:
            result = subprocess.run(["docker", "compose", "ps", "--format", "json"], cwd=REPO_ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False, text=True)
            if result.returncode == 0 and result.stdout.strip():
                rows = json.loads(result.stdout) if result.stdout.lstrip().startswith("[") else [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                owned.update(publisher["PublishedPort"] for row in rows for publisher in (row.get("Publishers") or [])
                             if publisher.get("Protocol") == "tcp")
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, AttributeError):
            pass
    with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
        def health(url: str):
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return {}
                payload = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    payload.extend(chunk)
                    if len(payload) > 16384:
                        return {}
                return json.loads(payload)

        try:
            core = health("http://127.0.0.1:3300/health")
            if isinstance(core, dict) and core.get("service") == SERVICE and core.get("status") == "ok":
                owned.add(3300)
                instance = core.get("desktop_instance")
                if isinstance(instance, str) and instance:
                    console = health("http://127.0.0.1:3100/api/health")
                    if isinstance(console, dict) and console.get("desktop_instance") == instance and console.get("ok") is True:
                        owned.add(3100)
        except (httpx.HTTPError, ValueError):
            pass
    statuses = {port: "free" if _port_free(port) else "used by your app" if port in owned else "used by another program"
                for port in (3100, 3300, 55432)}
    okay = all(statuses[port] != "used by another program" for port in (3100, 3300))
    message = "; ".join(f"{port}: {status}" for port, status in statuses.items())
    return okay, message + ". Port 55432 is only for optional developer PostgreSQL. " + (
        "Open your desktop app, then run make doctor again." if okay else
        "Check the other program before stopping it. Run lsof -nP -iTCP:3100 -iTCP:3300 -iTCP:55432 -sTCP:LISTEN, then make doctor.")


def _doctor_reader() -> tuple[bool, str]:
    import httpx

    from app.config import data_root
    from app.core.local_capabilities import enabled, blocked_message

    saved = data_root() / "local-settings.json"
    if not saved.exists() and not saved.is_symlink():
        return True, "Your built-in page reader is ready. Ask me to read a public page in your app, then run make doctor."
    if not enabled("research"):
        return False, blocked_message("research") + " Run make doctor."
    from app.signals.local_reader import _firecrawl

    try:
        endpoint = _firecrawl()
    except ValueError as exc:
        return False, str(exc) + " Run make doctor."
    if endpoint is None:
        return True, "Your built-in page reader is ready. Ask me to read a public page in your app, then run make doctor."
    if endpoint is not None:
        try:
            with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
                with client.stream("GET", endpoint[0] + "/") as response:
                    if 200 <= response.status_code < 300:
                        return True, "Your self-hosted Firecrawl is reachable. Try your page in your app, then run make doctor."
        except httpx.HTTPError:
            pass
    return False, "Open Keys and connections > Search and check your Firecrawl URL and key, or clear the URL to use your built-in page reader, then run make doctor."


def _doctor_email() -> tuple[bool, str]:
    from app.config import data_root
    from app.gtm import local_mail
    from app.core.local_env_import import _IMPORTING, env_path, import_local_env

    saved = data_root() / "local-settings.json"
    configuration = env_path()
    if saved.exists() or saved.is_symlink() or configuration.exists() or configuration.is_symlink():
        importing = getattr(_IMPORTING, "active", False)
        _IMPORTING.active = False
        try:
            import_local_env(strict=True)
        except (OSError, ValueError):
            return False, "Your .env settings could not be imported. Open Settings > Keys and connections, correct the file shown there, then run make doctor. No mail was sent."
        finally:
            _IMPORTING.active = importing
    values = local_mail._values() if saved.exists() or saved.is_symlink() else {}
    if any(values.get(name) for name in (
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"
    )):
        try:
            local_mail.settings()
            return True, "Your outreach email uses your configured SMTP mailbox. Your account email stays local. No mail was sent. Check your mailbox settings in your app, then run make doctor."
        except ValueError as exc:
            return False, str(exc) + " Then run make doctor. No mail was sent."
    if os.environ.get("EMAIL_PROVIDER", "log").strip().lower() == "log":
        return True, "Your email is saved locally, not sent. Open Keys and connections > Your mail account and save your Outgoing mail host, Mailbox address and Mail app password before asking for outreach, then run make doctor."
    return False, "Open Keys and connections > Your mail account and save your Outgoing mail host, Mailbox address and Mail app password, then run make doctor. No mail was sent."


def doctor() -> dict[str, tuple[bool, str]]:
    from app.core.local_env_import import _IMPORTING

    importing = getattr(_IMPORTING, "active", False)
    _IMPORTING.active = True
    try:
        return _doctor()
    finally:
        _IMPORTING.active = importing


def _doctor() -> dict[str, tuple[bool, str]]:
    if not is_local():
        raise LocalMaintenanceError("Use this check only in your local installation. Run make doctor from your local checkout.")
    from app.config import data_root, settings
    from app.core.providers_local import LocalProviderError, load_local_config
    from app.health import expected_schema_revision
    from app.secrets.dotenv_file import read_env_file, secrets_env_path
    from app.worker.health import healthy

    root = data_root().resolve()
    for name, value in read_env_file().items():
        os.environ.setdefault(name, value)
    mail = _doctor_email()
    database_ok = False
    try:
        _root, database = _paths()
        _check_database(database)
        with closing(_connection(database)) as connection:
            head = expected_schema_revision()
            database_ok = bool(head) and connection.execute("SELECT version_num FROM alembic_version").fetchall() == [(head,)]
    except (sqlite3.Error, LocalMaintenanceError):
        pass
    keys_ok = False
    if (root / "local-settings.json").is_file() or (root / "vault/providers").is_dir():
        try:
            load_local_config()
            keys_ok = True
        except (LocalProviderError, OSError):
            pass
    disk_root = root
    while not disk_root.exists() and disk_root != disk_root.parent:
        disk_root = disk_root.parent
    try:
        free_gib = shutil.disk_usage(disk_root).free / 1024**3
        disk = (free_gib >= 1, f"You have {free_gib:.1f} GiB free. Keep at least 1 GiB free without deleting your app data, then run make doctor.")
    except OSError:
        disk = (False, "Your free space could not be checked. Check your data folder permissions and available space, then run make doctor.")
    docker_ok = _command_ok(["docker", "--version"])
    compose_ok = _command_ok(["docker", "compose", "version"])
    return {
        "database": (database_ok, "Open your desktop app to initialize or upgrade your saved data, then run make doctor. Keep your existing data."),
        "keys": (keys_ok, ("Your model configuration is saved. " if keys_ok else
                 "Check your provider, key, URL and chat model in your app's setup. Keep your existing provider files and machine secret; restore them from your private backup if unreadable. ") +
                 "Loopback model servers do not need a key. No model call was made. Run make doctor after saving."),
        "reader": _doctor_reader(),
        "mail": mail,
        "worker": (healthy(), "Open or restart your desktop app, then run make doctor."),
        "env": (secrets_env_path().is_file() or keys_ok, "Your private .env settings file is present. Run make doctor after any changes." if secrets_env_path().is_file() else
                "Your optional private .env settings file is missing. Your app can use encrypted model settings instead. Save your settings in your app and use the same data folder for make doctor."),
        "guessed_emails": (os.environ.get("GUESSED_EMAILS", settings.guessed_emails).strip().lower() == "off",
                           "Keep email guessing off in your saved settings, restart your app, then run make doctor. Your outreach safety checks stay in place."),
        "disk": disk,
        "ports": _doctor_ports(),
        "docker": (True, ("Docker is present." if docker_ok else "Docker is missing.") + " You do not need it for your desktop app. If you choose developer containers, run docker --version."),
        "compose": (True, ("Compose is present." if compose_ok else "Compose is missing.") + " You do not need it for your desktop app. If you choose developer containers, run docker compose version."),
    }


def main(argv: list[str] | None = None) -> int:
    from alembic.util.exc import CommandError
    from sqlalchemy.exc import SQLAlchemyError

    parser = argparse.ArgumentParser(prog="python -m app.cli_local", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "upgrade"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, help="Your backup folder, outside your data directory.")
    commands.add_parser("restore").add_argument("archive", type=Path)
    commands.add_parser("cancel-restore", help="Cancel a queued replacement without changing your workspace.")
    commands.add_parser("doctor").add_argument("--data-dir", type=Path, help="Your desktop app's data folder, not a new empty folder.")
    commands.add_parser("seed-demo", help="Save your optional demo without keys or network access.")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "backup":
            skipped: set[str] = set()
            archive = backup(arguments.output, skipped=skipped)
            if skipped:
                print(f"Your backup is partial: {archive}. {len(skipped)} linked or special files were skipped. "
                      "Copy any files you need into your data folder as regular files, then try again.")
            else:
                print(f"Your backup is ready: {archive}. Keep it private and keep your matching recovery key separate.")
        elif arguments.command == "restore":
            recovery = restore(arguments.archive)
            print("Your data is restored. Restart your app and worker.")
            if recovery is not None:
                print(f"Your previous data is saved at {recovery}. Keep it until you verify your restored data.")
        elif arguments.command == "upgrade":
            upgrade(arguments.output)
            print("Your upgrade is complete. Restart your app and worker, then run doctor.")
        elif arguments.command == "cancel-restore":
            from app.core.local_data import cancel_pending

            cancel_pending()
            print("Your queued replacement is cleared. Reopen your app to return to your saved workspace.")
        elif arguments.command == "seed-demo":
            from app.demo_seed import seed_demo

            venture_id = seed_demo()
            print(
                f"Your Demo venture is ready ({venture_id}). Open it to view your sample summary and experiment plan. "
                "Nothing has started. To remove it, open your demo's Settings and choose Delete venture."
            )
        else:
            if arguments.data_dir is not None:
                if not is_local():
                    raise LocalMaintenanceError("Use this check only in your local installation. Run make doctor from your local checkout.")
                os.environ["COMPANY_OS_DATA_DIR"] = str(arguments.data_dir.expanduser().resolve())
            checks = doctor()
            next_command = "make doctor" + (" DATA_DIR=" + shlex.quote(str(arguments.data_dir.expanduser().resolve()))
                                           if arguments.data_dir is not None else "")
            labels = {"database": "saved data", "keys": "model connection", "reader": "page reading", "mail": "email",
                      "worker": "background work", "env": "saved settings", "guessed_emails": "email guessing",
                      "disk": "free space", "ports": "local ports", "docker": "optional developer Docker", "compose": "optional developer Compose"}
            for name, (okay, next_step) in checks.items():
                print(f"Your {labels[name]}: {'OK' if okay else 'needs attention'}. {next_step.replace('make doctor', next_command)}")
            return 0 if all(check[0] for check in checks.values()) else 1
    except LocalMaintenanceError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, SQLAlchemyError, CommandError, ValueError, TypeError, RuntimeError):
        print(
            "Your maintenance command could not finish. Keep your backup and previous data, "
            "check your configuration and folder permissions, then retry with your app and worker stopped.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    from app.cli_local import main

    raise SystemExit(main())
