"""s008 is additive, preserves legacy rows, and permits legacy-only rollback."""
import json

from sqlalchemy import inspect, text

from tests.integration.test_postgres_auth_security import ROOT, migration_head, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_build_pools import build_pool as build_pool


def test_s008_legacy_only_roundtrip_preserves_untrusted_historical_rows(build_pool):
    from alembic import command
    from alembic.config import Config
    from app.builder.qa_receipts import serialize
    from app.db.supymem_models import QaRun
    d = build_pool
    cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
    command.downgrade(cfg, "s007contentgen")
    assert "runtime_version" not in {col["name"] for col in inspect(d["engine"]).get_columns("qa_runs")}
    values = dict(id="qa_historical", app_id=d["app_id"], build_run_id=d["run_id"], workspace_id=d["wid"], venture_id=d["vid"])
    with d["engine"].begin() as conn:
        conn.execute(text("INSERT INTO qa_runs(id,app_id,build_run_id,workspace_id,venture_id,state,suites,passed,failed,created_at) "
                          "VALUES (:id,:app_id,:build_run_id,:workspace_id,:venture_id,'passed','[]',9999,-1,CURRENT_TIMESTAMP)"), values)
    for _ in range(2):
        command.upgrade(cfg, "head")
        with d["factory"]() as session:
            row = session.get(QaRun, values["id"])
            assert row.runtime_version == 0 and row.managed_build_id is None and row.passed == 9999 and row.failed == -1
            assert json.loads(row.receipt_json) == {}
            view = serialize(row)
            assert view["state"] == "not_run" and view["passed"] is None and not view["verified_report"]
        if _ == 0:
            command.downgrade(cfg, "s007contentgen")
    with d["engine"].connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration_head()
