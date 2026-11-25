"""Goal id fallback — ported from Paperclip ``issue-goal-fallback.ts``.

Upstream tests: ``server/src/__tests__/issue-goal-fallback.test.ts``.
Company OS maps: venture idea = company default goal, charter one-liner =
project goal, task ``why_now`` = explicit issue goal.

MIT: Paperclip AI.
"""

from __future__ import annotations

import json
from typing import Any

from app.db.models import Charter, PlannedTask, Venture
from app.state import db


def resolve_issue_goal_id(
    *,
    project_id: str | None,
    goal_id: str | None,
    project_goal_id: str | None = None,
    default_goal_id: str | None,
) -> str | None:
    """Copy of Paperclip ``resolveIssueGoalId``."""
    if goal_id:
        return goal_id
    if project_id:
        return project_goal_id
    return default_goal_id


def resolve_next_issue_goal_id(
    *,
    current_project_id: str | None,
    current_goal_id: str | None,
    current_project_goal_id: str | None = None,
    project_id: str | None = None,
    goal_id: str | None = None,
    project_goal_id: str | None = None,
    default_goal_id: str | None,
) -> str | None:
    """Copy of Paperclip ``resolveNextIssueGoalId``."""
    next_project_id = current_project_id if project_id is None else project_id
    if project_goal_id is not None:
        next_project_goal_id: str | None = project_goal_id
    elif next_project_id:
        next_project_goal_id = current_project_goal_id
    else:
        next_project_goal_id = None

    def fallback(target_project_id: str | None, target_project_goal_id: str | None) -> str | None:
        if target_project_id:
            return target_project_goal_id
        return default_goal_id

    if goal_id is not None:
        return goal_id or fallback(next_project_id, next_project_goal_id)

    current_fallback = fallback(current_project_id, current_project_goal_id)
    next_fallback = fallback(next_project_id, next_project_goal_id)

    if not current_goal_id:
        return next_fallback
    if current_goal_id == current_fallback:
        return next_fallback
    return current_goal_id


def _charter_one_liner(venture_id: str, charter_id: str | None) -> str | None:
    if not charter_id:
        return None
    factory = db()
    with factory() as session:
        row = session.get(Charter, charter_id)
        if not row or row.venture_id != venture_id:
            return None
        try:
            content = json.loads(row.content_json or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(content, dict):
        return None
    for key in ("one_liner", "mission", "problem"):
        value = str(content.get(key) or "").strip()
        if value:
            return value
    return None


def ancestry_for_task(venture_id: str, task: dict[str, Any] | PlannedTask | None = None) -> list[dict[str, str]]:
    """Company → charter → this task. Agents see why, not only a title.

    Paperclip heartbeat-context returns project/goal plus each ancestor.
    """
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            return []
        idea = (ven.raw_idea or "").strip()
        charter_id = ven.active_charter_id
        name = (ven.name or "").strip()
    chain: list[dict[str, str]] = []
    company_goal = idea or name
    if company_goal:
        chain.append({"kind": "company", "label": name or "Venture", "text": company_goal})
    one = _charter_one_liner(venture_id, charter_id)
    if one and one != company_goal:
        chain.append({"kind": "charter", "label": "Charter", "text": one})
    why = ""
    title = ""
    if isinstance(task, PlannedTask):
        why = (task.why_now or "").strip()
        title = (task.title or "").strip()
    elif isinstance(task, dict):
        why = str(task.get("why_now") or "").strip()
        title = str(task.get("title") or "").strip()
    explicit = why or title
    if explicit:
        chain.append({"kind": "task", "label": title or "This task", "text": explicit})
    return chain


def ancestry_markdown(venture_id: str, task: dict[str, Any] | PlannedTask | None = None) -> str:
    rows = ancestry_for_task(venture_id, task)
    if not rows:
        return ""
    lines = ["## Why this work exists"]
    for row in rows:
        lines.append(f"- **{row['label']}:** {row['text']}")
    return "\n".join(lines)
