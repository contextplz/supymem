import functools
import inspect
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def private_coding_install(tmp_path, monkeypatch):
    """Simulate safe host ancestors; make fake installs private unless a test says otherwise."""
    from tests.coding_tool_fixtures import private_test_install

    private_test_install(monkeypatch, tmp_path.parent)
    previous_umask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous_umask)


def pytest_configure(config):
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local":
        return
    directory = tempfile.TemporaryDirectory(prefix="company-os-unit-")
    config.add_cleanup(directory.cleanup)
    os.environ["COMPANY_OS_DATA_DIR"] = directory.name
    os.environ["COMPANY_OS_WORKING_MEMORY_PATH"] = str(Path(directory.name) / "memory.db")


@pytest.fixture(autouse=True)
def _isolated_local_setup(tmp_path, monkeypatch):
    """Tests can enable local mode after fixture setup."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    constructor = TestClient.__init__
    signature = inspect.signature(constructor)

    @functools.wraps(constructor)
    def initialize(instance, *args, **kwargs):
        arguments = signature.bind(instance, *args, **kwargs)
        if os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local":
            arguments.arguments.setdefault("base_url", "http://127.0.0.1")
            arguments.arguments.setdefault("client", ("127.0.0.1", 50000))
        constructor(*arguments.args, **arguments.kwargs)

    monkeypatch.setattr(TestClient, "__init__", initialize)


@pytest.fixture(autouse=True)
def _single_strategy_failure_is_final(request, monkeypatch):
    """Legacy loop tests script one-strategy failures; tests of the multi-strategy rule opt in with `strict_strategies`."""
    engine = sys.modules.get("desktop_runtime.cu_ai_engine")
    if engine is None and "test_cu_" in request.node.nodeid:
        try:
            from desktop_runtime import cu_ai_engine as engine
        except Exception:
            return
    if engine is not None and "strict_strategies" not in request.fixturenames:
        monkeypatch.setattr(engine, "MIN_STRATEGIES", 0, raising=False)
