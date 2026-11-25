"""Compile an executable marketing plan into dated atomic jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from core.ids import new_id

from app.marketing import store


def _at_hour(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _linkedin_slots(start: datetime, per_week: int, days: int) -> list[datetime]:
    """Spread LinkedIn posts across weekdays in the compile window.

    Always look at least 7 calendar days ahead so a Friday/weekend compile
    still schedules next week's Mon–Fri posts.
    """
    per_week = max(1, min(per_week, 7))
    preferred_hours = [9, 11, 14, 16, 10]
    slots: list[datetime] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = max(days, 7)
    end = day + timedelta(days=horizon)
    target = max(1, min(per_week, (per_week * horizon + 6) // 7))
    weekday_cursor = 0
    while day < end and len(slots) < target:
        if day.weekday() < 5:  # Mon–Fri
            hour = preferred_hours[weekday_cursor % len(preferred_hours)]
            slot = _at_hour(day, hour)
            if slot >= start:
                week_start = day - timedelta(days=day.weekday())
                in_week = sum(
                    1
                    for s in slots
                    if week_start <= s < week_start + timedelta(days=7)
                )
                if in_week < per_week:
                    slots.append(slot)
                    weekday_cursor += 1
        day += timedelta(days=1)
    return slots


def _x_slots(start: datetime, per_day: int, days: int) -> list[datetime]:
    per_day = max(1, min(per_day, 6))
    hours = [8, 12, 17, 20, 10, 15][:per_day]
    slots: list[datetime] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days):
        d = day + timedelta(days=i)
        for h in hours:
            slot = _at_hour(d, h)
            if slot >= start:
                slots.append(slot)
    return slots


def _reddit_slots(start: datetime, per_week: int, days: int) -> list[datetime]:
    per_week = max(1, min(per_week, 5))
    slots: list[datetime] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    # Tue / Thu / Sat mid-day.
    targets = {1, 3, 5}
    for i in range(days):
        d = day + timedelta(days=i)
        if d.weekday() in targets:
            slot = _at_hour(d, 13)
            if slot >= start:
                slots.append(slot)
            if len(slots) >= max(1, (per_week * days + 6) // 7):
                break
    return slots[: max(1, (per_week * days + 6) // 7)]


def compile_plan(
    *,
    workspace_id: str,
    plan: dict[str, Any],
    days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Expand plan.content.platforms into create → visual → publish job chains."""
    moment = now or utcnow()
    strategy = plan.get("strategy") or {}
    content = strategy.get("content") or {}
    platforms = content.get("platforms") or {}
    visuals = content.get("visuals") or {}
    visuals_enabled = bool(visuals.get("enabled", True))
    plan_id = plan["id"]

    connected: set[str] = set()
    try:
        from app.content.oauth import flow as oauth_flow

        connected = {
            a["platform"]
            for a in oauth_flow.list_accounts(workspace_id)
            if a.get("status") == "active"
        }
    except Exception:
        connected = set()

    specs: list[dict[str, Any]] = []

    for platform, cfg in platforms.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        # Only schedule publish chains for channels that are actually connected.
        # Community find/draft can still run without a live account.
        if platform in {"linkedin", "x"} and connected and platform not in connected:
            continue
        if platform == "reddit" and connected and platform not in connected:
            # Still allow find_threads / draft_reply without posting.
            if (strategy.get("community") or {}).get("mode") == "find_and_draft":
                slots = _reddit_slots(moment, int(cfg.get("per_week") or 1), days)
                for slot in slots:
                    find_id = new_id("mjb")
                    draft_id = new_id("mjb")
                    specs.append(
                        {
                            "id": find_id,
                            "workspace_id": workspace_id,
                            "plan_id": plan_id,
                            "kind": "find_threads",
                            "platform": "reddit",
                            "title": f"Find Reddit discussions · {slot.strftime('%a %b %d')}",
                            "payload": {
                                "limit": 10,
                                "themes": strategy.get("themes") or [],
                            },
                            "depends_on": [],
                            "status": "pending",
                            "priority": 40,
                            "scheduled_for": slot - timedelta(hours=2),
                        }
                    )
                    specs.append(
                        {
                            "id": draft_id,
                            "workspace_id": workspace_id,
                            "plan_id": plan_id,
                            "kind": "draft_reply",
                            "platform": "reddit",
                            "title": f"Draft Reddit replies · {slot.strftime('%a %b %d')}",
                            "payload": {"limit": 3},
                            "depends_on": [find_id],
                            "status": "pending",
                            "priority": 45,
                            "scheduled_for": slot - timedelta(hours=1),
                        }
                    )
            continue
        if platform == "linkedin":
            slots = _linkedin_slots(moment, int(cfg.get("per_week") or 4), days)
            want_visual = visuals_enabled and cfg.get("format") != "text"
        elif platform == "x":
            slots = _x_slots(moment, int(cfg.get("per_day") or 2), days)
            want_visual = False
        elif platform == "reddit":
            slots = _reddit_slots(moment, int(cfg.get("per_week") or 1), days)
            want_visual = False
            if (strategy.get("community") or {}).get("mode") == "find_and_draft":
                for slot in slots:
                    find_id = new_id("mjb")
                    draft_id = new_id("mjb")
                    specs.append(
                        {
                            "id": find_id,
                            "workspace_id": workspace_id,
                            "plan_id": plan_id,
                            "kind": "find_threads",
                            "platform": "reddit",
                            "title": f"Find Reddit discussions · {slot.strftime('%a %b %d')}",
                            "payload": {
                                "limit": 10,
                                "themes": strategy.get("themes") or [],
                            },
                            "depends_on": [],
                            "status": "pending",
                            "priority": 40,
                            "scheduled_for": slot - timedelta(hours=2),
                        }
                    )
                    specs.append(
                        {
                            "id": draft_id,
                            "workspace_id": workspace_id,
                            "plan_id": plan_id,
                            "kind": "draft_reply",
                            "platform": "reddit",
                            "title": f"Draft Reddit replies · {slot.strftime('%a %b %d')}",
                            "payload": {"limit": 3},
                            "depends_on": [find_id],
                            "status": "pending",
                            "priority": 45,
                            "scheduled_for": slot - timedelta(hours=1),
                        }
                    )
        else:
            continue

        for slot in slots:
            create_id = new_id("mjb")
            specs.append(
                {
                    "id": create_id,
                    "workspace_id": workspace_id,
                    "plan_id": plan_id,
                    "kind": "create_content",
                    "platform": platform,
                    "title": f"Create {platform} post · {slot.strftime('%a %b %d %H:%M')}",
                    "payload": {
                        "platform": platform,
                        "themes": strategy.get("themes") or [],
                    },
                    "depends_on": [],
                    "status": "pending",
                    "priority": 20 if platform == "linkedin" else 30,
                    "scheduled_for": slot - timedelta(hours=3),
                }
            )
            publish_depends = [create_id]
            if want_visual:
                visual_id = new_id("mjb")
                specs.append(
                    {
                        "id": visual_id,
                        "workspace_id": workspace_id,
                        "plan_id": plan_id,
                        "kind": "create_visual",
                        "platform": platform,
                        "title": f"Create visual · {platform} · {slot.strftime('%a %b %d')}",
                        "payload": {
                            "platform": platform,
                            "template": visuals.get("default_template") or "insight_card",
                            "brand": visuals.get("brand") or {},
                        },
                        "depends_on": [create_id],
                        "status": "pending",
                        "priority": 25,
                        "scheduled_for": slot - timedelta(hours=2),
                    }
                )
                publish_depends = [visual_id]
            publish_id = new_id("mjb")
            specs.append(
                {
                    "id": publish_id,
                    "workspace_id": workspace_id,
                    "plan_id": plan_id,
                    "kind": "publish_content",
                    "platform": platform,
                    "title": f"Publish {platform} · {slot.strftime('%a %b %d %H:%M')}",
                    "payload": {"platform": platform, "require_proof": True},
                    "depends_on": publish_depends,
                    "status": "pending",
                    "priority": 10,
                    "scheduled_for": slot,
                }
            )
            # Browser verify after API publish (OpenClaw/Playwright) — LinkedIn first.
            if platform == "linkedin":
                specs.append(
                    {
                        "id": new_id("mjb"),
                        "workspace_id": workspace_id,
                        "plan_id": plan_id,
                        "kind": "check_result",
                        "platform": platform,
                        "title": f"Verify {platform} · {slot.strftime('%a %b %d %H:%M')}",
                        "payload": {
                            "platform": platform,
                            "verify_browser": True,
                            "require_proof": True,
                        },
                        "depends_on": [publish_id],
                        "status": "pending",
                        "priority": 5,
                        "scheduled_for": slot + timedelta(minutes=3),
                    }
                )

    # Cold mail — wrap gtm.supymem when strategy.outbound.enabled.
    outbound = strategy.get("outbound") or {}
    if outbound.get("enabled"):
        per_week = max(1, min(int(outbound.get("per_week") or 3), 7))
        day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        emitted = 0
        for i in range(max(days, 7)):
            d = day + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            slot = _at_hour(d, 10)
            if slot < moment:
                continue
            enqueue_id = new_id("mjb")
            send_id = new_id("mjb")
            specs.append(
                {
                    "id": enqueue_id,
                    "workspace_id": workspace_id,
                    "plan_id": plan_id,
                    "kind": "enqueue_outreach",
                    "platform": "email",
                    "title": f"Enqueue cold mail · {slot.strftime('%a %b %d')}",
                    "payload": {
                        "contact_ids": outbound.get("contact_ids") or [],
                        "auto_send": False,
                    },
                    "depends_on": [],
                    "status": "pending",
                    "priority": 35,
                    "scheduled_for": slot - timedelta(hours=1),
                }
            )
            specs.append(
                {
                    "id": send_id,
                    "workspace_id": workspace_id,
                    "plan_id": plan_id,
                    "kind": "send_email_touch",
                    "platform": "email",
                    "title": f"Send email touch · {slot.strftime('%a %b %d')}",
                    "payload": {"limit": int(outbound.get("limit") or 3)},
                    "depends_on": [enqueue_id],
                    "status": "pending",
                    "priority": 36,
                    "scheduled_for": slot,
                }
            )
            emitted += 1
            if emitted >= per_week:
                break

    if (outbound.get("mode") or "") == "founder_led":
        specs.append(
            {
                "id": new_id("mjb"),
                "workspace_id": workspace_id,
                "plan_id": plan_id,
                "kind": "create_relationship_asset",
                "platform": "relationships",
                "title": "Monthly relationship SKU (in_review)",
                "payload": {"period": moment.strftime("%Y-%m")},
                "depends_on": [],
                "status": "pending",
                "priority": 38,
                "scheduled_for": moment,
            }
        )

    return store.insert_jobs(specs)
