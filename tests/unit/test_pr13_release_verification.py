"""PR13's opt-in migration task must gate deployment on reviewed database evidence."""
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from tests.company_os_harness import ROOT, load_company_os_app

if not (ROOT / "scripts/infra/deploy_services.py").is_file():
    pytest.skip("Hosted deployment verification is not in the public local release.", allow_module_level=True)

from scripts.infra import deploy_services as deploy

pytestmark = pytest.mark.hosted_only


@pytest.mark.parametrize("env,frozen", [("production", True), ("staging", False)])
def test_verification_rejected_outside_frozen_staging(monkeypatch, env, frozen):
    monkeypatch.setattr(deploy, "aws", lambda *a, **kw: pytest.fail("AWS must not be called"))
    with pytest.raises(deploy.ReleaseError, match="requires frozen staging"):
        deploy.plan_release(env, "staging-" + "a" * 40,
                            frozen_staging_images=frozen, verify_pr13_migration=True)


@pytest.mark.parametrize("failure", ["database", "initial_head", "final_head", "tables", "diagnosis_table", "diagnosis_columns", "dpa_column", "backfill", None])
@pytest.mark.parametrize("initial_heads", [["s010importintake"], ["u6v7w8x9y0z1"], ["s011gtmdiagnosis"]])
def test_verification_executes_upgrade_only_after_identity_and_checks_result(monkeypatch, failure, initial_heads):
    load_company_os_app()
    commands = []
    expected = "s011gtmdiagnosis"

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalars(self):
            return self.value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            sql = str(query)
            if "current_database" in sql:
                return Result("wrong_database" if failure == "database" else "supymem_os_staging")
            if "version_num" in sql:
                head = [expected] if commands else initial_heads
                if (not commands and failure == "initial_head") or (commands and failure == "final_head"):
                    head = ["unexpected_revision"]
                return Result(head)
            return Result(1 if failure == "backfill" else 0)

    tables = {"import_runs", "source_snapshots", "gtm_outreach_settings",
              "gtm_evidence_sources", "gtm_network_edges", "gtm_interviews", "gtm_pilots",
              "gtm_public_documents", "gtm_paste_bodies", "marketing_ad_proposals",
              "marketing_ad_campaigns", "gtm_god_plans", "gtm_god_tasks", "gtm_god_messages",
              "gtm_god_generation_runs"}
    if failure == "diagnosis_table":
        tables.remove("gtm_god_generation_runs")
    def columns(table):
        if table == "gtm_outreach_settings":
            return [] if failure == "dpa_column" else [{"name": "dpa_accepted_at"}]
        return [] if failure == "diagnosis_columns" else [{"name": name} for name in (
            "id", "workspace_id", "venture_id", "plan_id", "job_id", "actor_id", "kind",
            "idempotency_hash", "request_digest", "state", "document_json", "error_code",
            "ceiling_micro", "created_at", "updated_at", "finished_at")]
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                             connect=Connection, dispose=lambda: None)
    monkeypatch.setattr(sa, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(sa, "inspect", lambda c: SimpleNamespace(
        get_table_names=lambda: [] if failure == "tables" else tables,
        get_columns=columns,
    ))
    monkeypatch.setattr(deploy.subprocess, "run", lambda args, **kw: commands.append(args))
    if failure:
        with pytest.raises(RuntimeError, match="PR13"):
            exec(deploy.PR13_MIGRATION_COMMAND, {})
    else:
        exec(deploy.PR13_MIGRATION_COMMAND, {})
    assert len(commands) == (0 if failure in {"database", "initial_head"} else 1)


@pytest.mark.parametrize("heads", [[], ["t5u6v7w8x9y0"], ["s010importintake", "t5u6v7w8x9y0"],
                                   ["s011gtmdiagnosis", "unknown"], ["future_revision"]])
def test_unreviewed_or_multiple_initial_heads_stop_before_upgrade(monkeypatch, heads):
    load_company_os_app()
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, sql):
            return SimpleNamespace(scalar_one=lambda: "supymem_os_staging", scalars=lambda: heads)
    monkeypatch.setattr(sa, "create_engine", lambda *a, **kw: SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), connect=Connection))
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: pytest.fail("Unreviewed head must not migrate"))
    with pytest.raises(RuntimeError, match="initial revision"):
        exec(deploy.PR13_MIGRATION_COMMAND, {})
