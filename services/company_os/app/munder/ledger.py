"""Atomic hive task ledger — patch one card, never replace a stale snapshot.

Ported from Munder Difflin ``HiveManager.addTask`` / ``patchTask`` / ``deleteTask``
(the 2026-08-15 webhook-card-loss fix). Cards carry ``dependsOn`` and ``humanQA``.

MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.state import MEM
from app.memory.working import update_working_value

_STATUSES = ("todo", "doing", "blocked", "done")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _store(venture_id: str) -> dict[str, Any]:
    key = f"munder_ledger:{venture_id}"
    data = MEM.get(key)
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        data = {"tasks": []}
    return data


def _save(venture_id: str, data: dict[str, Any]) -> None:
    MEM[f"munder_ledger:{venture_id}"] = data


def snapshot(venture_id: str) -> dict[str, Any]:
    store = _store(venture_id)
    return {"tasks": [dict(t) for t in store.get("tasks") or []]}


def _normalize_card(task: dict[str, Any]) -> dict[str, Any]:
    status = task.get("status") if task.get("status") in _STATUSES else "todo"
    depends = task.get("dependsOn") or task.get("depends_on") or []
    if not isinstance(depends, list):
        depends = []
    qa = task.get("humanQA") or []
    if not isinstance(qa, list):
        qa = []
    return {
        "id": str(task.get("id") or ""),
        "title": str(task.get("title") or task.get("id") or ""),
        "description": task.get("description"),
        "assignee": task.get("assignee"),
        "status": status,
        "dependsOn": [str(d) for d in depends if d],
        "priority": int(task.get("priority") or 3),
        "createdAt": task.get("createdAt") or _now_iso(),
        "humanQA": qa,
        "result": task.get("result"),
    }


def _update(venture_id: str, change):
    result = None
    def apply(value):
        nonlocal result
        store = value if isinstance(value, dict) and isinstance(value.get("tasks"), list) else {"tasks": []}
        result = change(store)
        return store
    update_working_value(MEM, f"munder_ledger:{venture_id}", apply)
    return result


def add_task(venture_id: str, task: dict[str, Any]) -> bool:
    """Append against the latest shared ledger; idempotent by exact card id."""
    card = _normalize_card(task)
    def change(store):
        if not card["id"] or any(row.get("id") == card["id"] for row in store["tasks"]):
            return False
        store["tasks"].append(card)
        return True
    return _update(venture_id, change)


def _change_card(venture_id: str, task_id: str, change):
    def update(store):
        for index, card in enumerate(store["tasks"]):
            if card.get("id") == task_id:
                store["tasks"][index] = change(dict(card))
                return dict(store["tasks"][index])
        return None
    return _update(venture_id, update)


def patch_task(venture_id: str, task_id: str, patch: dict[str, Any]) -> bool:
    def change(card):
        merged = {**card, **{k: v for k, v in patch.items() if k != "id"}, "id": task_id}
        if "depends_on" in patch and "dependsOn" not in patch:
            merged["dependsOn"] = list(patch.get("depends_on") or [])
        if merged.get("status") not in _STATUSES:
            merged["status"] = card.get("status") or "todo"
        return merged
    return _change_card(venture_id, task_id, change) is not None


def delete_task(venture_id: str, task_id: str) -> bool:
    def change(store):
        before = len(store["tasks"])
        store["tasks"] = [card for card in store["tasks"] if card.get("id") != task_id]
        return len(store["tasks"]) != before
    return _update(venture_id, change)


def get_task(venture_id: str, task_id: str) -> dict[str, Any] | None:
    for card in snapshot(venture_id)["tasks"]:
        if card.get("id") == task_id:
            return card
    return None


def deps_satisfied(venture_id: str, task_id: str) -> bool:
    card = get_task(venture_id, task_id)
    if not card:
        return True
    done = {c["id"] for c in snapshot(venture_id)["tasks"] if c.get("status") == "done"}
    return all(dep in done for dep in (card.get("dependsOn") or []))


def ask_human(venture_id: str, task_id: str, question: str) -> dict[str, Any] | None:
    def change(card):
        card["humanQA"] = [*(card.get("humanQA") or []), {"q": question, "askedAt": _now_iso()}]
        card["status"] = "blocked"
        return card
    return _change_card(venture_id, task_id, change)


def answer_human(venture_id: str, task_id: str, answer: str) -> dict[str, Any] | None:
    def change(card):
        qa = list(card.get("humanQA") or [])
        for entry in reversed(qa):
            if entry.get("q") and not entry.get("a") and not entry.get("dismissedAt"):
                entry["a"], entry["answeredAt"] = answer, _now_iso()
                break
        card["humanQA"] = qa
        card["status"] = "blocked" if any(e.get("q") and not e.get("a") and not e.get("dismissedAt") for e in qa) else "todo"
        return card
    return _change_card(venture_id, task_id, change)


def sync_planned_runnable(venture_id: str) -> int:
    """Park planned tasks whose hive card is blocked or waiting on deps."""
    from sqlalchemy import select

    from app.db.models import PlannedTask
    from app.state import db
    from core.clock import utcnow

    factory = db()
    n = 0
    now = utcnow()
    cards = {c["id"]: c for c in snapshot(venture_id)["tasks"]}
    with factory() as session:
        rows = session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
            )
        ).all()
        for row in rows:
            card = cards.get(row.id)
            err = row.last_error or ""
            if err.startswith("needs_you:") and not err.startswith("needs_you:decision"):
                continue
            if card is None:
                continue
            blocked = card.get("status") == "blocked" or not deps_satisfied(venture_id, row.id)
            if blocked:
                if row.runnable != 0:
                    row.runnable = 0
                    if not err.startswith("needs_you:"):
                        row.last_error = "hive:depends" if card.get("status") != "blocked" else "hive:blocked"
                    row.updated_at = now
                    n += 1
            elif err in {"hive:depends", "hive:blocked", "needs_you:decision"}:
                row.runnable = 1
                row.last_error = None
                row.updated_at = now
                n += 1
        session.commit()
    return n
