"""Materialized private source cannot inherit static/builder publication authority."""
import pytest

from tests.company_os_harness import COMPANY_OS

if not (COMPANY_OS / "app/build/native_product_source_staging.py").is_file():
    pytest.skip("Hosted native source staging is not in the public local release.", allow_module_level=True)

from tests.integration.test_postgres_native_source_staging import (
    pg_database as pg_database, journey_db as journey_db, planning as planning,
    ready_plan as ready_plan, first_product as first_product,
    settled_product as settled_product, completed_diagnostic as completed_diagnostic,
    staging as staging, materialize,
)


def test_materialized_native_custody_is_not_accepted_hosting_source(staging):
    from app.hosting.service import _accepted_source, HostingError
    from app.builder.execution import review
    from app.db.supymem_models import FounderApp, BuildRun, BuildArtifact, PlanTask
    d = staging
    value = materialize(d)
    with d['factory']() as session:
        app = session.get(FounderApp, d['app_id'])
        artifact = session.get(BuildArtifact, value['source_artifact_id'])
        run = session.get(BuildRun, d['diagnostic_run_id'])
        assert artifact.kind == 'native_source_custody'
        assert run.artifact_id is None
        assert session.get(PlanTask, run.plan_task_id).state != 'done'
        with pytest.raises(HostingError, match='hosting_build_not_accepted'):
            _accepted_source(session, app, run.id)
        with pytest.raises(HostingError, match='hosting_build_not_accepted'):
            _accepted_source(session, app, run.id, historical=True)
    with pytest.raises(ValueError):
        review(d['diagnostic_run_id'], actor=d['actor'],
               source_artifact_id=value['source_artifact_id'],
               report_digest='a' * 64, decision='accept')
