"""Local recurring work, saved capability gates and opt-in API lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation

from app.core.local_mode import is_local

_worker: asyncio.Task[None] | None = None
log = logging.getLogger(__name__)

BACKGROUND_JOBS = {
    "inbox.briefing.tick": "You get summaries at your saved times for mailboxes you connected and allowed your app to read.",
    "inbox.cos_brief.tick": "You get a weekly summary at the time you chose.",
    "gtm.journey.maintain": "Follow up only on campaigns you started, within your sending limits and opt-out rules. Switch on both Background work and Send email in Settings > Capabilities.",
    "gtm.gmail.read": "Read replies to messages you sent and save opt-outs before another email can go out.",
    "autopilot.tick": "Advance only experiments you asked to run, within their saved limits.",
    "suggestions.generate": "You get a limited list of suggestions for work you started. Nothing starts until you ask.",
    "experiment.evaluate": "Check results for experiments you started, without inventing observations or starting another test.",
    "experiment.beliefs.tick": "Check saved evidence for experiments you started, within their saved limits.",
    "routines.fire": "Run only routines you saved, respecting their schedules, quiet hours and limits.",
    "routines.report": "Show results for your saved routines after they finish.",
}


def background_jobs(name: str) -> list[dict]:
    from app.worker.catalog import load_catalogue

    schedules = {"5m": "Every 5 minutes", "hourly": "Every hour", "daily": "Every day",
                 "founder_schedule": "On your saved schedule"}
    jobs = []
    for kind, spec in load_catalogue()["kinds"].items():
        if kind not in BACKGROUND_JOBS or not spec.get("stage2"):
            continue
        names = {spec.get("local_capability")}
        if kind == "gtm.journey.maintain":
            names.add("background_work")
        if name in names:
            trigger = spec.get("local_trigger", spec.get("trigger"))
            jobs.append({"kind": kind, "description": BACKGROUND_JOBS[kind], "schedule": schedules[trigger]})
    return jobs


def capability_enabled(name: str) -> bool:
    try:
        from app.core.local_capabilities import enabled
    except ImportError:
        # shortcut: owner toggles need local_capabilities, remove this fallback when that shared module is integrated.
        from cryptography.exceptions import InvalidTag
        from app.core.local_secrets import get_secret, read_settings

        if name in {"send_email", "read_replies"}:
            try:
                values = read_settings("smtp")["values"]
                required = ("host", "user", "postal_address") if name == "send_email" else ("imap_host", "user")
                return bool(all(values.get(field) for field in required) and get_secret("smtp", "password"))
            except (OSError, ValueError, InvalidTag):
                return False
        return True
    return enabled(name)


def spend_available() -> bool:
    from cryptography.exceptions import InvalidTag
    from app.core.local_secrets import read_settings
    from app.credits import local_meter
    from app.credits.errors import CreditsUnavailable
    from app.credits.units import usd_to_micro

    try:
        value = read_settings("spend")["values"].get("monthly_spend_cap")
        if value is None:
            return True
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        with local_meter._database() as connection:
            return local_meter._spent(connection, local_meter._month()) < usd_to_micro(amount)
    except (CreditsUnavailable, InvalidTag, OSError, ValueError, TypeError, InvalidOperation):
        log.warning("Open Keys and connections and check your monthly spending limit and local data folder before retrying.")
        return False


def blocked_reason(kind: str) -> str | None:
    from app.worker.catalog import load_catalogue
    from app.tasks.scheduler import INTERVALS
    if kind == "retention.purge":
        return None

    spec = load_catalogue()["kinds"].get(kind) or {}
    if spec.get("trigger") in INTERVALS and kind not in BACKGROUND_JOBS:
        return "Your local app does not run this scheduled work. Open Settings > Capabilities and choose your background work instead."
    name = spec.get("local_capability")
    if not name:
        return None
    if kind == "gtm.journey.maintain" and not capability_enabled("background_work"):
        try:
            from app.core.local_capabilities import blocked_message
        except ImportError:
            return "Open Settings > Features and turn on Background work, then retry your campaign."
        return blocked_message("background_work")
    if not capability_enabled(name):
        try:
            from app.core.local_capabilities import blocked_message
        except ImportError:
            if name == "send_email":
                return "Open Features and turn on Send email, then open Keys and connections and set your SMTP host, mailbox address, password and postal address."
            if name == "read_replies":
                return "Open Features and turn on Read replies, then open Keys and connections and set your IMAP host, mailbox address and password."
            return "Open Features and turn this feature on, then retry your background work."
        return blocked_message(name)
    if not spend_available():
        return "Open Keys and connections and increase your monthly spending limit, or wait until next month. Check that your local data folder is writable."
    return None


def tick_kinds(tick: str) -> list[str]:
    from app.worker.catalog import load_catalogue

    available = spend_available()
    retention = ["retention.purge"] if tick == "daily" else []
    return retention + [kind for kind, spec in load_catalogue()["kinds"].items()
        if kind in BACKGROUND_JOBS and spec.get("stage2") and spec.get("local_trigger", spec.get("trigger")) == tick
        and (kind != "gtm.journey.maintain" or capability_enabled("background_work"))
        and available and spec.get("local_capability") and capability_enabled(spec["local_capability"])]


def _targets(payload: dict) -> list[str]:
    from sqlalchemy import select
    from app.db.models import Venture, Workspace
    from app.state import db
    from app.tenancy import current_or_none

    query = select(Venture.id).join(Workspace, Venture.workspace_id == Workspace.id).where(
        Venture.archived_at.is_(None), Workspace.archived_at.is_(None))
    scope = current_or_none()
    if scope and scope.workspace_id not in {"", "none"}:
        query = query.where(Venture.workspace_id == scope.workspace_id)
        if scope.venture_id:
            query = query.where(Venture.id == scope.venture_id)
    if payload.get("founder_id"):
        query = query.where(Workspace.owner_id == payload["founder_id"])
    with db()() as session:
        return list(session.scalars(query.order_by(Venture.id)))


async def maintain_campaigns(payload: dict) -> None:
    from app.autopilot import guard, outreach
    from app.autopilot.authority import founder_scope
    from app.gtm import journey
    from app.connections.composio_pacing import RateLimited
    from app.ventures.lifecycle import is_paused

    for venture_id in await asyncio.to_thread(_targets, payload):
        if blocked_reason("gtm.journey.maintain"):
            return
        try:
            with founder_scope(venture_id, autonomous=True) as scope:
                if not await asyncio.to_thread(lambda: not is_paused(venture_id) and guard.get_settings(venture_id)["allow_send"]):
                    continue
                await asyncio.to_thread(outreach.schedule_followups, venture_id)
                await journey.auto_send(scope.workspace_id, venture_id, scope.actor)
        except RateLimited:
            raise
        except Exception:
            log.error("Open Find customers to review your campaign, then open Keys and connections and check your SMTP host, mailbox address, password and postal address.",
                extra={"venture_id": venture_id})


async def read_replies(payload: dict) -> None:
    from app.autopilot import outreach
    from app.autopilot.authority import founder_scope
    from app.worker.handlers import _gmail_read
    from app.connections.composio_pacing import RateLimited

    if payload.get("mailbox_id") or payload.get("workspace_id"):
        await _gmail_read(payload)
        return
    for venture_id in await asyncio.to_thread(_targets, payload):
        if blocked_reason("gtm.gmail.read"):
            return
        try:
            with founder_scope(venture_id, autonomous=True):
                if not await asyncio.to_thread(outreach.drafts, venture_id, "sent"):
                    continue
                await outreach.poll_replies(venture_id)
        except RateLimited:
            raise
        except Exception:
            log.error("Open Keys and connections and check your mailbox address, password and IMAP host before retrying.",
                extra={"venture_id": venture_id})


def evaluate_experiments(payload: dict) -> None:
    from app.autopilot.authority import founder_scope
    from app.experiments import ledger, service

    if not ledger.autopilot_enabled():
        return
    for venture_id in _targets(payload):
        if blocked_reason("experiment.evaluate"):
            return
        with founder_scope(venture_id, autonomous=True) as scope:
            service.run_evaluate({**payload, "venture_id": venture_id, "workspace_id": scope.workspace_id})


def install_handlers() -> None:
    from app.worker.handlers import enabled_kinds, register

    for kind, handler in {"gtm.journey.maintain": maintain_campaigns, "gtm.gmail.read": read_replies,
            "experiment.evaluate": evaluate_experiments}.items():
        if kind in enabled_kinds():
            register(kind, handler)


def is_running() -> bool:
    return _worker is not None and not _worker.done()


def redis_url() -> str:
    return "" if is_local() else os.environ.get("REDIS_URL", "").strip()


@asynccontextmanager
async def lifespan():
    global _worker
    if not is_local() or os.environ.get("SUPYMEM_SINGLE_PROCESS", "").strip() != "1":
        yield
        return

    from app.worker.main import run_worker

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop))
    _worker = task
    try:
        yield
    finally:
        stop.set()
        try:
            await task
        finally:
            if _worker is task:
                _worker = None
