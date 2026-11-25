"""Desktop QA download custody. No browser startup, GUI action or global Downloads access.

The actor's normal lazy ChromeFactory is bound to a fresh private task directory.
Only source-owned committed download receipts, fresh fixture GETs and independently
read bytes can prove the task. Missing custody is unknown, never a runtime diagnosis.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path


class OwnedDownloads:
    def __init__(self, parent: Path, browser):
        self.browser = browser
        self.data_dir = Path(tempfile.mkdtemp(prefix="owned-browser-", dir=parent))
        self.path = self.data_dir / "downloads"
        self.path.mkdir(mode=0o700)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.identity = self._identity(os.fstat(self.fd))
        self.closed = False
        self.cleanup_verified = None

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_uid

    def _same_directory(self):
        if self.closed:
            return False
        try:
            current = os.stat(self.path, follow_symlinks=False)
            held = os.fstat(self.fd)
            return (stat.S_ISDIR(current.st_mode) and current.st_uid == os.getuid()
                    and current.st_mode & 0o077 == 0
                    and self._identity(current) == self._identity(held) == self.identity)
        except OSError:
            return False

    def verify(self, server, sha_attr):
        from desktop_runtime.cu_browser import BrowserBackend

        backend = self.browser.b
        if (not self._same_directory() or not isinstance(backend, BrowserBackend)
                or backend._owned_tabs is not True or backend.download_dir != self.path
                or self.browser.factory.data_dir != self.data_dir):
            return None, "owned browser download custody unavailable"
        expected = getattr(server, sha_attr, None)
        if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            return None, "owned download checksum unavailable"
        if server.hit_count("/download") < 1:
            return False, "owned download GET missing"
        receipts = backend.downloads
        if (not isinstance(receipts, list) or not receipts
                or any(not isinstance(raw, str) for raw in receipts) or len(set(receipts)) != len(receipts)):
            return False, "fresh committed download receipt missing"
        matched = False
        for raw in receipts:
            if not isinstance(raw, str):
                return None, "owned browser download custody unavailable"
            path = Path(raw)
            if (path.parent != self.path
                    or re.fullmatch(r"download-[a-f0-9]{32}\.[A-Za-z0-9]{1,10}", path.name) is None):
                return None, "owned browser download custody unavailable"
            descriptor = None
            try:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                        or before.st_mode & 0o077 != 0 or before.st_nlink != 1 or before.st_size > 64 * 1024 * 1024):
                    return None, "owned browser download custody unavailable"
                digest = hashlib.sha256()
                total = 0
                while block := os.read(descriptor, 1024 * 1024):
                    total += len(block)
                    if total > 64 * 1024 * 1024:
                        return None, "owned browser download custody unavailable"
                    digest.update(block)
                after = os.fstat(descriptor)
                fields = lambda i: (i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns)
                current = os.stat(path.name, dir_fd=self.fd, follow_symlinks=False)
                if fields(before) != fields(after) or fields(after) != fields(current) or not self._same_directory():
                    return None, "owned browser download custody unavailable"
                matched = matched or digest.hexdigest() == expected
            except OSError:
                return None, "owned browser download readback unavailable"
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        return matched, "fresh owned download sha256"

    def close(self):
        if self.closed:
            return self.cleanup_verified
        verified = self._same_directory()
        try:
            if verified:
                # This exact private directory was freshly created by this QA
                # object. Descriptor unlink cannot follow entries into personal files.
                for leaf in os.listdir(self.fd):
                    try:
                        os.unlink(leaf, dir_fd=self.fd)
                    except OSError:
                        verified = False
                verified = verified and not os.listdir(self.fd) and self._same_directory()
        finally:
            os.close(self.fd)
            self.closed = True
            self.cleanup_verified = verified
        return verified


def bind(ctx, mac):
    """Bind before actor effects, or leave the oracle explicitly unavailable."""
    from desktop_runtime.cu_ai_browser import BrowserMac, ChromeFactory

    browser = getattr(mac, "browser", None)
    if (ctx.download_custody is not None or not isinstance(browser, BrowserMac)
            or browser.b is not None or not isinstance(browser.factory, ChromeFactory)
            or browser.factory.session is not None or browser.factory._owned_profile is not None):
        return False
    custody = OwnedDownloads(ctx.dir, browser)
    ctx.download_custody = custody
    ctx.cleanups.append(custody.close)  # committed before any browser startup/effect
    browser.factory.data_dir = custody.data_dir
    browser.factory.profile_email = ""  # this owned fixture requires no personal sign-in copy
    return True
