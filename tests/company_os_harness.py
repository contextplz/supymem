"""Shared helper: load company_os `app` without colliding with gateway/warden.

Fresh-state contract: every load behaves as if `app.*` had just been imported for the first time.

Default: purge `app.*` and re-import it (~450 modules, SQLAlchemy models, ~700 FastAPI routes), which
is most of every app-fixture's setup time. With COS_TEST_APP_REUSE=1 (set by the unit CI shards) the
first load imports normally and snapshots the pristine mutable state; later loads restore that
snapshot in place instead of re-importing (see `_PRISTINE`). tests/unit/test_company_os_harness_reuse.py
pins what is restored. COS_TEST_APP_REUSE unset/0 keeps the old purge-and-reimport behaviour.
"""

from __future__ import annotations

import collections
import contextvars
import functools
import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.ratelimit import RateLimiter

ROOT = Path(__file__).resolve().parents[1]
COMPANY_OS = ROOT / "services" / "company_os"


def local_owner_headers() -> dict[str, str]:
    from app.core.local_mode import is_local

    if not is_local():
        return {}
    from app.core.local_owner import _ensure_proxy_secret
    from core.local_security import _proxy_secret, console_origin

    _ensure_proxy_secret()
    return {"X-Local-Proxy-Secret": _proxy_secret().decode("ascii"), "Origin": console_origin()}


_PLAIN = (dict, collections.OrderedDict, collections.defaultdict, collections.Counter)
_UNSET = object()
_MAX_DEPTH = 5
# Pristine snapshot of the first import: module objects, their __dict__s, and in-place restorers.
_PRISTINE: dict = {"modules": None, "namespaces": None, "restorers": None, "context_vars": None, "local": None}


def configure_local_test_model():
    from app.core.local_mode import is_local

    if is_local():
        from app.core import providers_local
        providers_local.save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
                                      models={"chat": "gpt-6-luna"}, api_key="")


def fake_signed_in_codex(tmp_path, monkeypatch):
    from desktop_runtime import agent_status
    from tests.coding_tool_fixtures import private_test_install

    private_test_install(monkeypatch, tmp_path)
    binary = tmp_path / "codex"
    binary.touch(mode=0o700)
    responses = {("--version",): b"codex-cli 0.138.0", ("login", "status"): b"Logged in using ChatGPT"}
    monkeypatch.setattr(agent_status, "provider_binary", lambda provider, path_env=None: str(binary) if provider == "codex" else None)
    monkeypatch.setattr(agent_status, "_probe", lambda path, arguments, **kwargs: (0, responses[tuple(arguments)]))


def company_os_client(application, **options):
    """Use the desktop owner's credential without bypassing the local boundary."""
    return company_os_test_client(application, **options)


def _is_app_name(name: str) -> bool:
    return name == "app" or name.startswith("app.")


def _app_modules() -> dict:
    return {k: v for k, v in sys.modules.items() if _is_app_name(k)}


def _capture(obj, seen: set, restorers: list, depth: int = 0) -> None:
    """Record how to put `obj` (and containers/app instances it holds) back, in place."""
    if depth > _MAX_DEPTH or id(obj) in seen:
        return
    kind = type(obj)
    if kind in _PLAIN:
        seen.add(id(obj))
        saved = list(obj.items())
        restorers.append(lambda o=obj, s=saved: (o.clear(), o.update(s)))
        children = [v for _, v in saved]
    elif kind is list:
        seen.add(id(obj))
        saved = list(obj)
        restorers.append(lambda o=obj, s=saved: o.__setitem__(slice(None), s))
        children = saved
    elif kind in (set, collections.deque):
        seen.add(id(obj))
        saved = list(obj)
        restorers.append(lambda o=obj, s=saved: (o.clear(), o.update(s) if hasattr(o, "update") else o.extend(s)))
        children = saved
    elif isinstance(obj, functools._lru_cache_wrapper):
        seen.add(id(obj))
        restorers.append(obj.cache_clear)
        return
    elif isinstance(obj, RateLimiter):
        seen.add(id(obj))
        restorers.append(obj.reset)
        return
    elif _is_app_name(getattr(kind, "__module__", "") or "") and hasattr(obj, "__dict__") \
            and not isinstance(obj, (type, os.PathLike)) and not callable(obj):
        seen.add(id(obj))
        saved = dict(vars(obj))
        restorers.append(lambda o=obj, s=saved: (vars(o).clear(), vars(o).update(s)))
        children = list(saved.values())
    else:
        return
    for child in children:
        _capture(child, seen, restorers, depth + 1)


def _take_pristine() -> None:
    from app.core.local_mode import is_local

    modules = _app_modules()
    namespaces, restorers, seen = {}, [], set()
    context_vars = []
    for name, mod in modules.items():
        ns = vars(mod)
        namespaces[name] = dict(ns)
        for value in list(ns.values()):
            if isinstance(value, contextvars.ContextVar):
                context_vars.append((value, value.get(_UNSET)))
            _capture(value, seen, restorers)
        # Also the plain-container *class attributes* of app-defined classes (registries, caches).
        for value in list(ns.values()):
            if isinstance(value, type) and getattr(value, "__module__", None) == name:
                for attr, attr_value in list(vars(value).items()):
                    if not attr.startswith("__") and type(attr_value) in (*_PLAIN, list, set):
                        _capture(attr_value, seen, restorers)
    _PRISTINE.update(modules=modules, namespaces=namespaces, restorers=restorers, context_vars=context_vars, local=is_local())


def _cache_is_live() -> bool:
    modules = _PRISTINE["modules"]
    if not modules or sys.modules.get("app") is not modules.get("app"):
        return False
    from app.core.local_mode import is_local

    return _PRISTINE["local"] == is_local()


def _restore_pristine() -> None:
    import app.state as state_mod

    with state_mod._db_lock:
        _restore_pristine_locked()


def _restore_pristine_locked() -> None:
    modules = _PRISTINE["modules"]
    for name in list(_app_modules()):
        if name not in modules:
            del sys.modules[name]  # first imported lazily inside a test: import fresh next time
    for name, mod in modules.items():
        sys.modules[name] = mod
        ns, saved = vars(mod), _PRISTINE["namespaces"][name]
        for key in [k for k in ns if k not in saved]:
            del ns[key]
        for key, value in saved.items():
            if ns.get(key, _UNSET) is not value:
                ns[key] = value
    for restore in _PRISTINE["restorers"]:
        restore()
    for var, original in _PRISTINE["context_vars"]:
        if original is not _UNSET and var.get(_UNSET) is not original:
            var.set(original)
    _rebind_env_dependent_state()


def _rebind_env_dependent_state() -> None:
    """Things a fresh import computes from the *current* environment."""
    # Settings: class-body defaults read os.getenv at class creation, so evaluate config.py again in a
    # throwaway module and copy the result into the shared instance every importer already holds.
    import app.config as config_mod

    spec = importlib.util.spec_from_file_location("_cos_config_fresh", config_mod.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    vars(config_mod.settings).clear()
    vars(config_mod.settings).update(vars(fresh.settings))
    # Working memory singleton opens its SQLite file from COMPANY_OS_DATA_DIR / *_WORKING_MEMORY_PATH
    # at import; reopen it in place (every `from app.state import MEM` alias is the same object).
    from app.memory import working

    memory = working._WORKING
    if memory is not None:
        try:
            memory.close()
        except Exception:
            pass
        memory.__init__()


_TEMPLATE: dict = {"paths": {}, "building": False}


@pytest.fixture(autouse=True)
def local_owner_clients(monkeypatch, request):
    """Opt-in fixture for authenticated local journeys with mocked model calls."""
    if "local_owner_app" in request.fixturenames:
        return
    from starlette.testclient import TestClient

    original = TestClient.__enter__
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")

    def enter(client):
        original(client)
        from app.core.local_mode import is_local

        if is_local():
            from core.local_security import _proxy_secret

            monkeypatch.setenv("CONSOLE_ORIGIN", str(client.base_url).rstrip("/"))
            client.headers["Origin"] = str(client.base_url).rstrip("/")
            client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
            response = client.put("/local/providers", json={"provider": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "fixture-chat"}})
            assert response.status_code == 200, response.text
        return client

    monkeypatch.setattr(TestClient, "__enter__", enter)


def append_idea_revision(session, revision_id: str, **changes):
    """Keep fixture edits subject to the same immutable-history rule as production."""
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from core.clock import utcnow
    from core.ids import new_id

    previous = session.get(IdeaRevision, revision_id)
    values = {column.name: getattr(previous, column.name) for column in IdeaRevision.__table__.columns}
    revision = IdeaRevision(**(values | {"id": new_id("idv"), "rev": previous.rev + 1,
                                        "created_at": utcnow()} | changes))
    session.add(revision)
    session.flush()
    idea = session.get(IdeaSession, previous.session_id)
    if idea.current_revision_id == previous.id:
        idea.current_revision_id = revision.id
    if idea.confirmed_revision_id == previous.id:
        idea.confirmed_revision_id = revision.id
    idea.updated_at = revision.created_at
    return revision


def _install_sqlite_template(state_mod) -> None:
    """New SQLite files start as a copy of a schema-initialized database for their mode.

    `state.db()` runs create_all + the schema ensures + append-only triggers (~1800 statements,
    each DDL a disk commit) against every test's empty tmp file. The first result for each mode is
    kept as a template; later empty files are seeded with a byte copy first, so the same
    `db()` sequence still runs but finds everything in place. Every test still gets its own file.
    """
    real_make_engine = state_mod.make_engine

    def make_engine(url, *args, **kwargs):
        if not _TEMPLATE["building"]:
            _seed_from_template(state_mod, url)
        return real_make_engine(url, *args, **kwargs)

    state_mod.make_engine = make_engine


def _sqlite_file(url) -> Path | None:
    from sqlalchemy.engine import make_url

    parsed = make_url(str(url))
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:" \
            or parsed.database.startswith("file:"):
        return None
    return Path(parsed.database)


def _seed_from_template(state_mod, url) -> None:
    import shutil

    from app.core.local_mode import is_local

    target = _sqlite_file(url)
    if target is None or (target.exists() and target.stat().st_size > 0):
        return
    mode = is_local()
    if mode not in _TEMPLATE["paths"]:
        _build_template(state_mod, mode)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_TEMPLATE["paths"][mode], target)


def _build_template(state_mod, mode: bool) -> None:
    import atexit
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="cos-db-template-"))
    atexit.register(shutil.rmtree, tmp, True)
    path = tmp / "template.db"
    saved_env = os.environ.get("COMPANY_OS_DB_URL")
    saved_factory = (state_mod._engine, state_mod._factory)
    _TEMPLATE["building"] = True
    try:
        os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{path}"
        state_mod.reset_db_factory()
        state_mod.db()
        state_mod._engine.dispose()
    finally:
        _TEMPLATE["building"] = False
        if saved_env is None:
            os.environ.pop("COMPANY_OS_DB_URL", None)
        else:
            os.environ["COMPANY_OS_DB_URL"] = saved_env
        state_mod._engine, state_mod._factory = saved_factory
    import sqlite3

    with sqlite3.connect(path) as conn:  # fold the WAL into the main file before it is copied
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _TEMPLATE["paths"][mode] = path


def _purge_and_import():
    for k in list(sys.modules):
        if _is_app_name(k):
            del sys.modules[k]
    _clean_sys_path()
    import app.main  # noqa: F401, WPS433
    import app.state  # noqa: F401, WPS433
    import app.gtm.relationships.edges  # noqa: F401, WPS433


def _clean_sys_path() -> None:
    cleaned = [
        p
        for p in sys.path
        if "services/gateway" not in p
        and "services/warden" not in p
        and Path(p).resolve() != COMPANY_OS.resolve()
        and "services/environment" not in p
        and "/canary/" not in p and not p.endswith("/canary")
        and Path(p).resolve() != (ROOT / "config" / "scaffold").resolve()
    ]
    sys.path[:] = [str(COMPANY_OS), *cleaned]


def configure_local_gtm(monkeypatch):
    from app.core.local_mode import is_local

    if not is_local():
        return
    from app.core.local_secrets import update_settings
    from app.credits import metered
    from app.research import local_providers
    from app.signals.research import HttpResearchProvider
    from llm_gateway.sole import _sample_completion

    configure_local_test_model()
    update_settings("search", {"provider": "firecrawl", "firecrawl_url": "http://127.0.0.1:3002"}, {})

    async def read(url, *, main_only=True):
        return await HttpResearchProvider("firecrawl", "").scrape(url, main_only=main_only)

    async def complete(messages, **options):
        return _sample_completion(options["purpose"])

    monkeypatch.setattr(local_providers, "local_read", read)
    monkeypatch.setattr(metered, "_transmit", complete)


def load_company_os_app(*, reset: bool = True):
    if os.environ.get("COS_TEST_APP_REUSE") == "1":
        if _cache_is_live():
            _clean_sys_path()
            _restore_pristine()
        else:
            _purge_and_import()
            _take_pristine()
    else:
        _purge_and_import()
    import app.main as main_mod  # noqa: WPS433
    import app.state as state_mod  # noqa: WPS433
    from app.core.local_mode import is_local

    if is_local():
        from app.core import local_env_import

        local_env_import.repo_root = lambda: Path(os.environ["COMPANY_OS_DATA_DIR"])

    if os.environ.get("COS_TEST_APP_REUSE") == "1":
        _install_sqlite_template(state_mod)
    if reset:
        state_mod.reset_db_factory()
        # Prefer isolated working-memory path when tests set the env var.
        wm_path = os.environ.get("COMPANY_OS_WORKING_MEMORY_PATH")
        if wm_path and hasattr(state_mod, "reset_mem_for_tests"):
            state_mod.reset_mem_for_tests(Path(wm_path))
        else:
            state_mod.MEM.clear()
        # Drop a process-global vault left by a previous tmp_path client so BYOK
        # lookups cannot read or miss a secret from another test's data dir.
        from app.connectors.secrets import reset_store

        reset_store()

    # Back-compat for tests that poke module globals
    main_mod._MEM = state_mod.MEM
    main_mod._factory = None
    main_mod._engine = None

    def _reset_compat() -> None:
        state_mod.reset_db_factory()
        main_mod._factory = None
        main_mod._engine = None

    main_mod.reset_db = _reset_compat  # type: ignore[attr-defined]
    return main_mod


@pytest.fixture(autouse=True)
def local_chat(tmp_path, monkeypatch):
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local":
        return
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SUPYMEM_LLM_PROVIDER", raising=False)
    load_company_os_app()
    from fastapi.testclient import TestClient

    configure_local_test_model()
    request = TestClient.request

    def owner_request(client, method, url, *args, **kwargs):
        from app.core.local_mode import is_local
        from app.core.local_owner import _ensure_proxy_secret
        from core.local_security import _proxy_secret

        if is_local() and "x-local-proxy-secret" not in client.headers:
            _ensure_proxy_secret()
            headers = dict(kwargs.get("headers") or {})
            if not any(name.lower() == "x-local-proxy-secret" for name in headers):
                headers["x-local-proxy-secret"] = _proxy_secret().decode()
            kwargs["headers"] = headers
        return request(client, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", owner_request)


@pytest.fixture(autouse=True)
def local_publishing(local_chat):
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local":
        return
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe

        update_settings("publishing", {"provider": "vercel", "project": "offline-page-project",
            "waitlist_email": "founder@example.test"}, {"token": token_urlsafe(32)})
        update_settings("capabilities", {"publish_pages": True}, {})

@contextmanager
def local_owner_client(application, **options):
    with company_os_test_client(application, **options) as client:
        yield client


def company_os_test_client(application, **kwargs):
    from fastapi.testclient import TestClient
    from app.core.local_mode import is_local

    if is_local():
        from httpx import Headers
        from app.core.local_owner import _ensure_proxy_secret
        from core.local_security import _proxy_secret, console_origin

        _ensure_proxy_secret()
        kwargs.setdefault("base_url", console_origin())
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        headers = Headers(kwargs.get("headers") or {})
        headers.setdefault("Origin", console_origin())
        headers.setdefault("X-Local-Proxy-Secret", _proxy_secret().decode("ascii"))
        kwargs["headers"] = headers
    return TestClient(application, **kwargs)
