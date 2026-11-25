"""Build chain: one confirmation card from "build my landing page" to a queued build.

The founder never picks a milestone or retypes a command. The next eligible build milestone is chosen
from the approved plan (several become option buttons), a missing draft app is created by the same
confirmed action, and the card shows the build's credit ceiling before anything is reserved.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard

_LANDING = re.compile(r"landing|web\s*site|\bpage\b", re.I)
_PRODUCT = re.compile(r"\b(?:startup|product|app|application|company|mvp)\b", re.I)
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])")


def eligible(plan_detail: dict) -> list[tuple[dict, dict]]:
    """(milestone, task) pairs for included, runnable new-code build steps, in plan order."""
    return [(m, t) for m in plan_detail.get("milestones") or [] if m.get("included")
            for t in m.get("tasks") or []
            if t.get("capability_id") == "build.run" and t.get("implementation") == "new_code"
            and t.get("state") in {"pending", "ready", "failed"}]


def choose(pairs: list[tuple[dict, dict]], hint: str = "", task_id: str | None = None) -> tuple[dict, dict] | None:
    """The one pair to quote, or None when the founder still has to pick between several."""
    if task_id:
        return next((p for p in pairs if p[1].get("id") == task_id), None)
    if len(pairs) == 1:
        return pairs[0]
    if _LANDING.search(hint or ""):
        landing = [p for p in pairs if p[0].get("kind") == "landing" or _LANDING.search(str(p[1].get("title") or ""))]
        if len(landing) == 1:
            return landing[0]
    elif _PRODUCT.search(hint or ""):
        core = [p for p in pairs if p[0].get("kind") == "core"]
        if len(core) == 1:
            return core[0]
    return None


def options(pairs: list[tuple[dict, dict]]) -> dict[str, Any]:
    """Several eligible build steps: one button each, never 'tell me which milestone'."""
    actions = [{"label": f"Build: {p[1].get('title') or p[0].get('title')}"[:70], "action": "build.prepare",
                "payload": {"plan_task_id": p[1]["id"]}, "style": "primary" if i == 0 else "secondary"}
               for i, p in enumerate(pairs[:5])]
    return {"kind": "text", "citations": [{"question": True}], "actions": actions,
            "body_md": "Your approved plan has more than one build step ready. Pick the one you want to start. Nothing has been published."}


def _card(action_state: dict, interpretation: str, consequences: list[str]) -> dict:
    return replace(ConfirmationCard.for_action(get_action("build.start"), interpretation=interpretation,
        consequences=consequences, resulting_state=action_state), confirm_label="Start build").as_action()


def reply(venture_id: str, intro: str, plan_id: str, plan_detail: dict, apps: list[dict], idea_name: str,
          actor: str | None, hint: str = "", task_id: str | None = None) -> dict[str, Any]:
    """Approved plan, research and idea are already verified by the caller; return the single next reply."""
    from app.core.local_mode import is_local

    if is_local():
        from app.chat.build_path import local_build_reply

        return local_build_reply(venture_id, plan_task_id=task_id)

    def text(body: str, actions: list | None = None) -> dict[str, Any]:
        return {"kind": "text", "body_md": intro + body + " No build or publication was started.",
                "actions": actions or [], "citations": []}

    pairs = eligible(plan_detail)
    if not pairs:
        return text("The plan is approved, but no executable new-code build milestone is saved. "
                    "Review the plan scope here before a quote.")
    if len(apps) > 1:
        return text("The plan is approved, but multiple managed apps are saved. Open Builds to choose the app.",
                    [{"label": "Open builds", "href": f"/v/{venture_id}/builds", "style": "primary"}])
    if not actor:
        return text("The plan and a build milestone are ready. Sign in to this chat to receive a "
                    "venture-scoped build step.")
    picked = choose(pairs, hint, task_id)
    if picked is None:
        return options(pairs)
    milestone, task = picked
    what = f"{milestone['title']}: {task['title']}"
    from app.builder import budgets, execution as managed_builds

    if apps:
        try:
            quote = managed_builds.estimate(apps[0]["id"], actor=actor, plan_id=plan_id, plan_task_id=task["id"])
        except (managed_builds.BuildError, KeyError, PermissionError, ValueError) as exc:
            from app.chat.recovery import founder_failure
            failure = founder_failure(exc, venture_id, "your build estimate",
                {"action": "build.prepare", "payload": {"plan_task_id": task["id"]}},
                safety="No build or publication was started.")
            failure["body_md"] = intro + failure["body_md"]
            return failure
        if not quote.get("ready") or type(quote.get("ceiling_micro")) is not int:
            return text("A build estimate is not ready yet. Review your saved plan and build settings, then retry.",
                        [{"label": "Retry", "action": "build.prepare", "payload": {"plan_task_id": task["id"]}, "retry": True}])
        ceiling = quote["ceiling_micro"]
        card = _card({"app_id": apps[0]["id"], "quote_id": quote["quote_id"],
                      "input_digest": quote["input_digest"], "ceiling_micro": ceiling},
                     f"Build {apps[0]['name']} for milestone {milestone['title']}: {task['title']}.",
                     ["Queue this exact build.",
                      "The build will require separate QA and founder acceptance before publication."])
        return {"kind": "confirmation", "body_md": (intro +
            f"Next build step: {what}. You can start this build here. Nothing has been published."),
            "actions": [card], "citations": []}

    # No app yet: the confirmed action creates the draft app, quotes, and starts, in one step.
    name = idea_name.strip()[:120]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    if not name or not _SLUG.fullmatch(slug):
        return text("The plan is approved, but the app needs a name first. Open the idea to name it.",
                    [{"label": "Open idea", "href": f"/v/{venture_id}/idea", "style": "primary"}])
    from app.infra.apps import free_slug

    slug = free_slug(slug)  # addresses are global; never block a build on a name collision
    from app.chat.plan_chain import _workspace

    policy = budgets.runtime_quote(_workspace(venture_id), product=milestone.get("kind") != "landing")
    ceiling = policy.get("ceiling_micro")
    if not policy.get("ready") or type(ceiling) is not int:
        return text("The current build is unavailable right now. Review your saved inputs before retrying.")
    card = _card({"create_app": {"name": name, "slug": slug}, "plan_id": plan_id, "plan_task_id": task["id"],
                  "ceiling_micro": ceiling},
                 f"Set up the app {name} and build {what}.",
                 [f"Saves the draft app {name} and queues this exact build.",
                  "The build will require separate QA and founder acceptance before publication."])
    return {"kind": "confirmation", "body_md": (intro +
        f"Next build step: {what}. You can set up your app ({name}) at the address {slug} and start the build here. Nothing has been published."),
        "actions": [card], "citations": []}
