"""On-click founder brief for one planned (or live) task."""

from __future__ import annotations

import os
import re
from typing import Any

from app.config import data_root
from app.db.models import Venture
from app.planning.store import clean_task_title, find_task_by_title, get_task
from app.state import MEM, db
from app.ventures.current_idea import current_idea
from core.copy import founder_copy

_CACHE_PREFIX = "task_brief:"
_LLM_OFFLINE = {"1", "true", "yes"}


def _cache_key(venture_id: str, token: str) -> str:
    return f"{_CACHE_PREFIX}{venture_id}:{token}"


def _slug(title: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in title.lower())[:48] or "task"


def _artifact_text(venture_id: str, title: str, task_id: str | None = None) -> str:
    if task_id:
        from app.planning.saved_work import task_note

        durable = task_note(venture_id, task_id)
        if durable:
            return durable[:4000]
    path = data_root() / "ventures" / venture_id / "repo" / "tasks" / f"{_slug(title)}.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:4000]
    except OSError:
        return ""


def _related_chat(venture_id: str, title: str, *, limit: int = 4) -> list[str]:
    try:
        from app.chat.store import list_messages
    except Exception:  # noqa: BLE001
        return []
    words = {w for w in re.findall(r"[a-z0-9]{4,}", (title or "").lower())}
    if not words:
        return []
    hits: list[str] = []
    for msg in reversed(list_messages(venture_id, limit=80)):
        body = str(msg.get("body_md") or "")
        low = body.lower()
        if any(word in low for word in words):
            hits.append(re.sub(r"<!--.*?-->", "", body).strip())
        if len(hits) >= limit:
            break
    hits.reverse()
    return [h for h in hits if h][:limit]


def _venture_facts(venture_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            return {}
        return {
            "id": ven.id,
            "name": ven.name,
            "idea": current_idea(ven, session=session),
            "stage": ven.stage,
            "workspace_id": ven.workspace_id,
        }


def _phase_label(status: str | None, title: str) -> str:
    code = (status or "").lower()
    low = (title or "").lower()
    if code == "running" or low.startswith("working on "):
        return "in_progress"
    if code == "done" or low.startswith("just finished ") or low.startswith("finished "):
        return "done"
    if code in {"dismissed", "superseded"}:
        return "stopped"
    return "queued"


def collect_facts(
    venture_id: str,
    *,
    task_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Gather everything we can prove about this line of work."""
    venture = _venture_facts(venture_id)
    if not venture:
        return {}
    task = get_task(venture_id, task_id) if task_id and not str(task_id).startswith("live-") else None
    raw_title = (title or (task or {}).get("title") or "").strip()
    if task is None and raw_title:
        task = find_task_by_title(venture_id, raw_title)
    display = raw_title or (task or {}).get("title") or "This work"
    clean = clean_task_title(display) or display
    status = (task or {}).get("status") or ""
    phase = _phase_label(status, display if not status else "") if task else "not planned"
    artifact = _artifact_text(venture_id, (task or {}).get("title") or clean,
                              (task or {}).get("id"))
    return {
        "venture": venture,
        "task": task,
        "task_id": (task or {}).get("id") or task_id,
        "title": clean,
        "display_title": display,
        "status": status or phase,
        "phase": phase,
        "owner_name": (task or {}).get("owner_name"),
        "owner_role": (task or {}).get("owner_role"),
        "why_now": (task or {}).get("why_now"),
        "detail": (task or {}).get("detail"),
        "est_minutes": (task or {}).get("est_minutes"),
        "tags": (task or {}).get("tags") or [],
        "artifact": artifact,
        "chat": _related_chat(venture_id, clean),
    }


def render_grounded(facts: dict[str, Any]) -> str:
    """Founder-readable brief from stored facts only — no model required."""
    title = facts.get("title") or "This work"
    phase = facts.get("phase") or "queued"
    owner = facts.get("owner_name")
    who = owner or "your team"
    why = (facts.get("why_now") or "").strip()
    detail = (facts.get("detail") or "").strip()
    minutes = facts.get("est_minutes")
    artifact = (facts.get("artifact") or "").strip()
    chat = facts.get("chat") or []
    name = ((facts.get("venture") or {}).get("name") or "").strip()

    if phase == "not planned":
        lead = "This is a draft work description. You have not saved or started this work yet."
        doing = f"Choose how to approach **{title}** using the available context; missing facts stay unknown."
    elif phase == "in_progress":
        lead = f"**{who}** is doing this now."
        doing = (
            f"They are working through **{title}**"
            + (f" for {name}" if name else "")
            + ". This work is in progress."
        )
    elif phase == "done":
        lead = f"**{who}** already finished this."
        doing = (
            f"The work was **{title}**. "
            + ("Notes were saved after the work finished." if artifact else "It is marked completed in Work.")
        )
    elif phase == "stopped":
        lead = "This work is no longer active."
        doing = f"**{title}** was {facts.get('status') or 'stopped'}."
    else:
        lead = f"**{who}** is assigned to this work. It is saved for your review. Nothing starts automatically."
        doing = (
            f"The next action is **{title}**"
            + (f" for {name}" if name else "")
            + (f", about {int(minutes)} minutes" if isinstance(minutes, int) and minutes > 0 else "")
            + "."
        )

    lines = [f"**{title}**", "", lead, "", doing]
    if why:
        lines.extend(["", f"**Why this matters now.** {why}"])
    if detail and detail.lower() not in {why.lower(), title.lower()}:
        lines.extend(["", "You can read the full requirements and any missing information in Work."])
    elif not detail:
        lines.extend(
            [
                "",
                "**What they actually have to do.** "
                + _infer_work(title, phase),
            ]
        )
    if artifact:
        lines.extend(["", "You have a saved result in Work. Open it to read the details."])
    if chat:
        lines.extend(["", "You can read the latest updates on this item in your chat."])
    return founder_copy("\n".join(lines).strip() + "\n\n" + _next_step(facts))


def _next_step(facts: dict[str, Any]) -> str:
    phase = facts.get("phase")
    if phase == "in_progress":
        step = "Open Work to check this item's progress before you try it again."
    elif phase == "done":
        step = "Open Work to review the saved result and record whether it meets your needs."
    elif phase in {"stopped", "not planned"}:
        step = "Open Work to choose another item or describe the work you want next."
    elif not (facts.get("task") or {}).get("runnable", True):
        step = "Open Work to read this item's requirements. Then tell me in chat what you can supply, or paste your manual result, so we can check what is still missing."
    else:
        step = "Open Work, review this item's requirements, and choose Run when you want to begin."
    return "**Next step.** " + step


def _infer_work(title: str, phase: str) -> str:
    low = title.lower()
    if "recruit" in low or "design partner" in low:
        body = "Find a handful of real people who match the offer, ask them to try it, and write down who said yes and why."
    elif "instrument" in low or "funnel" in low:
        body = "Add the measurements so we can see whether people start, continue, and convert, not just guess."
    elif "landing" in low or "page" in low:
        body = "Write and ship a page that states the offer clearly enough for a stranger to understand and act."
    elif "test a paid" in low or "pricing" in low:
        body = "Put a real price in front of someone and record whether they would pay, a conversation or a checkout, not a slide."
    elif "research" in low or "competitor" in low or "compare" in low:
        body = "Look at live sources, write what is true today, and name the gap this company can own."
    elif "prototype" in low or "build" in low or "workflow" in low:
        body = "Make a working slice of the product that a design partner can click through, then note what broke."
    elif "waitlist" in low:
        body = "Stand up a way for interested people to sign, then watch whether anyone actually does."
    else:
        body = f"Complete “{title}” with a concrete artifact (notes, page, list, or measurement) the rest of the team can use."
    if phase == "done":
        return f"That meant: {body[0].lower() + body[1:]}"
    if phase == "in_progress":
        return f"Right now that means: {body[0].lower() + body[1:]}"
    return body


def _excerpt(text: str, limit: int = 280) -> str:
    clean = re.sub(r"^#+\s+", "", text, flags=re.M)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) <= limit:
        return clean
    cut = clean[: limit - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


def _llm_offline() -> bool:
    return (os.environ.get("LLM_OFFLINE") or "").strip().lower() in _LLM_OFFLINE


async def _llm_brief(facts: dict[str, Any]) -> str | None:
    if _llm_offline():
        return None
    workspace_id = (facts.get("venture") or {}).get("workspace_id")
    payload = {
        "company": (facts.get("venture") or {}).get("name"),
        "idea": (facts.get("venture") or {}).get("idea"),
        "title": facts.get("title"),
        "phase": facts.get("phase"),
        "status": facts.get("status"),
        "runnable": (facts.get("task") or {}).get("runnable", False),
        "owner": facts.get("owner_name"),
        "why_now": facts.get("why_now"),
        "detail": facts.get("detail"),
        "est_minutes": facts.get("est_minutes"),
        "artifact_excerpt": _excerpt(facts.get("artifact") or "", 600),
        "recent_notes": facts.get("chat") or [],
    }
    system = (
        "You explain one company task to the founder. "
        "Use only the facts given. Do not invent customers, metrics, or completed work. "
        "Write 3-5 short paragraphs in markdown. "
        "Cover: what this work is, why it matters now, what is happening right now "
        "(or what was already done), and what 'done' looks like. "
        "Speak plainly. No headings longer than a few words. No bullet dump of the JSON."
        " Speak to the reader as you. No internal jargon, code, layout dimensions, or em dashes."
        " Explain missing information and manual work without claiming it can run automatically."
        " Do not add a next-step section; the saved task's next step is appended separately."
    )
    try:
        import asyncio

        from app.credits import metered

        async def _call() -> str:
            resp = await metered.llm(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": str(payload)},
                ],
                tier="flash",
                purpose="concierge.render_step",
                max_tokens=500,
                workspace_id=workspace_id,
                venture_id=(facts.get("venture") or {}).get("id"),
            )
            choice = resp.choices[0].message.content if getattr(resp, "choices", None) else ""
            return (choice or "").strip()

        text = await asyncio.wait_for(_call(), timeout=25.0)
    except Exception:  # noqa: BLE001 — grounded brief always wins over a dead model
        return None
    if not text or len(text) < 40:
        return None
    return text


async def summarize_task(
    venture_id: str,
    *,
    task_id: str | None = None,
    title: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    facts = collect_facts(venture_id, task_id=task_id, title=title)
    if not facts:
        return {}
    token = str(facts.get("task_id") or _slug(str(facts.get("title") or "task")))
    key = _cache_key(venture_id, token)
    if not refresh and facts.get("task"):
        cached = MEM.get(key)
        if (isinstance(cached, dict) and "**Next step.**" in str(cached.get("body_md") or "")
                and cached.get("status") == facts.get("status")
                and str(cached.get("body_md") or "").endswith(_next_step(facts))):
            return {**cached, "body_md": founder_copy(cached["body_md"]), "cached": True}

    source = "grounded"
    body = render_grounded(facts)
    written = await _llm_brief(facts) if facts.get("task") else None
    if written:
        body = founder_copy(written + "\n\n" + _next_step(facts))
        source = "llm"

    result = {
        "task_id": facts.get("task_id"),
        "title": facts.get("title"),
        "status": facts.get("status"),
        "phase": facts.get("phase"),
        "owner_name": facts.get("owner_name"),
        "owner_role": facts.get("owner_role"),
        "body_md": body,
        "source": source,
        "cached": False,
    }
    MEM[key] = {k: v for k, v in result.items() if k != "cached"}
    return result
