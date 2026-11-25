"""Stale-custody resolvers run in every worker maintenance pass, and one failure never stops the rest."""
from contextlib import nullcontext

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_maintenance_runs_clarification_and_chat_turn_resolvers(monkeypatch):
    from app import state
    from app.approvals import service as approvals
    from app.build import device_orders
    from app.chat import requests
    from app.core import jobs
    from app.credits import reserve
    from app.hosting import service as hosting
    from app.idea import runner
    from app.research import execution
    from app.tasks import usage
    from app.worker import main

    ticks = []

    def boom():
        ticks.append("clarification")
        raise RuntimeError("one resolver failed")

    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(type("Session", (), {"commit": lambda _: None})()))
    monkeypatch.setattr(jobs, "reclaim_stale", lambda _: 0)
    monkeypatch.setattr(jobs, "expire_overdue_paid_journey", lambda _: 0)
    for module, name in ((reserve, "sweep_expired"), (device_orders, "sweep_device_leases"),
                         (execution, "reconcile_interrupted"), (usage, "reconcile_usage"),
                         (approvals, "expire_due"), (approvals, "sweep_notifications")):
        monkeypatch.setattr(module, name, lambda: None)
    monkeypatch.setattr(runner, "reconcile_interrupted_clarifications", boom)
    monkeypatch.setattr(requests, "reconcile_stale_turns", lambda: ticks.append("chat_turns"))
    monkeypatch.setattr(hosting, "reconcile_uncertain_publications", lambda: ticks.append("publications"))
    main._run_maintenance()
    assert ticks == ["clarification", "chat_turns", "publications"]
