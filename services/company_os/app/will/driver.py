"""Slate wakeups use the same consent, admission and budgets as autopilot."""

from __future__ import annotations

import logging

from app.ventures.lifecycle import is_paused, list_active

log = logging.getLogger(__name__)

_LIVE = {
    "live",
    "operate",
    "building",
    "demand_testing",
    "hosting",
    "readiness_review",
}


async def _drive_one(ven: dict) -> tuple[str, str]:
    """Only an opted-in live venture may enter the guarded journey."""
    from app.autopilot import guard, service

    vid = str(ven.get("id") or "")
    if not vid:
        return ("skip", "")
    if ven.get("paused") or is_paused(vid):
        return ("skipped", vid)
    if str(ven.get("stage") or "") not in _LIVE:
        return ("skip", vid)
    from app.munder.control import should_halt
    from app.munder.breaker import block_for_venture
    from app.paperclip.budget import hard_stop_for_venture

    if should_halt(vid):
        return ("halted", vid)
    if hard_stop_for_venture(vid):
        return ("budget", vid)
    if block_for_venture(vid):
        return ("breaker", vid)
    if not guard.get_settings(vid)["god_mode"]:
        return ("skipped", vid)
    if guard.mac_offline(vid, service._owner(vid)):
        return ("skipped", vid)
    result = await service.tick(vid)
    return ("started" if result["done"] or result["status"] == "queued" else result["status"], vid)


async def drive_open_work() -> dict:
    """Serialize guarded passes; one venture's failure cannot block the rest."""
    started: list[str] = []
    skipped: list[str] = []
    # ponytail: one venture pass at a time; use a bounded pool if admission throughput requires it.
    for ven in list_active():
        try:
            status, ident = await _drive_one(ven)
        except Exception:
            log.exception("autopilot worker crashed")
            continue
        if status == "started":
            started.append(ident)
        elif status == "skipped":
            skipped.append(ident)
    return {"started": started, "skipped": skipped}
