"""Windows private files, process locks and per-user Credential Manager keys."""

from __future__ import annotations

import ctypes
import hashlib
import os
import uuid
from functools import lru_cache
from pathlib import Path

DWORD = ctypes.c_uint32
POINTER = ctypes.c_void_p


@lru_cache
def _api(library, name, arguments, result=DWORD):
    function = getattr(ctypes.WinDLL(library, use_last_error=True), name)
    function.argtypes = arguments
    function.restype = result
    return function


def _check(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def _free(pointer):
    _api("kernel32", "LocalFree", (POINTER,), POINTER)(pointer)


def _security(*, directory=False):
    token = POINTER()
    _check(_api("advapi32", "OpenProcessToken", (POINTER, DWORD, POINTER))(
        _api("kernel32", "GetCurrentProcess", (), POINTER)(), 8, ctypes.byref(token)))
    try:
        size = DWORD()
        query = _api("advapi32", "GetTokenInformation", (POINTER, DWORD, POINTER, DWORD, POINTER))
        query(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        _check(query(token, 1, buffer, size, ctypes.byref(size)))
        sid = POINTER.from_buffer(buffer)
        text = ctypes.c_wchar_p()
        _check(_api("advapi32", "ConvertSidToStringSidW", (POINTER, POINTER))(sid, ctypes.byref(text)))
        try:
            descriptor = POINTER()
            sddl = f"O:{text.value}D:P(A;{'OICI' if directory else ''};FA;;;{text.value})"
            _check(_api("advapi32", "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                        (ctypes.c_wchar_p, DWORD, POINTER, POINTER))(sddl, 1, ctypes.byref(descriptor), None))
            return descriptor
        finally:
            _free(text)
    finally:
        _api("kernel32", "CloseHandle", (POINTER,))(token)


def _check_private(handle, expected, *, directory=False, protect=False):
    details = (DWORD * 13)()
    _check(_api("kernel32", "GetFileInformationByHandle", (POINTER, POINTER))(handle, details))
    if details[0] & 0x400 or bool(details[0] & 0x10) != directory or not directory and details[10] != 1:
        raise ValueError("Your local path must not be a link and must have the expected file type. Restore your private data folder, then retry.")
    descriptor, owner, acl = POINTER(), POINTER(), POINTER()
    status = _api("advapi32", "GetSecurityInfo", (POINTER, DWORD, DWORD, POINTER, POINTER, POINTER, POINTER, POINTER))(
        handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        expected_owner, expected_acl = POINTER(), POINTER()
        present, defaulted = DWORD(), DWORD()
        _check(_api("advapi32", "GetSecurityDescriptorOwner", (POINTER, POINTER, POINTER))(
            expected, ctypes.byref(expected_owner), ctypes.byref(defaulted)))
        _check(_api("advapi32", "GetSecurityDescriptorDacl", (POINTER, POINTER, POINTER, POINTER))(
            expected, ctypes.byref(present), ctypes.byref(expected_acl), ctypes.byref(defaulted)))
        if not _api("advapi32", "EqualSid", (POINTER, POINTER))(owner, expected_owner):
            raise ValueError("Your local files belong to another Windows account. Choose a folder you own, then retry.")
        if directory or protect:
            status = _api("advapi32", "SetSecurityInfo", (POINTER, DWORD, DWORD, POINTER, POINTER, POINTER, POINTER))(
                handle, 1, 0x80000004, None, None, expected_acl, None)
            if status:
                raise ctypes.WinError(status)
            if directory:
                return
            _check_private(handle, expected)
            return
        control, revision = ctypes.c_uint16(), DWORD()
        _check(_api("advapi32", "GetSecurityDescriptorControl", (POINTER, POINTER, POINTER))(
            descriptor, ctypes.byref(control), ctypes.byref(revision)))
        size = ctypes.c_uint16.from_address(expected_acl.value + 2).value
        if (not acl.value or not control.value & 0x1000
                or ctypes.c_uint16.from_address(acl.value + 2).value != size
                or ctypes.string_at(acl, size) != ctypes.string_at(expected_acl, size)):
            raise ValueError("Your local files must be private to your Windows account. Restore your private data folder, then retry.")
    finally:
        _free(descriptor)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("size", DWORD), ("descriptor", POINTER), ("inherit", DWORD)]


def protect_private(path: Path, *, directory=False):
    descriptor = _security(directory=directory)
    handle = None
    try:
        handle = _api("kernel32", "CreateFileW", (ctypes.c_wchar_p, DWORD, DWORD, POINTER, DWORD, DWORD, POINTER), POINTER)(
            str(path), 0x60080, 3, None, 3, 0x200000 | (0x2000000 if directory else 0x80), None)
        if handle == POINTER(-1).value:
            handle = None
            _check(False)
        _check_private(handle, descriptor, directory=directory, protect=True)
    finally:
        if handle is not None:
            _api("kernel32", "CloseHandle", (POINTER,))(handle)
        _free(descriptor)


def private_directory(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _security(directory=True)
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
    handle = None
    try:
        if not _api("kernel32", "CreateDirectoryW", (ctypes.c_wchar_p, POINTER))(
                str(path), ctypes.byref(attributes)) and ctypes.get_last_error() != 183:
            _check(False)
        handle = _api("kernel32", "CreateFileW", (ctypes.c_wchar_p, DWORD, DWORD, POINTER, DWORD, DWORD, POINTER), POINTER)(
            str(path), 0x60080, 7, None, 3, 0x200000 | 0x2000000, None)
        if handle == POINTER(-1).value:
            handle = None
            _check(False)
        _check_private(handle, descriptor, directory=True)
    finally:
        if handle is not None:
            _api("kernel32", "CloseHandle", (POINTER,))(handle)
        _free(descriptor)


def private_open(path, flags):
    import msvcrt

    descriptor = _security()
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
    handle = None
    try:
        access = 0x80000000 | (0x40000000 if flags & (os.O_WRONLY | os.O_RDWR) else 0)
        disposition = 1 if flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
        handle = _api("kernel32", "CreateFileW", (ctypes.c_wchar_p, DWORD, DWORD, POINTER, DWORD, DWORD, POINTER), POINTER)(
            str(path), access, 7, ctypes.byref(attributes), disposition, 0x200000 | 0x80, None)
        if handle == POINTER(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        _check_private(handle, descriptor)
        result = msvcrt.open_osfhandle(handle, os.O_BINARY | flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR | os.O_APPEND))
        handle = None
        return result
    finally:
        if handle is not None:
            _api("kernel32", "CloseHandle", (POINTER,))(handle)
        _free(descriptor)


class _Overlapped(ctypes.Structure):
    _fields_ = [("internal", POINTER), ("internal_high", POINTER),
                ("offset", DWORD), ("offset_high", DWORD), ("event", POINTER)]


def lock_file(stream, *, exclusive=True, blocking=True, unlock=False):
    import msvcrt

    handle = msvcrt.get_osfhandle(stream.fileno())
    overlapped = _Overlapped()
    if unlock:
        success = _api("kernel32", "UnlockFileEx", (POINTER, DWORD, DWORD, DWORD, POINTER))(
            handle, 0, 1, 0, ctypes.byref(overlapped))
    else:
        success = _api("kernel32", "LockFileEx", (POINTER, DWORD, DWORD, DWORD, DWORD, POINTER))(
            handle, (2 if exclusive else 0) | (0 if blocking else 1), 0, 1, 0, ctypes.byref(overlapped))
    if not success and not blocking and ctypes.get_last_error() == 33:
        raise BlockingIOError("Your data is in use. Stop your app before maintenance, then retry.")
    _check(success)


def atomic_write(path, content):
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.close(private_open(path, os.O_RDONLY))
        except FileNotFoundError:
            pass
        _check(_api("kernel32", "MoveFileExW", (ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD))(
            str(temporary), str(path), 1 | 8))
    finally:
        temporary.unlink(missing_ok=True)


class _Credential(ctypes.Structure):
    _fields_ = [("flags", DWORD), ("type", DWORD), ("target", ctypes.c_wchar_p),
                ("comment", ctypes.c_wchar_p), ("written", DWORD * 2), ("size", DWORD),
                ("blob", POINTER), ("persist", DWORD), ("attributes_count", DWORD),
                ("attributes", POINTER), ("alias", ctypes.c_wchar_p), ("username", ctypes.c_wchar_p)]


def _credential_blob(target):
    credential = ctypes.POINTER(_Credential)()
    if not _api("advapi32", "CredReadW", (ctypes.c_wchar_p, DWORD, DWORD, POINTER))(
            target, 1, 0, ctypes.byref(credential)):
        if ctypes.get_last_error() == 1168:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not 0 < credential.contents.size <= 1024 or not credential.contents.blob:
            raise ValueError("Restore your local encryption key from your backup, then retry.")
        return ctypes.string_at(credential.contents.blob, credential.contents.size)
    finally:
        _api("advapi32", "CredFree", (POINTER,), None)(credential)


def _read_credential(target):
    value = _credential_blob(target)
    if value is not None and len(value) != 32:
        raise ValueError("Restore your local encryption key from your backup, then retry.")
    return value


def _read_password_credential(target):
    value = _credential_blob(target)
    return value.decode("utf-16-le") if value is not None else None


def _write_credential(target, value):
    blob = ctypes.create_string_buffer(value)
    credential = _Credential(type=1, target=target, size=len(value), blob=ctypes.cast(blob, POINTER), persist=2)
    try:
        _check(_api("advapi32", "CredWriteW", (POINTER, DWORD))(ctypes.byref(credential), 0))
    finally:
        ctypes.memset(blob, 0, len(blob))


def credential_key(path: Path, *, create=False):
    from app.core.local_mode import is_local
    if is_local():
        import base64
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from app.core.local_secrets import _key, _locked, _read_private, _atomic_write, legacy_secret, acknowledge_legacy

        account = "vault-master-key" if path.name == "master.key" else path.name
        migrated = False
        with _locked() as root:
            wrapping = _key(root, create=create)
            if path == root / "local-key.bin":
                return wrapping
            content = _read_private(path)
            associated = str(path.relative_to(root)).replace("\\", "/").encode()
            reference = content is not None and len(content) == 64 and all(byte in b"0123456789abcdef" for byte in content)
            if reference and os.name != "nt":
                value = bytes.fromhex(content.decode())
                nonce = os.urandom(12)
                _atomic_write(path, nonce + AESGCM(wrapping).encrypt(nonce, value, associated))
                content = _read_private(path)
                reference = False
            if content is not None and not reference:
                try:
                    value = AESGCM(wrapping).decrypt(content[:12], content[12:], associated)
                except InvalidTag:
                    raise ValueError("Restore your encrypted vault backup, then reopen Keys and connections and retry.") from None
            else:
                inherited = legacy_secret(account)
                value = base64.b64decode(inherited, validate=True) if inherited else os.urandom(32) if create and not reference else None
                if value is None or len(value) != 32:
                    raise ValueError("Open Keys and connections, re-enter your connected app keys, then retry. Keep your encrypted vault backup for recovery.")
                nonce = os.urandom(12)
                if reference:
                    backup = path.with_name(path.name + ".keychain-recovery")
                    if _read_private(backup) not in {None, content}:
                        backup = path.with_name(path.name + ".keychain-recovery-" + os.urandom(8).hex())
                    _atomic_write(backup, content)
                _atomic_write(path, nonce + AESGCM(wrapping).encrypt(nonce, value, associated))
                migrated = inherited is not None
            if len(value) != 32:
                raise ValueError("Restore your encrypted vault backup, then reopen Keys and connections and retry.")
        if migrated:
            acknowledge_legacy([account])
        return value
    identity = os.path.normcase(str(path.parent.resolve() / path.name))
    reference = hashlib.sha256(identity.encode()).hexdigest()
    try:
        try:
            descriptor = private_open(path, os.O_RDONLY)
        except FileNotFoundError:
            saved_reference = None
        else:
            with os.fdopen(descriptor, "rb") as source:
                saved_reference = source.read(65).decode("ascii")
            if len(saved_reference) != 64 or any(character not in "0123456789abcdef" for character in saved_reference):
                raise ValueError("Your saved key reference is damaged. Restore your full backup in Keys and connections, under Your data and recovery, then retry.")
            reference = saved_reference
        if saved_reference is None:
            from app.cli_local import _directory_roots
            from app.config import data_root

            root = data_root().resolve()
            try:
                relative = path.resolve().relative_to(root)
            except ValueError:
                relative = None
            if relative is not None:
                for origin in _directory_roots(root, create=False)[1:]:
                    previous = os.path.normcase(str(Path(origin) / relative))
                    candidate = hashlib.sha256(previous.encode()).hexdigest()
                    if _read_credential("com.supymem.local/key/" + candidate) is not None:
                        reference = candidate
                        break
        target = "com.supymem.local/key/" + reference
        value = _read_credential(target)
        if value is None and create:
            value = os.urandom(32)
            _write_credential(target, value)
            if _read_credential(target) != value:
                raise ValueError("Your key could not be saved securely. Check your Windows account access, then retry.")
        if value is None or len(value) != 32:
            raise ValueError("Restore your local encryption key from your backup, then retry.")
        if saved_reference is None:
            atomic_write(path, reference.encode("ascii"))
        return value
    except OSError:
        raise ValueError("Your keys could not be accessed securely. Check your Windows account access, then retry.") from None
