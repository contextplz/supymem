"""Owned download custody regressions. No app startup, native input or provider calls."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
import cu_tasks
from cu_tasks import Ctx, downloads, oracles
from cu_tasks.fixture_server import FixtureServer
from desktop_runtime.cu_ai_browser import BrowserMac, ChromeFactory
from desktop_runtime.cu_browser import BrowserBackend


@pytest.fixture
def owned(tmp_path):
    server = FixtureServer("abcdef12")
    ctx = Ctx(tmp_path / "task", uid=server.uid, server=server)
    ctx.dir.mkdir()
    browser = BrowserMac(factory=ChromeFactory(tmp_path / "shared"))
    assert downloads.bind(ctx, SimpleNamespace(browser=browser)) is True
    # Source-shaped backend only; no Playwright/CDP initialization.
    backend = BrowserBackend.__new__(BrowserBackend)
    backend.download_dir = ctx.download_custody.path
    backend._owned_tabs = True
    backend.downloads = []
    browser.b = backend
    try:
        yield ctx, browser, server
    finally:
        ctx.download_custody.close()


def stored(owned, raw=None):
    ctx, browser, server = owned
    leaf = ctx.download_custody.path / ("download-" + "1" * 32 + ".csv")
    leaf.write_bytes(server.csv if raw is None else raw)
    leaf.chmod(0o600)
    browser.b.downloads.append(str(leaf))
    server.hits.append(("GET", "/download"))
    return leaf


def verdict(ctx):
    return oracles.downloaded()(ctx)[0]


def test_exact_owned_receipt_get_hash_and_private_fresh_directory_required(owned):
    ctx, browser, server = owned
    custody = ctx.download_custody
    assert custody.path.parent.parent == ctx.dir
    assert browser.factory.data_dir == custody.data_dir and browser.factory.profile_email == ""
    assert verdict(ctx) is False
    path = stored(owned)
    assert path.name != server.csv_name  # suggested filename is not destination authority
    assert verdict(ctx) is True
    server.hits.clear()
    assert verdict(ctx) is False
    server.hits.append(("GET", "/download"))
    path.write_bytes(b"wrong fixture bytes")
    assert verdict(ctx) is False


def test_matching_bytes_without_committed_receipt_do_not_pass(owned):
    ctx, browser, _ = owned
    stored(owned)
    browser.b.downloads.clear()
    assert verdict(ctx) is False


@pytest.mark.parametrize("corruption", ["foreign_path", "symlink", "hardlink", "public_mode",
                                        "not_owned_context", "wrong_backend_directory",
                                        "wrong_factory_directory", "wrong_fixture_uid", "invalid_receipt"])
def test_unbound_or_unsafe_custody_never_passes_or_reads_personal_target(owned, tmp_path, corruption):
    ctx, browser, server = owned
    path = stored(owned)
    personal = tmp_path / "personal.csv"
    personal.write_bytes(server.csv)
    if corruption == "foreign_path":
        browser.b.downloads = [str(personal)]
    elif corruption == "symlink":
        path.unlink()
        path.symlink_to(personal)
    elif corruption == "hardlink":
        path.unlink()
        os.link(personal, path)
    elif corruption == "public_mode":
        path.chmod(0o644)
    elif corruption == "not_owned_context":
        browser.b._owned_tabs = False
    elif corruption == "wrong_backend_directory":
        browser.b.download_dir = tmp_path
    elif corruption == "wrong_factory_directory":
        browser.factory.data_dir = tmp_path
    elif corruption == "wrong_fixture_uid":
        ctx.uid = "foreign"
    else:
        browser.b.downloads = [{"not": "a committed path"}]
    assert verdict(ctx) is not True
    assert ctx.download_custody.close() is True
    assert personal.read_bytes() == server.csv


def test_replaced_download_directory_is_unknown_and_cleanup_cannot_touch_replacement(owned):
    ctx, _, server = owned
    stored(owned)
    custody = ctx.download_custody
    original = custody.path.with_name("original-owned-downloads")
    custody.path.rename(original)
    custody.path.mkdir(mode=0o700)
    personal = custody.path / "personal.txt"
    personal.write_text("private")
    assert verdict(ctx) is None
    assert custody.close() is False and custody.close() is False
    assert personal.read_text() == "private"
    assert (original / ("download-" + "1" * 32 + ".csv")).read_bytes() == server.csv


def test_owned_cleanup_is_once_and_unlinks_symlink_entry_without_following_it(owned, tmp_path):
    ctx, _, _ = owned
    stored(owned)
    personal = tmp_path / "personal.txt"
    personal.write_text("private")
    (ctx.download_custody.path / "untrusted-link").symlink_to(personal)
    assert ctx.download_custody.close() is True
    assert ctx.download_custody.close() is True
    assert list(ctx.download_custody.path.iterdir()) == [] and personal.read_text() == "private"


@pytest.mark.parametrize("state", ["native_only", "anonymous_browser", "already_started", "active_factory"])
def test_missing_or_started_backend_has_unavailable_oracle_and_no_new_effects(tmp_path, state):
    server = FixtureServer("abcdef12")
    ctx = Ctx(tmp_path / "task", uid=server.uid, server=server)
    ctx.dir.mkdir()
    browser = BrowserMac(factory=ChromeFactory(tmp_path / "shared"))
    mac = SimpleNamespace(browser=browser)
    if state == "native_only":
        mac = SimpleNamespace()
    elif state == "anonymous_browser":
        mac.browser = SimpleNamespace(b=None, factory=browser.factory)
    elif state == "already_started":
        browser.b = SimpleNamespace()
    else:
        browser.factory.session = SimpleNamespace()
    assert downloads.bind(ctx, mac) is False
    assert ctx.download_custody is None and ctx.cleanups == [] and list(ctx.dir.iterdir()) == []
    assert verdict(ctx) is None


def test_catalog_backend_binding_uses_same_fixture_and_registers_cleanup_before_actor(tmp_path, monkeypatch):
    captured = []
    def bind(ctx, mac):
        captured.append((ctx.uid, mac))
        ctx.cleanups.append(lambda: captured.append("cleaned"))
    task = cu_tasks.Task("owned", "browser", 1, "task", "oracle", "low", lambda _: (None, "unknown"), bind_backend=bind)
    monkeypatch.setattr(cu_tasks, "load", lambda: {task.id: task})
    specs = {}
    cu_tasks.install(specs, lambda: tmp_path)
    specs[task.id]["setup"]()
    backend = object()
    specs[task.id]["bind_backend"](backend)
    assert len(captured) == 1 and captured[0][1] is backend
    assert specs[task.id]["cleanup"]() is True and captured[-1] == "cleaned"
