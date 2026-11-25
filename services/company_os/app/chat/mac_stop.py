"""Chat milestone for a Mac task that stopped with a #616 ``cu_*`` reason code."""
from __future__ import annotations

import json
import re
from contextlib import nullcontext

from sqlalchemy.exc import SQLAlchemyError

from app.chat.blockers import GUIDES, guide_code, with_explain
from app.chat.mac_offers import cu_offer
from app.chat.hosting import OFFER as HOST_OFFER
from app.chat.milestones import post_milestone


def _task_details(venture_id: str, order_id: str, session) -> dict:
    from app.core.local_mode import is_local
    from app.db.models import VentureDocument
    from app.state import db

    fallback = {"task_id": order_id, "order_id": order_id}
    if not is_local():
        return fallback
    try:
        with nullcontext(session) if session is not None else db()() as saved:
            row = saved.get(VentureDocument, order_id)
            if row is None or row.venture_id != venture_id:
                return fallback
            body = json.loads(row.provenance_json or "{}")
            manifest = body.get("manifest", {})
            snapshot = body.get("task_snapshot", {})
            task = snapshot.get("kind") if snapshot.get("kind") == "landing_page_build" else manifest.get("task_id")
            if not isinstance(task, str) or not task:
                return fallback
            coding = manifest.get("coding_loop_verification") or manifest.get("coding_verification")
            from app.pagebuilder.local import saved_page_check_message
            return {**fallback, "task_id": task,
                    "check_message": saved_page_check_message(body),
                    **({"build_action": "pagebuilder.build" if snapshot.get("kind") == "landing_page_build" else "build.tool.start"} if coding else {})}
    except (SQLAlchemyError, ValueError, TypeError, AttributeError):
        return fallback


def post_mac_stopped(venture_id: str, order_id: str, reason_code: str | None, title: str | None = None,
                     *, outcome_code: str | None = None, session=None) -> bool:
    """Deduplicated guidance; stale notices can join the lease transaction."""
    reason_guide = guide_code(reason_code)
    code = guide_code(f"mac_{outcome_code}") or reason_guide
    if (outcome_code == "needs_founder" and reason_guide
            or outcome_code == "blocked" and reason_guide == "computer_use_permissions"):
        code = reason_guide
    if code is None:
        return False
    guide = GUIDES[code]
    task = _task_details(venture_id, order_id, session)
    next_step = guide["steps"][0].rstrip(".").replace("Mac task", "task").replace("Mac app", "desktop app")
    next_step = re.sub(r"\b(?:your|this|the same) Mac\b", "your laptop", next_step)
    what = ("Your coding tool stopped before I could verify its work. Choose Retry to review it."
            if code == "mac_needs_founder" else f"Your coding tool stopped. {next_step}. Then choose Retry.")
    what = task.pop("check_message", None) or what
    actions = [{"label": "Retry", "href": f"/v/{venture_id}/device-work-orders/{order_id}", "style": "primary",
                "task_id": task["task_id"], "payload": task}]
    if code == "computer_use_permissions" and (offer := cu_offer(venture_id, "mac_screen_recording")):
        actions.insert(0, offer)
    reply = with_explain({"actions": actions}, code)
    prefix = "mac_unresponsive" if code == "mac_unresponsive" else "mac_stopped"
    posted = post_milestone(venture_id, key=f"{prefix}:{order_id}", kind="mac_task_stopped",
                            body_md=what,
                            actions=reply["actions"],
                            failure={"step": "mac.task", "code": reason_code or code, "job_id": order_id},
                            session=session)
    if posted:
        from app.needs_you.hooks import from_mac_stop

        from_mac_stop(venture_id, order_id, code)
    return posted


def post_mac_finished(venture_id: str, order_id: str, title: str | None = None) -> bool:
    """Once per order: a cleanly closed, not-yet-verified Mac task is ready for the founder's yes/no."""
    href = f"/v/{venture_id}/device-work-orders/{order_id}"
    task = _task_details(venture_id, order_id, None)
    what = "Your coding tool finished; open the result and tell me if it looks right."
    return post_milestone(venture_id, key=f"mac_finished:{order_id}", kind="mac_task_finished",
                          body_md=what,
                          actions=[{"label": "Review the result", "href": href, "style": "primary", "task_id": task["task_id"], "payload": task},
                                   *([HOST_OFFER] if (title or "").startswith("Build ") else [])])
