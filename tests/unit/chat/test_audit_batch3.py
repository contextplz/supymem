"""Audit batch 3: free app address (C11), 2-decimal chat credits (C4b), publish not 'waiting' before a build (Plan strip)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from tests.company_os_harness import load_company_os_app


def test_credits_text_two_decimals():
    load_company_os_app()
    from app.credits.units import credits_text

    assert credits_text(308.1536) == "308.15"
    assert credits_text(5) == "5.00"
    assert credits_text("0.005") == "0.01"


def test_free_slug_skips_taken_addresses(monkeypatch):
    load_company_os_app()
    from app.infra import apps

    taken = {"audit-qa", "audit-qa-2"}
    monkeypatch.setattr(apps, "slug_available", lambda s: {"available": s not in taken})
    assert apps.free_slug("audit-qa") == "audit-qa-3"
    assert apps.free_slug("fresh") == "fresh"
    assert len(apps.free_slug("x" * 63)) <= 63


def _project(title, accepted, runnable=0):
    from app.ventures import run_status

    now = datetime(2026, 1, 1)
    task = SimpleNamespace(status="open", title=title, runnable=runnable, finished_at=None, updated_at=now, created_at=now)
    result = {"state": "idle", "changed_at": None}
    run_status._project_recent_task(result, "v1", task, accepted_build=accepted)
    return result


def test_publish_task_not_waiting_before_accepted_build():
    title = "Publish the landing page via managed publication"
    assert _project(title, False)["state"] == "idle"
    assert _project(title, True)["state"] == "needs_you"
    assert _project("Record a demo", False)["state"] == "needs_you"
