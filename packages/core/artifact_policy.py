"""Bounded source/artifact transport. Paths never authorize host filesystem access."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .redact import redact_string


class ArtifactRejected(ValueError):
    """A safe machine code, never file content or a credential."""


@dataclass(frozen=True)
class ArtifactLimits:
    max_files: int = 2000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    max_archive_bytes: int = 32 * 1024 * 1024
    max_ratio: int = 100
    max_depth: int = 20

    def __post_init__(self):
        for value in self.__dict__.values():
            if type(value) is not int or value <= 0:
                raise ValueError("invalid_artifact_limits")


DEFAULT_LIMITS = ArtifactLimits()
_SECRET_NAMES = {".git", ".ssh", ".aws", ".azure", ".npmrc", ".pypirc", ".netrc", ".openhands",
                 "credentials", "credentials.json", "id_rsa", "id_ed25519"}


def is_credential_path(name: str) -> bool:
    """Return whether a relative artifact path names credential material.

    Exporters may use this predicate to omit runtime-owned credentials before
    creating a source snapshot. Importers still reject these paths through
    ``safe_name`` so a remote archive can never smuggle one back in.
    """
    if not isinstance(name, str):
        return False
    parts = name.split("/")
    return any(p.casefold() in _SECRET_NAMES or p.casefold().startswith(".env") or
               p.casefold().endswith((".pem", ".p12", ".pfx", ".key")) for p in parts)


def validate_windows_name(name: str) -> None:
    for part in name.split("/"):
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if (part.endswith((".", " ")) or any(character in ':<>"|?*' for character in part)
                or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789\u00b9\u00b2\u00b3"):
            raise ArtifactRejected("artifact_path_invalid")


def safe_name(name: str, limits: ArtifactLimits = DEFAULT_LIMITS) -> str:
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 1024:
        raise ArtifactRejected("artifact_path_invalid")
    if "\\" in name or ":" in name or any(unicodedata.category(c).startswith("C") for c in name):
        raise ArtifactRejected("artifact_path_invalid")
    parts = name.split("/")
    if len(parts) > limits.max_depth or any(p in {"", ".", ".."} or len(p.encode("utf-8")) > 255 for p in parts):
        raise ArtifactRejected("artifact_path_invalid")
    if unicodedata.normalize("NFC", name) != name:
        raise ArtifactRejected("artifact_path_not_normalized")
    validate_windows_name(name)
    if is_credential_path(name):
        raise ArtifactRejected("artifact_credential_path")
    return name


def _record_components(name: str, seen: dict[str, str]) -> None:
    parts = name.split("/")
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        folded = prefix.casefold()
        if folded in seen and seen[folded] != prefix:
            raise ArtifactRejected("artifact_path_collision")
        seen[folded] = prefix


def inspect_blob(blob: bytes, *, limits: ArtifactLimits = DEFAULT_LIMITS, exact_secrets: tuple[str, ...] = ()) -> None:
    if len(blob) > limits.max_file_bytes:
        raise ArtifactRejected("artifact_file_too_large")
    text = blob.decode("utf-8", errors="ignore")
    if redact_string(text) != text or any(secret and secret in text for secret in exact_secrets):
        raise ArtifactRejected("artifact_secret_detected")


def _system_real(path: str) -> str:
    import tempfile

    aliases = [tempfile.gettempdir(), "/tmp", "/var"]
    try:
        from app.core.local_mode import is_local
    except ImportError:
        local = os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
    else:
        local = is_local()
    if local:
        aliases.extend(os.environ[name] for name in ("HOME", "COMPANY_OS_DATA_DIR") if os.environ.get(name))
    for alias in sorted({os.path.abspath(alias) for alias in aliases}, key=len, reverse=True):
        real = os.path.realpath(alias)
        if real != alias and (path == alias or path.startswith(alias.rstrip(os.sep) + os.sep)):
            return real + path[len(alias.rstrip(os.sep)):]
    return path


@contextmanager
def directory_fd(path: Path):
    """Resolve configured roots, then refuse every symlink below them."""
    path = Path(_system_real(os.path.abspath(path)))
    if os.name == "nt":
        with _windows_directory(path) as directory:
            yield directory
        return
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    except OSError as exc:
        raise ArtifactRejected("artifact_directory_unsafe") from exc
    finally:
        os.close(fd)


def _windows_handle(path: Path, flags: int, *, directory: bool = False):
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    access = 0x80 if directory else 0x40000000 if flags & os.O_WRONLY else 0x80000000
    handle = create(str(path), access, 3 if directory else 1, None,
        1 if flags & os.O_EXCL else 3, 0x200000 | (0x2000000 if directory else 0x80), None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    information = (ctypes.c_uint32 * 13)()
    query = kernel.GetFileInformationByHandle
    query.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    query.restype = ctypes.c_int
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    if (not query(handle, information) or information[0] & 0x400
            or bool(information[0] & 0x10) != directory or not directory and information[10] != 1):
        close(handle)
        raise ArtifactRejected("artifact_directory_unsafe" if directory else "artifact_file_type_unsafe")
    return handle, close


@contextmanager
def _windows_directory(path: Path, *, create: bool = False):
    handles = []
    path = Path(os.path.abspath(path))
    try:
        for directory in [*reversed(path.parents), path]:
            if create and directory != Path(path.anchor):
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            handles.append(_windows_handle(directory, os.O_RDONLY, directory=True))
        yield path
    except OSError as exc:
        raise ArtifactRejected("artifact_directory_unsafe") from exc
    finally:
        for handle, close in reversed(handles):
            close(handle)


def _windows_file(path: Path, flags: int) -> int:
    import msvcrt

    handle, close = _windows_handle(path, flags)
    try:
        return msvcrt.open_osfhandle(handle, os.O_BINARY | flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR))
    except BaseException:
        close(handle)
        raise


def _open_at(parent, leaf: str, flags: int) -> int:
    if os.name == "nt":
        return _windows_file(parent / leaf, flags)
    return os.open(leaf, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)


@contextmanager
def _parent(root: Path, name: str, *, create: bool = False):
    safe_name(name)
    if os.name == "nt":
        with directory_fd(root), _windows_directory(root.joinpath(*name.split("/")[:-1]), create=create) as parent:
            yield parent, name.split("/")[-1]
        return
    with directory_fd(root) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in name.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            yield fd, name.split("/")[-1]
        except OSError as exc:
            raise ArtifactRejected("artifact_path_unsafe") from exc
        finally:
            os.close(fd)


def read_file(root: Path, name: str, *, limits: ArtifactLimits = DEFAULT_LIMITS,
              exact_secrets: tuple[str, ...] = ()) -> bytes:
    safe_name(name, limits)
    with _parent(root, name) as (parent_fd, leaf):
        fd = _open_at(parent_fd, leaf, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ArtifactRejected("artifact_file_type_unsafe")
            if before.st_size > limits.max_file_bytes:
                raise ArtifactRejected("artifact_file_too_large")
            blob = stream.read(limits.max_file_bytes + 1)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ArtifactRejected("artifact_file_changed")
    inspect_blob(blob, limits=limits, exact_secrets=exact_secrets)
    return blob


def write_file(root: Path, name: str, blob: bytes, *, limits: ArtifactLimits = DEFAULT_LIMITS,
               exact_secrets: tuple[str, ...] = ()) -> None:
    safe_name(name, limits)
    inspect_blob(blob, limits=limits, exact_secrets=exact_secrets)
    with _parent(root, name, create=True) as (parent_fd, leaf):
        # Refuse overwrites and links. The caller swaps a vetted snapshot after
        # acceptance; a remote file must not replace arbitrary host content.
        fd = _open_at(parent_fd, leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(blob)
        except BaseException:
            if os.name == "nt":
                os.unlink(parent_fd / leaf)
            else:
                os.unlink(leaf, dir_fd=parent_fd)
            raise


def replace_file(root: Path, name: str, blob: bytes, *, limits: ArtifactLimits = DEFAULT_LIMITS,
                 exact_secrets: tuple[str, ...] = ()) -> None:
    """Replace a vetted source file without opening or following its old inode."""
    import secrets
    safe_name(name, limits)
    inspect_blob(blob, limits=limits, exact_secrets=exact_secrets)
    with _parent(root, name, create=True) as (parent_fd, leaf):
        try:
            old = os.stat(parent_fd / leaf, follow_symlinks=False) if os.name == "nt" else os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(old.st_mode) or old.st_nlink != 1 or getattr(old, "st_file_attributes", 0) & 0x400:
                raise ArtifactRejected("artifact_file_type_unsafe")
        except FileNotFoundError:
            pass
        temp = ".artifact-" + secrets.token_hex(12)
        fd = _open_at(parent_fd, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(blob)
            if os.name == "nt":
                os.replace(parent_fd / temp, parent_fd / leaf)
            else:
                os.rename(temp, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                if os.name == "nt":
                    os.unlink(parent_fd / temp)
                else:
                    os.unlink(temp, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def tree_manifest(root: Path, *, skip_dirs: frozenset[str] = frozenset(),
                  limits: ArtifactLimits = DEFAULT_LIMITS, exact_secrets: tuple[str, ...] = (),
                  omit_credential_paths: bool = False) -> list[dict]:
    with directory_fd(root):
        pass
    items, names, total, components, entries = [], set(), 0, {}, 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not (
            omit_credential_paths and is_credential_path((Path(base) / d).relative_to(root).as_posix())))
        entries += len(dirs) + len(files)
        if entries > limits.max_files * 2:
            raise ArtifactRejected("artifact_entry_count_exceeded")
        for name in dirs:
            path = Path(base) / name
            name = safe_name(path.relative_to(root).as_posix(), limits)
            _record_components(name, components)
            if path.is_symlink():
                raise ArtifactRejected("artifact_link_denied")
        for leaf in sorted(files):
            relative = (Path(base) / leaf).relative_to(root).as_posix()
            if omit_credential_paths and is_credential_path(relative):
                continue
            name = safe_name(relative, limits)
            _record_components(name, components)
            folded = name.casefold()
            if folded in names:
                raise ArtifactRejected("artifact_path_collision")
            names.add(folded)
            if len(names) > limits.max_files:
                raise ArtifactRejected("artifact_file_count_exceeded")
            blob = read_file(root, name, limits=limits, exact_secrets=exact_secrets)
            total += len(blob)
            if total > limits.max_total_bytes:
                raise ArtifactRejected("artifact_total_too_large")
            items.append({"path": name, "size_bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
    return items


def extract_zip(blob: bytes, destination: Path, *, limits: ArtifactLimits = DEFAULT_LIMITS,
                exact_secrets: tuple[str, ...] = ()) -> list[dict]:
    """Validate all bytes before creating a new destination. No partial success."""
    if len(blob) > limits.max_archive_bytes:
        raise ArtifactRejected("artifact_archive_too_large")
    pending, names, file_names, total, components = [], set(), set(), 0, {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_files * 2:
                raise ArtifactRejected("artifact_entry_count_exceeded")
            for info in entries:
                name = safe_name(info.filename.rstrip("/") if info.is_dir() else info.filename, limits)
                _record_components(name, components)
                folded = name.casefold()
                if folded in names:
                    raise ArtifactRejected("artifact_path_collision")
                names.add(folded)
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG}:
                    raise ArtifactRejected("artifact_file_type_unsafe")
                if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ArtifactRejected("artifact_encoding_unsupported")
                if info.is_dir():
                    continue
                file_names.add(folded)
                if len(file_names) > limits.max_files:
                    raise ArtifactRejected("artifact_file_count_exceeded")
                if info.file_size > limits.max_file_bytes or info.file_size > max(info.compress_size, 1) * limits.max_ratio:
                    raise ArtifactRejected("artifact_expansion_limit")
                total += info.file_size
                if total > limits.max_total_bytes:
                    raise ArtifactRejected("artifact_total_too_large")
                with archive.open(info) as stream:
                    content = stream.read(limits.max_file_bytes + 1)
                if len(content) != info.file_size:
                    raise ArtifactRejected("artifact_size_mismatch")
                inspect_blob(content, limits=limits, exact_secrets=exact_secrets)
                pending.append((name, content))
            if any("/".join(name.split("/")[:i]) in file_names for name in names for i in range(1, len(name.split("/")))):
                raise ArtifactRejected("artifact_path_collision")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ArtifactRejected("artifact_archive_invalid") from exc
    with directory_fd(destination.parent) as parent_fd:
        try:
            if os.name == "nt":
                os.mkdir(parent_fd / destination.name, 0o700)
            else:
                os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
        except OSError as exc:
            raise ArtifactRejected("artifact_destination_exists") from exc
    try:
        for name, content in pending:
            write_file(destination, name, content, limits=limits, exact_secrets=exact_secrets)
        return tree_manifest(destination, limits=limits, exact_secrets=exact_secrets)
    except BaseException:
        import shutil
        shutil.rmtree(destination)
        raise
