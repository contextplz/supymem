"""The reuse path of `load_company_os_app` must hand back pristine state, like a fresh import."""

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["Warehouse order tracking", "Home cooking plans"])
def test_local_client_keeps_origin_and_rejects_remote_peers(monkeypatch, domain):
    from fastapi import FastAPI
    from core.local_security import LocalNetworkMiddleware
    from tests.company_os_harness import company_os_test_client

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    load_company_os_app()
    application = FastAPI()

    @application.post("/fixture")
    def fixture_response():
        return {"domain": domain}

    with company_os_test_client(LocalNetworkMiddleware(application)) as client:
        response = client.post("/fixture", data={"domain": domain})
        assert response.status_code == 200
        assert response.json() == {"domain": domain}
        assert client.headers["Origin"] == str(client.base_url).rstrip("/")
    with company_os_test_client(LocalNetworkMiddleware(application), client=("203.0.113.9", 50000)) as remote:
        assert remote.post("/fixture", data={"domain": domain}).status_code == 403


@pytest.fixture
def reuse(monkeypatch, tmp_path):
    monkeypatch.setenv("COS_TEST_APP_REUSE", "1")
    monkeypatch.delenv("COMPANY_OS_WORKING_MEMORY_PATH", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "one"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/one.db")
    load_company_os_app()  # make sure a pristine snapshot exists
    return monkeypatch


def test_module_containers_instances_and_added_names_are_restored(reuse):
    import app.api.greeting_api as greeting
    import app.org.registry as org_registry

    cache = greeting._cache
    before = dict(cache)
    cache["leaked"] = 1
    registry = org_registry.registry
    attrs = dict(vars(registry))
    vars(registry)["leaked"] = True
    greeting.LEAKED = object()

    load_company_os_app()

    assert greeting._cache is cache and dict(cache) == before
    assert vars(registry) == attrs
    assert not hasattr(greeting, "LEAKED")


def test_module_imported_lazily_during_a_test_is_dropped(reuse):
    sys.modules["app.zz_lazily_imported"] = types.ModuleType("app.zz_lazily_imported")
    load_company_os_app()
    assert "app.zz_lazily_imported" not in sys.modules


@pytest.mark.parametrize("domain", ["Warehouse operations", "Family recipes"], ids=["b2b", "consumer"])
def test_greeting_skip_survives_an_overlapping_failed_save(reuse, monkeypatch, domain):
    from threading import local

    from app.api import greeting_api
    from app.db.models import Workspace
    from app.workspaces import resolve
    from sqlalchemy.orm import Session

    person = resolve.LOCAL_PERSON_ID
    greeting_api._update(person, first_name=domain)
    failed_read, skipped_saved, outcome = Event(), Event(), local()
    original_get = Session.get

    def overlapping_get(session, model, identifier, **kwargs):
        value = original_get(session, model, identifier, **kwargs)
        if model is Workspace and kwargs.get("with_for_update") and getattr(outcome, "value", None) == "failed":
            failed_read.set()
            skipped_saved.wait(2)
        return value

    def save(value):
        outcome.value = value
        greeting_api._update(person, greeting_outcome=value,
            **({"greeted_at": "2026-11-04T00:00:01Z"} if value == "skipped" else {}))
        if value == "skipped":
            skipped_saved.set()

    monkeypatch.setattr(Session, "get", overlapping_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(save, "failed")
        assert failed_read.wait(5)
        skipped = pool.submit(save, "skipped")
        failed.result(timeout=5)
        skipped.result(timeout=5)
    saved = greeting_api._state(person)
    assert saved["greeted_at"] == "2026-11-04T00:00:01Z" and saved["greeting_outcome"] == "skipped"
    assert saved["first_name"] == domain


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_env_dependent_state_follows_the_current_environment(reuse, tmp_path, domain):
    import app.state as state

    state.MEM["leaked"] = 1
    state.MEM[domain] = 1
    reuse.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "two"))
    reuse.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/two.db")

    load_company_os_app()

    import app.config as config
    import app.state as state

    assert "leaked" not in state.MEM
    assert domain not in state.MEM
    assert state.MEM.path.parent == tmp_path / "two"
    assert config.settings.db_url.endswith("two.db")


@pytest.mark.parametrize("startup_path", ["bootstrap", "reload"])
def test_bootstrap_cannot_initialize_db_during_fixture_schema_setup(reuse, monkeypatch, caplog, startup_path):
    import app.bootstrap as bootstrap
    import app.state as state
    from app.db.models import Base

    state.db()
    table = Base.metadata.tables["ventures"]
    table.drop(state._engine)
    state.reset_db_factory()
    entered = Event()
    release = Event()
    bootstrap_started = Event()
    engines = []

    def before_create(_table, connection, **kwargs):
        engines.append(connection.engine)
        if len(engines) == 1:
            entered.set()
            assert release.wait(10), "fixture schema setup was not released"

    def after_create(_table, connection, **kwargs):
        if len(engines) > 1:
            release.set()

    def boot():
        bootstrap_started.set()
        if startup_path == "reload":
            load_company_os_app()
            state.db()
            return []
        return bootstrap.bootstrap()

    monkeypatch.setattr(bootstrap, "_mark_owned_auth_from_env", lambda: None)
    event.listen(table, "before_create", before_create)
    event.listen(table, "after_create", after_create)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            setup = pool.submit(state.db)
            try:
                assert entered.wait(10), "fixture did not start schema setup"
                startup = pool.submit(boot)
                assert bootstrap_started.wait(10), "bootstrap did not start"
                release.wait(1)
            finally:
                release.set()
            factory = setup.result(timeout=10)
            assert startup.result(timeout=10) == []
    finally:
        event.remove(table, "before_create", before_create)
        event.remove(table, "after_create", after_create)
    assert len(engines) == 1
    assert (state.db() is factory) == (startup_path == "bootstrap")
    assert "crash_sweep failed" not in caplog.text


@pytest.mark.parametrize("domain", ["Freight audit workflows", "Home garden journals"])
def test_sqlite_templates_follow_mode_without_overwriting_existing_files(monkeypatch, tmp_path, domain):
    from tests import company_os_harness as harness

    load_company_os_app()
    from app.core.local_mode import is_local

    monkeypatch.setitem(harness._TEMPLATE, "paths", {})
    built = []

    def build(state_mod, mode):
        assert mode == is_local()
        built.append(mode)
        template = tmp_path / f"template_{len(built)}.db"
        template.write_text(domain)
        harness._TEMPLATE["paths"][mode] = template

    monkeypatch.setattr(harness, "_build_template", build)
    for index, mode in enumerate(("hosted", "local", "local", "hosted")):
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        target = tmp_path / f"copy_{index}.db"
        harness._seed_from_template(None, f"sqlite:///{target}")
        assert target.read_text() == domain
    assert built == [False, True]
    target.write_text(domain + " saved data")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    harness._seed_from_template(None, f"sqlite:///{target}")
    assert target.read_text() == domain + " saved data"
    assert built == [False, True]


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"], ids=["b2b", "consumer"])
def test_sqlite_template_is_not_shared_between_hosted_and_local(reuse, tmp_path, domain):
    from sqlalchemy import inspect, text

    for index, mode in enumerate(("hosted", "local", "hosted")):
        reuse.setenv("SUPYMEM_MODE", mode)
        reuse.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/{domain}-{index}.db")
        load_company_os_app()
        from app import state
        state.db()
        tables = inspect(state._engine).get_table_names()
        if mode == "local":
            assert "alembic_version" in tables
            with state._engine.connect() as connection:
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        else:
            assert "alembic_version" not in tables
