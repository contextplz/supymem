"""Exact-card concurrency on migrated PostgreSQL, no live provider calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tests.integration.test_postgres_planning import pg_database as pg_database, journey_db as journey_db, planning as planning, run


def test_parallel_confirm_dispatches_once_and_retains_receipt(planning, monkeypatch):
    from app.chat import proposals
    from app.planning import compiler, execution
    d = planning
    run(d)
    plan_id = execution.identifier(d["job_id"])
    card = proposals.prepare_plan_approval(d["vid"], d["actor"], plan_id)
    entered, release = Event(), Event()
    original = compiler.approve_plan
    calls = []
    def held(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(compiler, "approve_plan", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(proposals.confirm, d["vid"], d["actor"], card["id"])
        try:
            assert entered.wait(10)
            with pytest.raises(ValueError, match="not_confirmable"):
                proposals.confirm(d["vid"], d["actor"], card["id"])
        finally:
            release.set()
        receipt = first.result(timeout=10)
    assert len(calls) == 1
    assert receipt["state"] == "succeeded"
    assert proposals.confirm(d["vid"], d["actor"], card["id"]) == receipt
    assert len(calls) == 1
