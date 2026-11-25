"""Actual 42→45 PG migrations with synthetic local history and no providers.

Coordinator lease only. Every case owns a loopback database; create_all never
supplies schema. Financial rows are local fixtures, not live wallet evidence.
"""
from datetime import timedelta

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.integration.test_postgres_wallet_bridge_rollout import bridge_db as bridge_db, _migrate
from tests.unit.db.test_supymem_migrations import HISTORY_DOWNGRADES, _late_migrations, _seed_late_history

TASK_TABLES = {"tasks", "task_runs", "task_steps", "task_events", "maintenance_schedules"}
TASK_MIGRATION_HEADS = {
    "s045tasks", "s046lootdrop", "s047credentialepoch", "s048gatewayidentities", "s049refreshuse",
    "s050gtmlearning", "s051chathistory", "s052referralrewards", "s053waitlistprops", "s054leadlibrary",
    "s055composioproject", "s056multigmail", "s057inboxbrief", "s058memsearch", "s059scaleidx",
    "s061codingconsent", "s062spend", "s063routines",
}


@pytest.fixture()
def prior_bridge(bridge_db):
    d = bridge_db
    _migrate("downgrade", "base")
    _migrate("upgrade", "s042walletbridge")
    assert TASK_TABLES.isdisjoint(inspect(d["engine"]).get_table_names())
    # s001 copies live plan metadata. Remove that one early ORM column to
    # reproduce the accepted pre-task schema and exercise all three ALTERs.
    with d["engine"].begin() as connection:
        if "due_at" in {column["name"] for column in inspect(connection).get_columns("plan_tasks")}:
            connection.execute(text("ALTER TABLE plan_tasks DROP COLUMN due_at"))
    for table, column in (("people", "timezone"), ("jobs", "cancel_requested"), ("plan_tasks", "due_at")):
        assert column not in {item["name"] for item in inspect(d["engine"]).get_columns(table)}
    seed_prior_rows(d)
    return d


def seed_prior_rows(d):
    from app.credits._txn import naive_utc
    from app.credits.grants import grant
    from app.credits.reserve import protect_outcome, reserve
    from app.db.models import CreditAccount
    from app.db.supymem_models import ExecutionPlan, PlanMilestone

    now = naive_utc()
    with d["engine"].begin() as connection:
        connection.execute(text("INSERT INTO people (id,display_name,created_at) VALUES ('per_task_migrate','Synthetic founder',:now)"), {"now": now})
        connection.execute(text("INSERT INTO workspaces (id,kind,name,entry_mode,owner_id,created_at,updated_at) "
            "VALUES ('ws_task_migrate','company','Synthetic migration','idea','per_task_migrate',:now,:now)"), {"now": now})
        connection.execute(text("INSERT INTO ventures (id,workspace_id,name,raw_idea,stage,autonomy_level,created_at,updated_at) "
            "VALUES ('ven_task_migrate','ws_task_migrate','Synthetic venture','Fixture','draft','L1',:now,:now)"), {"now": now})
        connection.execute(text("INSERT INTO jobs (id,kind,workspace_id,venture_id,payload_json,status,priority,attempts,max_attempts,timeout_s,created_at) "
            "VALUES ('job_task_migrate','synthetic.legacy','ws_task_migrate','ven_task_migrate','{}','scheduled',5,0,3,900,:now)"), {"now": now})
    with d["factory"]() as session:
        session.add(CreditAccount(id="cra_task_migrate", scope="person", owner_person_id="per_task_migrate",
            status="active", granted_micro=0, spent_micro=0, reserved_micro=0, version=0, created_at=now, updated_at=now))
        session.add(ExecutionPlan(id="plan_task_migrate", workspace_id="ws_task_migrate", venture_id="ven_task_migrate",
            version=1, state="draft", summary="Preserve this plan", created_at=now))
        session.flush()
        session.add(PlanMilestone(id="milestone_task_migrate", plan_id="plan_task_migrate", workspace_id="ws_task_migrate",
            venture_id="ven_task_migrate", order=0, title="Preserve milestone", kind="research", state="pending"))
        session.commit()
    with d["engine"].begin() as connection:
        connection.execute(text('INSERT INTO plan_tasks (id,milestone_id,workspace_id,venture_id,"order",title,description,tool_hints,state,estimate_credits,actual_credits,created_at) '
            "VALUES ('plan_item_task_migrate','milestone_task_migrate','ws_task_migrate','ven_task_migrate',0,'Preserve task','Original detail','[]','pending',0,0,:now)"), {"now": now})
    grant(account_id="cra_task_migrate", amount_micro=1000, reason="Synthetic pre-upgrade grant", actor_kind="system",
        actor_id="synthetic", idempotency_key="synthetic-task-migration-grant")
    hold = reserve(account_id="cra_task_migrate", amount_micro=100, purpose="synthetic.migration-custody",
        workspace_id="ws_task_migrate", venture_id="ven_task_migrate", request_id="synthetic-task-migration-hold")
    protect_outcome(hold)


def legacy_snapshot(d):
    queries = (
        "SELECT id,display_name,created_at FROM people WHERE id='per_task_migrate'",
        "SELECT id,owner_id,name FROM workspaces WHERE id='ws_task_migrate'",
        "SELECT id,workspace_id,name,stage FROM ventures WHERE id='ven_task_migrate'",
        "SELECT id,kind,payload_json,status,attempts,created_at FROM jobs WHERE id='job_task_migrate'",
        "SELECT id,title,description,state,created_at FROM plan_tasks WHERE id='plan_item_task_migrate'",
        "SELECT id,scope,owner_person_id,granted_micro,spent_micro,reserved_micro,status,version FROM credit_accounts WHERE id='cra_task_migrate'",
        "SELECT id,amount_micro,prev_hash,entry_hash,idempotency_key FROM credit_ledger WHERE account_id='cra_task_migrate' ORDER BY seq",
        "SELECT id,account_id,status,amount_micro,request_id,workspace_id,venture_id,task_invocation FROM credit_reservations WHERE account_id='cra_task_migrate'",
        "SELECT value_json FROM credit_policies WHERE key='founder_wallet_phase'",
    )
    with d["engine"].connect() as connection:
        return [connection.execute(text(query)).all() for query in queries]


def seed_task_history(d):
    from app.credits._txn import naive_utc
    from app.db.task_models import TaskEvent, TaskRun, TaskStep
    from app.tasks.contracts import Contract
    from app.tasks.locks import lock_run
    from app.tasks.service import create
    now = naive_utc()
    with d["factory"]() as session:
        task = create(session, workspace_id="ws_task_migrate", venture_id="ven_task_migrate", owner_person_id="per_task_migrate",
            contract=Contract(title="Synthetic migration task", goal="Summarize my waitlist",
                steps=[{"title": "Summarize", "skill": "tasks.waitlist_summary"}]), start=False)
        run = TaskRun(id="run_task_migrate", task_id=task.id, run_key="synthetic", state="working", started_at=now,
            deadline_at=now + timedelta(minutes=5))
        session.add(run)
        session.flush()
        lock_run(session, run.id)
        step = TaskStep(id="step_task_migrate", run_id=run.id, position=0, title="Synthetic checkpoint",
            skill="tasks.waitlist_summary", state="submitted", deadline_at=run.deadline_at)
        session.add(step)
        session.flush()
        session.add(TaskEvent(id="event_task_migrate", task_id=task.id, run_id=run.id, step_id=step.id,
            kind="checkpoint", message="Synthetic saved checkpoint", data_json='{"synthetic":true}', created_at=now))
        session.commit()


def task_snapshot(d):
    with d["engine"].connect() as connection:
        return [connection.execute(text("SELECT * FROM " + table + " ORDER BY 1")).all() for table in sorted(TASK_TABLES)]


def test_bridge_to_tasks_is_additive_and_installs_append_only_history(prior_bridge):
    d, before = prior_bridge, legacy_snapshot(prior_bridge)
    _migrate("upgrade", "head")
    assert TASK_TABLES <= set(inspect(d["engine"]).get_table_names())
    assert legacy_snapshot(d) == before
    with d["engine"].connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() in TASK_MIGRATION_HEADS
        assert connection.execute(text("SELECT timezone FROM people WHERE id='per_task_migrate'")).scalar_one() is None
        assert connection.execute(text("SELECT cancel_requested FROM jobs WHERE id='job_task_migrate'")).scalar_one() is False
        assert connection.execute(text("SELECT due_at FROM plan_tasks WHERE id='plan_item_task_migrate'")).scalar_one() is None
    seed_task_history(d)
    for statement in ("UPDATE task_events SET message='tampered' WHERE id='event_task_migrate'",
                      "DELETE FROM task_events WHERE id='event_task_migrate'"):
        with pytest.raises(DBAPIError, match="task_events_append_only"), d["engine"].begin() as connection:
            connection.execute(text(statement))
    assert legacy_snapshot(d) == before


def test_empty_task_schema_downgrades_and_reupgrades_without_touching_legacy_rows(prior_bridge):
    d, before = prior_bridge, legacy_snapshot(prior_bridge)
    _migrate("upgrade", "head")
    _migrate("downgrade", "s042walletbridge")
    assert TASK_TABLES.isdisjoint(inspect(d["engine"]).get_table_names())
    with d["engine"].connect() as connection:
        assert connection.execute(text("SELECT to_regprocedure('task_events_append_only()')")).scalar_one() is None
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "s042walletbridge"
    assert legacy_snapshot(d) == before
    _migrate("upgrade", "head")
    assert TASK_TABLES <= set(inspect(d["engine"]).get_table_names()) and legacy_snapshot(d) == before


@pytest.mark.parametrize("history", ["task_graph", "maintenance_only"])
def test_any_task_or_schedule_history_refuses_downgrade_before_schema_change(prior_bridge, history):
    from app.credits._txn import naive_utc
    from app.db.task_models import MaintenanceSchedule
    d = prior_bridge
    _migrate("upgrade", "head")
    if history == "task_graph":
        seed_task_history(d)
    else:
        with d["factory"]() as session:
            session.add(MaintenanceSchedule(tick="maintenance", interval_seconds=60,
                next_fire_at=naive_utc(), updated_at=naive_utc()))
            session.commit()
    before, legacy = task_snapshot(d), legacy_snapshot(d)
    with pytest.raises(RuntimeError, match="task_history_prevents_downgrade"):
        _migrate("downgrade", "s042walletbridge")
    assert TASK_TABLES <= set(inspect(d["engine"]).get_table_names())
    assert task_snapshot(d) == before and legacy_snapshot(d) == legacy
    with d["engine"].connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() in TASK_MIGRATION_HEADS


def _late_schema_snapshot(engine):
    with engine.connect() as connection:
        return (
            connection.execute(text("SELECT table_name,column_name,data_type,is_nullable,column_default "
                "FROM information_schema.columns WHERE table_schema=current_schema() ORDER BY table_name,column_name")).all(),
            connection.execute(text("SELECT tablename,indexname,indexdef FROM pg_indexes "
                "WHERE schemaname=current_schema() ORDER BY tablename,indexname")).all(),
        )


@pytest.mark.parametrize("revision,table,error", HISTORY_DOWNGRADES)
@pytest.mark.parametrize("domain", ["Industrial sensor maintenance", "Home birdwatching"])
def test_late_history_guards_hold_locks_through_ddl_and_preserve_failed_chain(prior_bridge, revision, table, error, domain):
    migration = next(item for item in _late_migrations() if item.revision == revision)
    engine = prior_bridge["engine"]
    before = legacy_snapshot(prior_bridge)
    _migrate("upgrade", "head")
    schema = _late_schema_snapshot(engine)
    probes = set()

    def probe_writer(connection, cursor, statement, parameters, context, many):
        statement = statement.lstrip()
        if f"FROM {table}" in statement and statement.startswith("SELECT"):
            phase = "check"
        elif statement.startswith("DROP TABLE") and table in statement:
            phase = "ddl"
        else:
            return
        if phase in probes:
            return
        probes.add(phase)
        with pytest.raises(DBAPIError, match="lock timeout"), engine.begin() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
            _seed_late_history(writer, table, domain)

    event.listen(Engine, "before_cursor_execute", probe_writer)
    try:
        _migrate("downgrade", migration.down_revision)
    finally:
        event.remove(Engine, "before_cursor_execute", probe_writer)
    assert probes == {"check", "ddl"}
    assert table not in inspect(engine).get_table_names()
    assert legacy_snapshot(prior_bridge) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == migration.down_revision
    _migrate("upgrade", "head")
    assert _late_schema_snapshot(engine) == schema
    with engine.begin() as connection:
        _seed_late_history(connection, table, domain)
        rows = connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all()
    with pytest.raises(RuntimeError, match=error):
        _migrate("downgrade", migration.down_revision)
    assert _late_schema_snapshot(engine) == schema
    assert legacy_snapshot(prior_bridge) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "s063routines"
        assert connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all() == rows


@pytest.mark.parametrize("revision,index,table,columns", [
    ("s059scaleidx", "ix_jobs_workspace_claim", "jobs", "id"),
    ("s059scaleidx", "ix_jobs_workspace_inflight", "jobs", "id"),
    ("s059scaleidx", "ix_jobs_workspace_rotation", "jobs", "id"),
    ("s059scaleidx", "ix_gtm_email_events_mailbox_type_latest", "jobs", "id"),
    ("s062spend", "ix_credit_ledger_account_created", "credit_ledger", "created_at, account_id"),
])
@pytest.mark.parametrize("domain", ["Industrial sensor maintenance", "Home birdwatching"])
def test_valid_wrong_postgres_index_definition_is_not_accepted(prior_bridge, revision, index, table, columns, domain):
    _migrate("upgrade", "head")
    migration = next(item for item in _late_migrations() if item.revision == revision)
    _migrate("downgrade", migration.down_revision)
    engine = prior_bridge["engine"]
    with engine.begin() as connection:
        connection.execute(text("UPDATE ventures SET name=:domain WHERE id='ven_task_migrate'"), {"domain": domain})
        connection.execute(text(f"CREATE INDEX {index} ON {table} ({columns})"))
        definition = connection.scalar(text("SELECT pg_get_indexdef(to_regclass(:index))"), {"index": index})
        assert connection.scalar(text("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(:index)"), {"index": index})
    before = legacy_snapshot(prior_bridge)
    with pytest.raises(RuntimeError, match="index_definition_mismatch"):
        _migrate("upgrade", revision)
    assert legacy_snapshot(prior_bridge) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_get_indexdef(to_regclass(:index))"), {"index": index}) == definition
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == migration.down_revision
