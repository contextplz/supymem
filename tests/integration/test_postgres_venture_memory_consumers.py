"""Actual migrated shared-memory reads reach nonchat planning and current briefs."""
import asyncio

import pytest
from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_planning import planning as planning  # noqa: F401


def test_memory_reaches_plan_and_new_chat_then_corrects_without_local_invalidation(planning, monkeypatch):
    from app.chat.conversations import create
    from app.chat.store import append_message
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.planning.execution import inputs
    from app.tenancy import bind
    from app.ventures.state_brief import get_venture_state_brief
    import app.memory as factory
    d = planning
    reader, writer = PostgresMemoryAdapter(), PostgresMemoryAdapter()
    monkeypatch.setattr(factory, "_ADAPTER", reader)
    def write(statement, key):
        source = append_message(d["vid"], role="user", body_md="Remember that " + statement)
        return MemoryWrite(venture_id=d["vid"], kind=MemoryKind.FOUNDER_PREFERENCE,
            subject="launch", statement=statement, evidence=["chat:" + source["id"]],
            confidence=1, confidence_source="founder", author=d["actor"], dedupe_key=key)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        first = write("I prefer an explicit source review before launch", "launch-review")
        mid = asyncio.run(writer.remember(first))
        before = get_venture_state_brief(d["vid"])
        plan = None
        with d["factory"]() as session:
            plan = inputs(session, d["wid"], d["vid"])
        saved = plan["context"]["institutional_memory"]
        assert [row["statement"] for row in saved["statements"]] == [first.statement]
        assert saved["effect_authority"] is False and saved["current_state_authority"] is False
        assert before["memory"]["preferences"][0]["statement"] == first.statement
        create(d["vid"], idempotency_key="new-preserves-venture-memory")
        assert get_venture_state_brief(d["vid"])["memory"]["preferences"][0]["id"] == mid
        correction = write("I prefer current source evidence and mobile review before launch", "launch-correction")
        newer = asyncio.run(writer.supersede(mid, by=correction, reason="founder correction"))
        # No force flag, invalidation or reader restart: the separate adapter's
        # committed correction must immediately replace the old brief context.
        after = get_venture_state_brief(d["vid"])
        assert [row["statement"] for row in after["memory"]["preferences"]] == [correction.statement]
        with d["factory"]() as session:
            changed = inputs(session, d["wid"], d["vid"])
        assert changed["dependency_digest"] != plan["dependency_digest"]
        assert d["calls"] == []
        asyncio.run(writer.forget(venture_id=d["vid"], mem_id=newer, reason="founder removal"))
        assert get_venture_state_brief(d["vid"])["memory"]["preferences"] == []
    with bind(workspace_id="wsp_foreign", venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(PermissionError, match="venture_brief_scope_mismatch"):
            get_venture_state_brief(d["vid"])


def test_reviewed_build_carries_plan_memory_and_refuses_a_later_correction(planning, monkeypatch):
    from app.chat.store import append_message
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.planning import compiler, execution as ex
    from app.builder.execution import _inputs, _app, BuildError
    from app.infra.apps import create_draft
    from app.tenancy import bind
    from tests.integration.test_postgres_planning import run
    from app.db.models import Job
    import app.memory as factory
    d = planning
    adapter = PostgresMemoryAdapter()
    monkeypatch.setattr(factory, "_ADAPTER", adapter)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        source = append_message(d["vid"], role="user", body_md="Remember that I prefer plain language and accessible contrast")
        mid = asyncio.run(adapter.remember(MemoryWrite(venture_id=d["vid"],
            kind=MemoryKind.FOUNDER_PREFERENCE, subject="design", statement="I prefer plain language and accessible contrast",
            evidence=["chat:" + source["id"]], confidence=1, confidence_source="founder",
            author=d["actor"], dedupe_key="design-preference")))
        with d["factory"]() as session:
            session.get(Job, d["job_id"]).priority = -100
            session.commit()
        assert run(d).id == d["job_id"]  # Exact owned job, normal worker and network stub.
        plan = compiler.get_plan(ex.identifier(d["job_id"]))
        compiler.approve_plan(plan["id"], person_id=d["actor"], review_token=plan["review_token"])
        app = create_draft(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"],
                           name="Venture memory build", slug="memory-" + d["vid"])
        task = plan["milestones"][0]["tasks"][0]["id"]
        with d["factory"]() as session:
            inputs = _inputs(session, _app(session, app["id"], d["actor"]), plan["id"], task, None)
        context = inputs["product_context"]["institutional_memory"]
        assert [row["id"] for row in context["statements"]] == [mid]
        assert context["effect_authority"] is False
        assert len(d["calls"]) == 1
        asyncio.run(adapter.forget(venture_id=d["vid"], mem_id=mid, reason="founder removed this preference"))
        with d["factory"]() as session:
            with pytest.raises(BuildError, match="build_plan_inputs_changed"):
                _inputs(session, _app(session, app["id"], d["actor"]), plan["id"], task, None)


def test_empty_memory_preserves_an_existing_plan_context(planning, monkeypatch):
    import json
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.planning import compiler, execution as ex
    from app.db.supymem_models import ExecutionPlan
    from tests.integration.test_postgres_planning import run
    from app.db.models import Job
    import app.memory as factory
    d = planning
    monkeypatch.setattr(factory, "_ADAPTER", PostgresMemoryAdapter())
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).priority = -100
        session.commit()
    assert run(d).id == d["job_id"]
    pid = ex.identifier(d["job_id"])
    with d["factory"]() as session:
        row = session.get(ExecutionPlan, pid)
        doc = json.loads(row.structured)
        # A saved legacy plan has no institutional-context field. An empty
        # new store must not retroactively require another planning charge.
        doc["_runtime"]["context"].pop("institutional_memory")
        row.structured = json.dumps(doc)
        session.commit()
        live, stale = compiler._current(row, doc, session)
        assert stale is False
        assert live["context"]["institutional_memory"]["statements"] == []
    plan = compiler.get_plan(pid)
    compiler.approve_plan(pid, person_id=d["actor"], review_token=plan["review_token"])
    assert len(d["calls"]) == 1
