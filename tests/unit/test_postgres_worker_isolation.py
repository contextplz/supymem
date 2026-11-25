"""Pytest configuration leaves database ownership to the isolated DB fixtures."""

import os
import sys
from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy.engine import make_url

from tests.conftest import pytest_configure


@pytest.mark.parametrize("database,host", [("invoice_workflow", "127.0.0.1"), ("fitness_tracker", "::1")])
def test_configuration_never_replaces_fixture_database_urls(database, host, monkeypatch):
    raw = make_url("postgresql+psycopg://tester:secret@localhost:5432/source").set(
        host=host, database=database, query={"application_name": database},
    ).render_as_string(hide_password=False)
    keys = ("TEST_POSTGRES_URL", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "GATEWAY_USERS_DB")
    for key in keys:
        monkeypatch.setenv(key, raw)
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: pytest.fail("configuration opened a DB"))
    for config in (SimpleNamespace(), SimpleNamespace(workerinput={"workerid": "gw0"})):
        pytest_configure(config)
        assert all(os.environ[key] == raw for key in keys)
