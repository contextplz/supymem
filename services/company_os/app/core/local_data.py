"""Local data transfers stage replacements until all app processes have stopped."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from app.cli_local import (
    LocalMaintenanceError,
    _backup,
    _backup_directory,
    _archive_manifest,
    _check_database,
    _check_revision,
    _finish_restore,
    _link,
    _paths,
    _plaintext_settings,
    _portable_name,
    _private_directory,
    _recover_restore,
    _restore,
    _stamp,
    _sync_directory,
    _verify_archive,
)


def _merge_import_settings(root: Path, path: Path) -> bytes | None:
    from cryptography.exceptions import InvalidTag

    from app.api.local_settings import SECRET_FIELDS
    from app.core.local_secrets import _load, _read_private

    current_path = root / path.name
    try:
        if not path.exists():
            return _read_private(current_path)
        incoming = json.loads(_read_private(path))
        _check_public_settings(incoming)
        if not current_path.exists() and not current_path.is_symlink():
            return None
        current, previous, keys = _load(root)
        previous_model = previous.get("model", {})
        previous_models = previous_model.get("providers", {})
        previous_model_sources = previous_model.get("sources", {})
        if (not isinstance(previous_model_sources, dict) or not isinstance(previous_models, dict)
                or any(not isinstance(fields, dict) for fields in previous_models.values())):
            raise ValueError
    except (OSError, ValueError, TypeError, InvalidTag):
        raise LocalMaintenanceError("Your settings could not be read. Re-enter any keys marked unreadable in Keys and connections, "
                                    "then choose your ZIP again under Your data and recovery.") from None

    restored = incoming["values"]
    # A workspace import is not permission to send a saved key to a different server.
    destinations = (
        ("smtp", ("password",), ("host", "port", "user", "imap_host", "imap_port"), "Your mail account"),
        ("search", ("api_key",), ("provider",), "Your search and page reader"),
        ("search", ("firecrawl_key",), ("firecrawl_url",), "Your search and page reader"),
        ("publishing", SECRET_FIELDS["publishing"], ("provider", "project", "bucket", "region", "prefix", "site_url"),
         "Your page publishing account"),
    )
    for section, names, fields, screen in destinations:
        if any(restored.get(section, {}).get(field) != previous.get(section, {}).get(field) for field in fields):
            if keys is None or any(keys.get(section, {}).get(name) for name in names):
                raise LocalMaintenanceError(f"Your ZIP changes a saved connection. Open Keys and connections, under {screen}, "
                                            "match the connection in your ZIP and re-enter its key, then retry the import.")
    restored_model = restored.get("model", {})
    restored_models = restored_model.get("providers", {})
    for provider in previous_models.keys() | restored_models.keys():
        if previous_models.get(provider, {}).get("base_url") != restored_models.get(provider, {}).get("base_url"):
            if keys is None or keys.get("model", {}).get(provider):
                raise LocalMaintenanceError("Your ZIP changes an AI connection. Open Keys and connections, under Your AI provider, "
                                            "match the address in your ZIP and re-enter its key, then retry the import.")
        elif (provider in previous_models and provider in restored_models
                and (keys is None or provider in keys.get("model", {}))):
            name = f"{provider}.api_key"
            source = previous_model_sources.get(name)
            if source in ("app", "env"):
                restored_model.setdefault("sources", {})[name] = source

    incoming["secrets"] = current["secrets"]
    sources = incoming.setdefault("sources", {})
    for section, fields in current["sources"].items():
        for name in fields.keys() & set(SECRET_FIELDS.get(section, ())):
            sources.setdefault(section, {})[name] = fields[name]
    return json.dumps(incoming).encode()


def _private(path: PurePosixPath, root: Path) -> bool:
    return ((root / path).resolve() == Path(os.environ.get("SUPYMEM_LOCAL_PROXY_SECRET_FILE") or root / "owner-session.secret").resolve()
            or _plaintext_settings(path, root) or any(part in {
        "vault", "local-key.bin", "owner-session.secret", "local-settings.json", "local-settings.lock",
        "secrets.bin", "secrets.json", "master.key",
        ".aws", ".ssh", ".codex",
    } or part.endswith("-app-version") for part in path.parts))


def _public_model_source_names(model: dict) -> set[str]:
    providers = model.get("providers")
    if not isinstance(providers, dict):
        raise ValueError
    names = {"provider"}
    scalar_fields = {"base_url", "catalog_model", "context_window"}
    for provider, fields in providers.items():
        if not isinstance(fields, dict) or set(fields) - (scalar_fields | {"models"}):
            raise ValueError
        models = fields.get("models", {})
        if not isinstance(models, dict):
            raise ValueError
        names.update(f"{provider}.{name}" for name in scalar_fields & fields.keys())
        names.update(f"{provider}.models.{role}" for role in models)
    return names


def _check_public_settings(payload: dict) -> None:
    from app.api.local_settings import SCHEMAS, SECRET_FIELDS

    if (not isinstance(payload, dict) or not {"values", "secrets"} <= set(payload)
            or set(payload) - {"values", "secrets", "sources"}
            or payload["secrets"] is not None or not isinstance(payload["values"], dict)):
        raise ValueError
    sources = payload.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError
    for section, fields in sources.items():
        values = payload["values"].get(section)
        if (not isinstance(fields, dict) or not isinstance(values, dict)
                or any(name not in values or not isinstance(source, str) or source not in {"app", "env"}
                       for name, source in fields.items())):
            raise ValueError
    for section, values in payload["values"].items():
        if not isinstance(values, dict):
            raise ValueError
        if section == "capabilities":
            if any(not isinstance(value, bool) for value in values.values()):
                raise ValueError
        elif section == "model":
            from app.core.providers_local import _validate_document, _validated_settings, validate_endpoint

            public_sources = _public_model_source_names(values)
            _validate_document(values)
            for provider, fields in values["providers"].items():
                if fields.get("base_url"):
                    validate_endpoint(fields["base_url"])
                if "catalog_model" in fields:
                    _validated_settings({"provider": provider, "catalog_model": fields["catalog_model"]})
            if set(values.get("sources", {})) - public_sources:
                raise ValueError
        elif section == "runtime":
            if set(values) - {"offline", "cassette_mode"}:
                raise ValueError
            if "offline" in values and str(values["offline"]).lower() not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
                raise ValueError
            if "cassette_mode" in values and (not isinstance(values["cassette_mode"], str)
                    or values["cassette_mode"] not in {"off", "record", "replay", "auto"}):
                raise ValueError
        else:
            if section not in SCHEMAS or any(name in values for name in SECRET_FIELDS[section]):
                raise ValueError
            SCHEMAS[section].model_validate(values)


def export_data(*, skipped: set[str] | None = None) -> Path:
    from app.core.local_process import open_private

    # shortcut: SQLite is snapshotted but live files are copied, add writer quiescence if concurrent file edits must be exported atomically.
    root, database = _paths()
    destination = root.with_name(f"{root.name}-exports")
    _private_directory(destination)
    archive = destination / f"data-{_stamp()}.zip"
    with tempfile.TemporaryDirectory(dir=destination) as temporary:
        snapshot = _backup(root, database, temporary, skipped=skipped)
        pending = Path(temporary) / "data.zip"
        with os.fdopen(open_private(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as target:
            with tarfile.open(snapshot) as source, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
                with source.extractfile("manifest.json") as stream:
                    manifest = json.load(stream)
                exported = {}
                for member in source:
                    if member.name in {"manifest.json", "data"}:
                        continue
                    relative = PurePosixPath(member.name).relative_to("data")
                    public_settings = relative == PurePosixPath("local-settings.json")
                    if _private(relative, root) and not public_settings:
                        continue
                    entry = zipfile.ZipInfo(member.name + ("/" if member.isdir() else ""))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = ((stat.S_IFDIR | 0o700) if member.isdir() else (stat.S_IFREG | (member.mode & 0o700) | 0o600)) << 16
                    if member.isdir():
                        output.writestr(entry, b"")
                        continue
                    with source.extractfile(member) as stream:
                        if public_settings:
                            payload = json.load(stream)
                            payload["secrets"] = None
                            model = payload["values"].get("model")
                            if isinstance(model, dict) and isinstance(model.get("sources"), dict):
                                public_sources = _public_model_source_names(model)
                                model["sources"] = {name: source for name, source in model["sources"].items()
                                                    if name in public_sources}
                            if "sources" in payload:
                                payload["sources"] = {
                                    section: {name: source for name, source in fields.items()
                                              if name in payload["values"].get(section, {})}
                                    for section, fields in payload["sources"].items()
                                    if section in payload["values"]
                                }
                            _check_public_settings(payload)
                            content = json.dumps(payload).encode()
                            output.writestr(entry, content)
                            exported[relative.as_posix()] = hashlib.sha256(content).hexdigest()
                        else:
                            with output.open(entry, "w", force_zip64=True) as item:
                                shutil.copyfileobj(stream, item)
                            exported[relative.as_posix()] = manifest["files"][relative.as_posix()]
                manifest["files"] = exported
                manifest["databases"] = [store for store in manifest["databases"] if store in exported]
                entry = zipfile.ZipInfo("manifest.json")
                entry.external_attr = (stat.S_IFREG | 0o600) << 16
                output.writestr(entry, json.dumps(manifest))
            target.flush()
            os.fsync(target.fileno())
        os.link(pending, archive)
    return archive


def _protect_restored_tree(root: Path) -> None:
    if os.name == "nt":
        from app.core.local_windows import protect_private

    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        directory = stat.S_ISDIR(metadata.st_mode)
        if _link(metadata) or not (directory or stat.S_ISREG(metadata.st_mode)):
            raise LocalMaintenanceError("Your restore contains a link or special file. Choose an intact copy in "
                                        "Keys and connections, under Your data and recovery, then retry.")
        if os.name == "nt":
            protect_private(path, directory=directory)
        else:
            path.chmod(0o700 if directory else (metadata.st_mode & 0o700) | 0o600)


def _unpack_zip(archive: Path, staging: Path, root: Path, database: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if sum(member.file_size for member in members) > shutil.disk_usage(staging).free:
                raise LocalMaintenanceError("Your import needs more free disk space. Free some space, then retry.")
            names = set()
            components: dict[str, str] = {}
            for member in members:
                path = PurePosixPath(member.filename)
                kind = stat.S_IFMT(member.external_attr >> 16)
                if (path.is_absolute() or ".." in path.parts
                        or path in names or member.flag_bits & 1
                        or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or (path.parts and path.parts[0] == "data" and _private(path.relative_to("data"), root)
                            and (str(path) != "data/local-settings.json" or member.is_dir() or member.file_size > 1_048_576))
                        or (str(path) != "manifest.json" and (len(path.parts) < 2 or path.parts[0] != "data"))):
                    raise ValueError
                _portable_name(member.filename.removesuffix("/"), components)
                names.add(path)
                destination = staging / path
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with source.open(member) as stream, destination.open("xb") as target:
                        os.chmod(destination, ((member.external_attr >> 16) & 0o700) | 0o600)
                        shutil.copyfileobj(stream, target)
        if (staging / "data").is_dir():
            _protect_restored_tree(staging / "data")
        public_settings = staging / "data/local-settings.json"
        if public_settings.exists():
            _check_public_settings(json.loads(public_settings.read_text()))
        _verify_archive(staging, staging / "data" / database.relative_to(root), require_app_version=True)
    except LocalMaintenanceError:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile, RuntimeError):
        raise LocalMaintenanceError("Your archive could not be verified. Open Keys and connections, under Your data and recovery, choose an intact data export, then retry.") from None


def _pending(root: Path) -> Path:
    return root.with_name(f".{root.name}-pending-restore")


def create_backup(*, skipped: set[str] | None = None) -> str:
    root, database = _paths()
    return _backup(root, database, skipped=skipped).name


def backups() -> list[str]:
    root, _database = _paths()
    return sorted(path.name for path in _backup_directory(root).glob("backup-*.tar.gz")
                  if path.is_file() and not path.is_symlink())


def queue_restore(name: str) -> None:
    root, database = _paths()
    if name not in backups():
        raise LocalMaintenanceError("Choose one of your listed backups, then retry your restore.")
    _queue(_backup_directory(root) / name, root, database, imported=False)


def queue_import(archive: Path) -> None:
    root, database = _paths()
    _queue(archive, root, database, imported=True)


def _queue(archive: Path, root: Path, database: Path, *, imported: bool) -> None:
    from app.core.local_secrets import _atomic_write

    pending = _pending(root)
    if pending.exists() or pending.is_symlink():
        raise LocalMaintenanceError("Your restore is already ready. Quit and reopen your app to finish it.")
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root.parent) as temporary:
        staging = Path(temporary)
        _private_directory(staging)
        if imported:
            _unpack_zip(archive, staging, root, database)
            _merge_import_settings(root, staging / "data/local-settings.json")
        else:
            _restore(archive, staging / "data", staging / "data" / database.relative_to(root), relocate=False)
        _check_revision(staging / "data" / database.relative_to(root), imported=imported)
        if not imported:
            restored = staging / "data"
            manifest = _archive_manifest(restored, restored / database.relative_to(root))
            _atomic_write(staging / "manifest.json", json.dumps(manifest).encode())
        _atomic_write(staging / "request.json", json.dumps({"imported": imported}).encode())
        os.rename(staging, pending)
        _sync_directory(root.parent)


def apply_pending() -> Path | None:
    from app.core.local_process import process_lock
    from app.db.local_sqlite import _check_app_version

    root, database = _paths()
    pending = _pending(root)
    with process_lock(exclusive=True):
        _recover_restore(root)
        if not pending.exists() and not pending.is_symlink():
            return None
        _check_app_version(database)
        try:
            return _apply_pending(root, database, pending)
        except LocalMaintenanceError as error:
            message = str(error)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            message = ("Your restore request could not be verified. Check your free disk space, then pick an intact "
                       "archive in Keys and connections, under Your data and recovery.")
        raise LocalMaintenanceError(
            f"{message} To cancel this replacement, stop your app and worker, run "
            "`python -m app.cli_local cancel-restore`, then reopen your app."
        ) from None


def _apply_pending(root: Path, database: Path, pending: Path) -> Path | None:
    from app.core.local_secrets import _atomic_write
    from app.db.local_sqlite import _version_marker

    if pending.is_symlink():
        raise LocalMaintenanceError("Your restore folder is a link. Remove the link, then choose your backup again.")
    request = json.loads((pending / "request.json").read_text())
    if (not isinstance(request, dict) or set(request) != {"imported"}
            or not isinstance(request["imported"], bool)):
        raise ValueError
    pending_database = pending / "data" / database.relative_to(root)
    incoming = _verify_archive(pending, pending_database, require_app_version=request["imported"])
    _check_revision(pending_database, imported=request["imported"])
    settings = _merge_import_settings(root, pending / "data/local-settings.json") if request["imported"] else None
    try:
        _check_database(database)
    except LocalMaintenanceError:
        archive = None
    else:
        archive = _backup(root, database)
    with tempfile.TemporaryDirectory(prefix=".apply-", dir=root.parent) as temporary:
        staging = Path(temporary)
        _private_directory(staging)
        restored = staging / "data"
        shutil.copytree(pending / "data", restored, symlinks=True)
        if request["imported"]:
            for source in sorted(root.rglob("*")):
                relative = source.relative_to(root)
                if (source.is_file() and _private(PurePosixPath(relative.as_posix()), root)
                        and source != _version_marker(database)
                        and relative != Path("local-settings.json")
                        and not _plaintext_settings(PurePosixPath(relative.as_posix()), root)):
                    if source.is_symlink():
                        raise LocalMaintenanceError("Your saved settings contain a link. Replace it with a regular file, then retry.")
                    destination = restored / relative
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(source, destination)
            if settings is not None:
                _atomic_write(restored / "local-settings.json", settings)
        _protect_restored_tree(restored)
        _keep_backup_directory(restored)
        _finish_restore(restored, root, database, incoming, pending=True, imported=request["imported"])
    return archive


def _keep_backup_directory(restored: Path) -> None:
    from app.core.local_secrets import _atomic_write, _read_private, read_settings

    try:
        current = read_settings("data")
        path = restored / "local-settings.json"
        content = _read_private(path)
        if content is None and "backup_directory" not in current["values"]:
            return
        payload = json.loads(content) if content is not None else {"values": {}, "secrets": None}
        for field in ("values", "sources"):
            saved = payload.setdefault(field, {}).setdefault("data", {})
            previous = current.get(field, {})
            if "backup_directory" in previous:
                saved["backup_directory"] = previous["backup_directory"]
            else:
                saved.pop("backup_directory", None)
        _atomic_write(path, json.dumps(payload).encode())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise LocalMaintenanceError(
            "Your backup folder setting could not be kept. Check Full backup folder path in Keys and connections, "
            "under Your data folder and backup, before trying again."
        ) from None


def cancel_pending() -> None:
    from app.cli_local import _offline

    root, _database = _paths()
    with _offline():
        _recover_restore(root)
        pending = _pending(root)
        if pending.is_symlink():
            pending.unlink()
        elif pending.exists():
            retired = pending.with_name(f"{pending.name}-canceled-{_stamp()}")
            os.replace(pending, retired)
            shutil.rmtree(retired, ignore_errors=True)
