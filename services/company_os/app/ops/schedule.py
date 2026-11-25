"""Daily cycles and sweeps — Polsia Beat schedule, in-process (doc 49)."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.ops.services import company as company_svc
from app.ops.services import reports as reports_svc
from app.ops.services import tasks as tasks_svc


def _workspace_is_paused(workspace_id: str) -> bool:
    from app.ventures.lifecycle import is_workspace_paused

    return is_workspace_paused(workspace_id)


def _enqueue_and_run(
    workspace_id: str, agent_type: str, title: str, *, source: str = "scheduler"
) -> dict[str, Any]:
    if _workspace_is_paused(workspace_id):
        return {"skipped": True, "reason": "venture_paused"}
    task = tasks_svc.create_task(
        workspace_id, title=title, agent_type=agent_type, source=source
    )
    return tasks_svc.execute_task(workspace_id, task["id"])


def run_morning_cycle(workspace_id: str) -> dict[str, Any]:
    """Orchestrator plans the day, then a starter set of agents."""
    if _workspace_is_paused(workspace_id):
        return {"skipped": True, "reason": "venture_paused", "tasks": [], "report": None}
    cfg = company_svc.get_config(workspace_id)
    if cfg is None:
        company_svc.upsert_config(workspace_id, {"name": "Company"})
    plan = _enqueue_and_run(
        workspace_id,
        "orchestrator",
        "Write the morning plan for today",
        source="morning_cycle",
    )
    starters = (
        ("business_planning", "Review strategy and KPIs"),
        ("competitor_research", "Refresh competitor profiles"),
        ("social_media", "Draft today's social posts"),
        ("finance", "Snapshot revenue and spend"),
    )
    results = [plan]
    for agent_type, title in starters:
        results.append(
            _enqueue_and_run(workspace_id, agent_type, title, source="morning_cycle")
        )
    report = reports_svc.save_morning_plan(
        workspace_id,
        date.today(),
        plan.get("result_summary") or "Morning plan recorded.",
        tasks_planned=len(results),
    )
    return {"tasks": results, "report": report}


def run_evening_cycle(workspace_id: str) -> dict[str, Any]:
    if _workspace_is_paused(workspace_id):
        return {
            "skipped": True,
            "reason": "venture_paused",
            "task": None,
            "report": None,
            "today": None,
        }
    summary_task = _enqueue_and_run(
        workspace_id,
        "orchestrator",
        "Write the evening summary for today",
        source="evening_cycle",
    )
    today = tasks_svc.get_tasks_today_summary(workspace_id)
    report = reports_svc.save_evening_summary(
        workspace_id,
        date.today(),
        summary=summary_task.get("result_summary") or "Evening summary recorded.",
        tasks_completed=today["tasks_today_completed"],
        tasks_failed=today["tasks_today_failed"],
    )
    return {"task": summary_task, "report": report, "today": today}


def run_social_sweep(workspace_id: str) -> dict[str, Any]:
    return _enqueue_and_run(
        workspace_id,
        "social_media",
        "Check social mentions and reply to engaging comments",
    )


def run_email_sweep(workspace_id: str) -> dict[str, Any]:
    return _enqueue_and_run(
        workspace_id,
        "customer_support",
        "Check inbox and reply to customer emails",
    )


def run_ads_stripe_sync(workspace_id: str) -> list[dict[str, Any]]:
    return [
        _enqueue_and_run(
            workspace_id, "ads_management", "Sync ad metrics from Google and Meta"
        ),
        _enqueue_and_run(
            workspace_id,
            "finance",
            "Check for failed Stripe payments and update revenue snapshot",
        ),
    ]
