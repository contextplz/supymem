"""Freeze migration task must gate deployment on reviewed database evidence."""
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from tests.company_os_harness import ROOT, load_company_os_app

if not (ROOT / "scripts/infra/deploy_services.py").is_file():
    pytest.skip("Hosted deployment verification is not in the public local release.", allow_module_level=True)

from scripts.infra import deploy_services as deploy

pytestmark = pytest.mark.hosted_only


def test_chat_binding_audit_refuses_foreign_identity_before_source_reads(monkeypatch, capsys):
    from scripts.infra import audit_chat_conversation_bindings as audit

    commands = []

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement):
            query = str(statement)
            commands.append(query)
            value = "on" if query == "SHOW transaction_read_only" else "foreign_database"
            return SimpleNamespace(scalar_one=lambda: value)

    with pytest.raises(ValueError, match="Database identity mismatch"):
        audit.audit(Connection(), "supymem_os_staging")
    assert commands == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                        "SET LOCAL statement_timeout = '30s'", "SHOW transaction_read_only", "SELECT current_database()"]
    monkeypatch.setenv("COMPANY_OS_DB_URL", "postgresql://fixture:never_print@localhost/foreign_database")
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setattr("sys.argv", ["audit", "--expected-database", "supymem_os_staging"])
    monkeypatch.setattr(audit.sa, "create_engine", lambda *args, **kwargs: pytest.fail("foreign identity must not connect"))
    assert audit.main() == 1
    assert capsys.readouterr().out == audit.MARKER + '{"read_only": true, "error_class": "ValueError"}\n'


def test_allowed_start_heads_match_this_lineage_graph():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from pathlib import Path

    service = Path(__file__).resolve().parents[2] / "services/company_os"
    config = Config(str(service / "alembic.ini"))
    config.set_main_option("script_location", str(service / "app/db/migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert deploy.FREEZE_ALLOWED_START_HEADS == {rev.revision for rev in scripts.walk_revisions()}
    assert deploy.FREEZE_HEAD in deploy.FREEZE_ALLOWED_START_HEADS
    assert deploy.FREEZE_HEAD == scripts.get_current_head()
    assert deploy.FORBIDDEN_START_HEADS.isdisjoint(deploy.FREEZE_ALLOWED_START_HEADS)
    assert deploy.FREEZE_HEAD in deploy.FREEZE_MIGRATION_COMMAND
    assert "s011gtmdiagnosis" in deploy.FREEZE_ALLOWED_START_HEADS
    assert "gtm_god_generation_runs" in deploy.FREEZE_MIGRATION_COMMAND


@pytest.mark.parametrize("app_env,database", [("staging", "supymem_os_staging"), ("production", "supymem_os")])
@pytest.mark.parametrize("failure", [
    "database", "v0", "initial_head", "final_head", "tables", "cleanup_columns",
    "dpa_column", "site_columns", "backfill", "memory_tables", "memory_columns", "chat_columns", "chat_backfill",
    "task_tables", "task_columns", "legacy_task_columns", "shared_tables", "referral_columns", "waitlist_columns",
    "lead_library_columns", None,
])
@pytest.mark.parametrize("initial_heads", [["s010importintake"], ["u6v7w8x9y0z1"], ["s002credits"], ["s013cleanupfromu6"],
                                          ["s036touchventure"], ["s037scopedmemory"], ["s038godmodeoffline"], ["s039godmoderequests"],
                                          ["s042walletbridge"], ["s049refreshuse"], ["s050gtmlearning"], ["s051chathistory"], ["s052referralrewards"], ["s053waitlistprops"], ["s054leadlibrary"], ["s055composioproject"]])
def test_verification_executes_upgrade_only_after_identity_and_checks_result(
    monkeypatch, app_env, database, failure, initial_heads,
):
    load_company_os_app()
    monkeypatch.setenv("APP_ENV", app_env)
    commands = []
    expected = deploy.FREEZE_HEAD

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
                if failure == "v0":
                    return Result("supymem")
                return Result("wrong_database" if failure == "database" else database)
            if "version_num" in sql:
                head = [expected] if commands else initial_heads
                if (not commands and failure == "initial_head") or (commands and failure == "final_head"):
                    head = ["unexpected_revision"]
                return Result(head)
            if "FROM chat_messages" in sql:
                return Result(1 if failure == "chat_backfill" else 0)
            return Result(1 if failure == "backfill" else 0)

    tables = {"import_runs", "source_snapshots", "gtm_outreach_settings",
              "gtm_evidence_sources", "gtm_network_edges", "gtm_interviews", "gtm_pilots",
              "gtm_public_documents", "gtm_paste_bodies", "marketing_ad_proposals",
              "marketing_ad_campaigns", "gtm_god_generation_runs", "gtm_god_plans", "gtm_god_tasks", "gtm_god_messages",
              "import_object_cleanups", "venture_waitlists", "venture_waitlist_entries", "site_versions",
              "chat_conversations", "chat_context_checkpoints", "institutional_memories", "memory_working_entries",
              "beta_code_redemptions", "person_referral_codes",
              "tasks", "task_runs", "task_steps", "task_events", "maintenance_schedules", "people", "jobs", "plan_tasks",
              "gateway_identities", "gtm_shared_email_patterns", "gtm_shared_address_validity",
              "gtm_shared_email_outcomes", "gtm_public_research_cache",
              "lead_library_entries", "lead_library_tags", "lead_library_usage"}
    if failure == "tables":
        tables = set()
    elif failure == "memory_tables":
        tables.remove("institutional_memories")
    elif failure == "task_tables":
        tables.remove("task_runs")
    elif failure == "shared_tables":
        tables.remove("gtm_shared_email_outcomes")

    def columns(table):
        scoped = {
            "venture_waitlist_entries": ("props",),
            "beta_code_redemptions": ("installed_at", "qualified_at", "reward_ledger_id"),
            "person_referral_codes": ("last_nudged_at",),
            "chat_messages": ("conversation_id", "author_person_id"),
            "chat_conversations": ("id", "workspace_id", "venture_id", "creator_person_id", "visibility", "creation_key"),
            "chat_context_checkpoints": ("id", "conversation_id", "version", "watermark_id", "source_ids_json", "source_digest", "payload_json"),
            "institutional_memories": ("id", "workspace_id", "venture_id", "dedupe_key", "record_json", "version"),
            "memory_working_entries": ("scope_key", "key", "workspace_id", "venture_id", "payload_json", "version"),
            "lead_library_entries": ("id", "external_lead_id", "email_hash", "lead_type", "function", "seniority", "country_code", "status", "content_hash", "search_text"),
            "lead_library_tags": ("entry_id", "tag_type", "tag"),
            "lead_library_usage": ("id", "entry_id", "workspace_id", "venture_id", "contacted_at", "outcome"),
        }
        task_columns = {
            "tasks": ("id", "workspace_id", "venture_id", "owner_person_id", "create_key", "create_digest", "title", "goal", "state", "kind", "trigger_json", "scope_json", "done_test_json", "budget_json", "tolerance", "blocker_json", "next_fire_at", "last_run_at", "deadline_at", "deadline_is_estimate", "created_at", "updated_at"),
            "task_runs": ("id", "task_id", "state", "run_key", "control_job_id", "wallet_account_id", "evaluation_json", "allowance_json", "cost_micro", "reserved_cost_micro", "tokens_used", "reserved_tokens", "attempts", "tool_calls", "started_at", "finished_at", "deadline_at", "summary"),
            "task_steps": ("id", "run_id", "job_id", "position", "title", "state", "skill", "runs_on", "inputs_json", "done_test_json", "evidence_json", "attempt", "strategy_id", "cost_micro", "deadline_at", "lease_owner", "lease_token", "lease_version", "lease_expires_at", "started_at", "finished_at"),
            "task_events": ("id", "task_id", "run_id", "step_id", "kind", "message", "data_json", "created_at"),
            "maintenance_schedules": ("tick", "interval_seconds", "next_fire_at", "updated_at"),
            "people": ("timezone",), "jobs": ("cancel_requested",), "plan_tasks": ("due_at",),
        }
        if table in task_columns:
            values = task_columns[table]
            if failure == "task_columns" and table == "task_runs":
                values = tuple(name for name in values if name != "allowance_json")
            elif failure == "legacy_task_columns" and table == "people":
                values = ()
            return [{"name": name} for name in values]
        if table in scoped:
            if table == "beta_code_redemptions" and failure == "referral_columns":
                return []
            if table == "venture_waitlist_entries" and failure == "waitlist_columns":
                return []
            if (table == "chat_messages" and failure == "chat_columns") or (table == "institutional_memories" and failure == "memory_columns"):
                return []
            if table == "lead_library_usage" and failure == "lead_library_columns":
                return [{"name": name} for name in scoped[table] if name != "workspace_id"]
            return [{"name": name} for name in scoped[table]]
        if table == "site_versions":
            return [] if failure == "site_columns" else [{"name": name} for name in ("id", "workspace_id", "venture_id", "copy_json", "sha256")]
        if table == "gtm_outreach_settings":
            return [] if failure == "dpa_column" else [{"name": "dpa_accepted_at"}]
        return [] if failure == "cleanup_columns" else [{"name": name} for name in (
            "id", "workspace_id", "venture_id", "app_id", "import_run_id",
            "object_kind", "reason", "initiated_by", "state")]

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                             connect=Connection, dispose=lambda: None)
    monkeypatch.setattr(sa, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(sa, "inspect", lambda c: SimpleNamespace(
        get_table_names=lambda: tables,
        get_columns=columns,
    ))
    monkeypatch.setattr(deploy.subprocess, "run", lambda args, **kw: commands.append(args))
    if failure:
        with pytest.raises(RuntimeError, match="freeze migration"):
            exec(deploy.FREEZE_MIGRATION_COMMAND, {})
    else:
        exec(deploy.FREEZE_MIGRATION_COMMAND, {})
    assert len(commands) == (0 if failure in {"database", "v0", "initial_head"} else 1)


@pytest.mark.parametrize("heads", [[], ["s010importintake", "t5u6v7w8x9y0"],
                                   ["s013cleanupfromu6", "unknown"], ["future_revision"]])
def test_unreviewed_or_multiple_initial_heads_stop_before_upgrade(monkeypatch, heads):
    load_company_os_app()
    monkeypatch.setenv("APP_ENV", "staging")

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
        exec(deploy.FREEZE_MIGRATION_COMMAND, {})


def test_migrate_uses_inline_freeze_command_when_requested(monkeypatch):
    plan = {
        "env": "production",
        "cluster": "cluster",
        "verify_freeze_migration": True,
        "services": [{
            "kind": "core-api",
            "candidate": {"family": "core", "containerDefinitions": [{"name": "app", "command": ["old"]}]},
            "network": {"awsvpcConfiguration": {"subnets": ["private"]}},
            "platformVersion": "LATEST",
        }],
    }
    captured = {}

    def fake_aws(service_name, operation, **payload):
        if operation == "register-task-definition":
            captured["command"] = payload["containerDefinitions"][0]["command"]
            return {"taskDefinition": {"taskDefinitionArn": "migration:2"}}
        if operation == "run-task":
            return {"tasks": [{"taskArn": "task:migration"}]}
        if operation == "describe-tasks":
            return {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}]}
        raise AssertionError(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)
    assert deploy.migrate(plan) == "task:migration"
    assert captured["command"] == ["python", "-c", deploy.FREEZE_MIGRATION_COMMAND]
