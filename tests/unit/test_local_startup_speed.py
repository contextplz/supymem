import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import json
import logging
import socket
import sys
import time

attempts = []
def deny(*args, **kwargs):
    attempts.append('network')
    raise AssertionError('Local startup must not contact another service')
socket.create_connection = deny
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.getaddrinfo = deny
started = time.perf_counter()
import app.main
elapsed = time.perf_counter() - started
cloud = sorted({name.split('.')[0] for name in sys.modules
                if name.split('.')[0] in {'boto3', 'botocore', 'stripe', 'numpy', 'pandas', 'scipy', 'sklearn', 'spacy', 'torch'}})
deferred = [name for name in ('alembic', 'coding_agents.dispatcher', 'app.ventures.next_step_catalogue') if name in sys.modules]
admin = [name for name in ('app.admin.router', 'app.admin.internal',
                          'app.admin.server_diagnostics', 'app.api.diagram_lab_api') if name in sys.modules]
statuses = []
if sys.argv[1] == 'start':
    from tests.company_os_harness import company_os_test_client
    with company_os_test_client(app.main.app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        statuses = [client.get(path).status_code for path in ('/health', '/ready', '/ui-config')]
print(json.dumps({'seconds': elapsed, 'cloud': cloud, 'deferred': deferred, 'admin': admin, 'model_sdk': 'openai' in sys.modules,
                  'attempts': attempts, 'statuses': statuses,
                  'json_logging': any(type(handler.formatter).__name__ == 'JsonFormatter'
                                      for handler in logging.getLogger().handlers)}))
"""


@pytest.fixture(params=["equipment-software.example", "cycling-club.example"])
def local_probe(tmp_path, request):
    data = tmp_path / request.param
    data.mkdir()
    environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ}
    environment.update(
        PYTHONPATH=f"{ROOT}:{ROOT / 'packages'}:{ROOT / 'services/company_os'}",
        HOME=str(data), APP_ENV="local", SUPYMEM_MODE="local",
        COMPANY_OS_DATA_DIR=str(data), COMPANY_OS_WORKING_MEMORY_PATH=str(data / "memory.sqlite3"),
        GUARDRAILS_MODE="deterministic_only", LLM_OFFLINE="1", LLM_CASSETTE_MODE="replay",
    )

    def run(mode):
        result = subprocess.run([sys.executable, "-c", PROBE, mode], cwd=data, env=environment,
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.splitlines()[-1])

    return run


def test_local_api_import_budget_and_no_hosted_sdk(local_probe):
    result = local_probe("import")
    assert result["cloud"] == []
    assert result["deferred"] == []
    assert result["seconds"] < 8


def test_local_api_logs_failures_without_an_environment_flag(local_probe):
    assert local_probe("import")["json_logging"]


def test_local_api_start_and_first_requests_make_no_network_call(local_probe):
    result = local_probe("start")
    assert result["statuses"] == [200, 200, 200]
    assert result["attempts"] == []


def test_local_api_does_not_import_hosted_admin_routers(local_probe):
    assert local_probe("import")["admin"] == []


def test_local_api_defers_model_sdk_until_a_model_call(local_probe):
    assert local_probe("import")["model_sdk"] is False


def test_installed_smoke_reports_first_and_warm_launch_through_stop(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("startup_smoke", ROOT / "scripts/desktop/smoke_local_stack.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    executable = tmp_path / "desktop"
    resources = tmp_path / "resources"
    calls = []
    timestamps = iter(range(8))

    def launch(arguments, **kwargs):
        data = Path(arguments[-1])
        calls.append((data, (data / "database.sqlite3").exists()))
        data.mkdir(exist_ok=True)
        (data / "database.sqlite3").touch()
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 180

    monkeypatch.setattr(sys, "argv", ["smoke", str(executable), str(resources)])
    monkeypatch.setattr(smoke, "installed_resources", lambda *_args: resources)
    monkeypatch.setattr(smoke.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
    monkeypatch.setattr(smoke.subprocess, "run", launch)
    monkeypatch.setattr(smoke.time, "perf_counter", lambda: next(timestamps))
    smoke.main()
    assert [warm for _data, warm in calls] == [False, True, False, True]
    assert calls[0][0] == calls[1][0] and calls[2][0] == calls[3][0]
    assert not tmp_path.exists()
    output = capsys.readouterr().out
    assert output.count("PASS first-run ") == 2
    assert output.count("PASS warm-start ") == 2
    assert output.count("launch-through-stop=1.000s") == 4
    assert "SUMMARY PASS=4 FAIL=0 SKIP=0" in output


@pytest.mark.parametrize("domain", ["equipment-software.example", "cycling-club.example"])
@pytest.mark.parametrize("current", [False, True])
def test_schema_columns_only_open_write_transactions_when_missing(tmp_path, monkeypatch, isolate_app_imports, domain, current):
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from sqlalchemy import create_engine, inspect, text
    from app.gtm.supymem.schema import _alter_columns

    engine = create_engine(f"sqlite:///{tmp_path / 'schema.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE startup_fixture (value TEXT)"))
            connection.execute(text("INSERT INTO startup_fixture (value) VALUES (:domain)"), {"domain": domain})
        begin = Mock(wraps=engine.begin)
        monkeypatch.setattr(engine, "begin", begin)
        _alter_columns(engine, "startup_fixture", {"value" if current else "extra": "TEXT"}, {"startup_fixture"})
        assert begin.call_count == (0 if current else 1)
        assert {column["name"] for column in inspect(engine).get_columns("startup_fixture")} == (
            {"value"} if current else {"value", "extra"})
        with engine.connect() as connection:
            assert connection.execute(text("SELECT value FROM startup_fixture")).scalar() == domain
    finally:
        engine.dispose()


@pytest.mark.parametrize("domain", ["equipment-software.example", "cycling-club.example"])
def test_current_schema_is_verified_once_without_opening_a_write_engine(tmp_path, monkeypatch, isolate_app_imports, domain):
    import sqlite3

    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "0.1.108")
    from app.db import local_sqlite

    database = tmp_path / "startup.sqlite3"
    url = f"sqlite:///{database}"
    local_sqlite.migrate_sqlite(url)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE startup_fixture (domain TEXT)")
        connection.execute("INSERT INTO startup_fixture VALUES (?)", (domain,))
    reader = Mock(wraps=local_sqlite._schema_reader)
    engine = Mock(wraps=local_sqlite.create_engine)
    monkeypatch.setattr(local_sqlite, "_schema_reader", reader)
    monkeypatch.setattr(local_sqlite, "create_engine", engine)

    assert local_sqlite.migrate_sqlite(url) is None
    assert reader.call_count == 1
    assert engine.call_count == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT domain FROM startup_fixture").fetchone() == (domain,)


@pytest.mark.parametrize("domain", ["equipment-software.example", "cycling-club.example"])
def test_desktop_schema_repairs_hold_the_migration_lock(tmp_path, monkeypatch, isolate_app_imports, domain):
    data = tmp_path / domain
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(data))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{data / 'database.sqlite3'}")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(data / "memory.sqlite3"))
    from app import state
    from app.core.local_process import file_lock
    from app.marketing.media import schema

    monkeypatch.setattr(state, "_factory", None)
    monkeypatch.setattr(state, "_engine", None)
    repair = schema.ensure_marketing_media_schema
    called = Mock()

    def verify_locked(engine):
        with data.with_name(f".{domain}-database.sqlite3-upgrade.lock").open("r+b") as lock:
            with pytest.raises(BlockingIOError), file_lock(lock, blocking=False):
                pass
        repair(engine)
        called()

    monkeypatch.setattr(schema, "ensure_marketing_media_schema", verify_locked)
    state.db()
    called.assert_called_once()
