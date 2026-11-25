"""Migration helpers own Company OS modules and restore caller context."""
import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_helper_replaces_polluted_app_and_restores_context(monkeypatch, direction):
    from tests.integration import test_postgres_concurrency as helper
    from alembic import command
    root = Path(__file__).resolve().parents[2]
    fake = types.ModuleType("app")
    fake.__path__ = [str(root / "config/scaffold/app")]
    analytics = types.ModuleType("app.analytics")
    analytics.__path__ = [str(root / "config/scaffold/app/analytics")]
    for key in list(sys.modules):
        if key == "app" or key.startswith("app."):
            monkeypatch.delitem(sys.modules, key)
    monkeypatch.setitem(sys.modules, "app", fake)
    monkeypatch.setitem(sys.modules, "app.analytics", analytics)
    monkeypatch.setenv("COMPANY_OS_DB_URL", "caller-owned-sentinel")
    before = sys.path[:]
    called = []
    def migration(cfg, revision):
        module = importlib.import_module("app.analytics.feature_catalog")
        assert Path(module.__file__).resolve() == root / "services/company_os/app/analytics/feature_catalog.py"
        called.append(revision)
    monkeypatch.setattr(command, direction, migration)
    getattr(helper, "_alembic_upgrade" if direction == "upgrade" else "_alembic_downgrade_base")("postgresql+psycopg://localhost/scoped-fake")
    import os
    assert called == ["head" if direction == "upgrade" else "base"]
    assert os.environ["COMPANY_OS_DB_URL"] == "caller-owned-sentinel"
    assert sys.path == before
    assert sys.modules["app"] is fake and sys.modules["app.analytics"] is analytics
