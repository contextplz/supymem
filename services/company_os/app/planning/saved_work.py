"""Read venture-scoped, durable task notes for founder chat and task briefs."""

from __future__ import annotations

import re

from sqlalchemy import select

from app.db.models import VentureDocument
from app.state import db

_STOP = frozenset({"a", "about", "am", "and", "at", "completed", "content", "draft",
                   "exact", "for", "from", "in", "is", "me", "my", "of", "on", "saved",
                   "show", "task", "the", "this", "to", "where", "work", "you"})


def _terms(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z0-9-]{2,}", text.lower()) if word not in _STOP}


def task_note(venture_id: str, task_id: str) -> str:
    with db()() as session:
        row = session.get(VentureDocument, f"task-note:{task_id}")
        if row is None or row.venture_id != venture_id or row.kind != "task_note":
            return ""
        return row.body_md or ""


def answer_saved_work(venture_id: str, question: str) -> str:
    """Return a saved note or an explicit absence; never schedule duplicate work."""
    with db()() as session:
        notes = session.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == venture_id,
            VentureDocument.kind.in_(("task_note", "founder_result")),
        ).order_by(VentureDocument.updated_at.desc()).limit(60)).all()
        candidates = [(row.title, row.body_md or "", row.id) for row in notes]
    if not candidates:
        return ("I cannot retrieve a durable saved task note for this venture. "
                "A chat completion that names a local `repo/tasks/` path is not itself the draft, "
                "and I will not queue another task or claim that the draft is reviewable.")
    requested_id = re.search(r"\btsk_[A-Za-z0-9]+\b", question)
    if requested_id:
        candidates = [row for row in candidates if row[2] == f"task-note:{requested_id.group()}"]
        if not candidates:
            return "I cannot find a durable note for that task in this venture. I did not queue new work."
    else:
        words = _terms(question)
        scored = [(len(words & _terms(title)), title, body) for title, body, _ in candidates]
        scored.sort(key=lambda row: row[0], reverse=True)
        if not scored or scored[0][0] == 0:
            titles = ", ".join(title for title, _, _ in candidates[:3])
            return ("I cannot match that request to a saved task note. "
                    f"Recent saved notes: {titles}. I did not queue new work.")
        best = scored[0]
        if len(scored) > 1 and scored[1][0] == best[0]:
            return ("Several saved notes match. Name the exact task before I show one: "
                    f"{best[1]}; {scored[1][1]}. I did not queue new work.")
        title, body = best[1], best[2]
    if requested_id:
        title, body, _ = candidates[0]
    if not body:
        return f"The saved note for **{title}** has no body. I did not queue new work."
    return (f"Saved task note for **{title}** (not a sent message or approved outreach):\n\n"
            + body[:16000]
            + ("\n\n[The note is longer than this chat view; ask for the exact task ID to inspect it.]"
               if len(body) > 16000 else ""))
