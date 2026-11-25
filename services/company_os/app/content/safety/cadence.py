"""Posting rhythm: daily caps, minimum gaps, jitter, and the promo ratio.

Both Unipile and Composio warn that fixed intervals and fixed daily times are
themselves an automation fingerprint, so nothing here schedules on a round
number. Every slot carries randomness and lands inside working hours.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.clock import utcnow
from sqlalchemy import func, select

from app.db.models import ContentPost
from app.state import db


@dataclass(frozen=True, slots=True)
class CadenceVerdict:
    ok: bool
    reason: str = ""


#: Minimum minutes between two posts on the same account.
MIN_GAP_MINUTES: dict[str, int] = {
    "x": 95,
    "linkedin": 1_200,
    "reddit": 1_440,
    "bluesky": 65,
    "mastodon": 65,
    "devto": 2_880,
}

#: Target spacing before jitter is applied.
BASE_INTERVAL_MINUTES: dict[str, int] = {
    "x": 420,
    "linkedin": 2_160,
    "reddit": 4_320,
    "bluesky": 260,
    "mastodon": 260,
    "devto": 5_040,
}

_DEFAULT_GAP = 180
_DEFAULT_INTERVAL = 600
_JITTER_FRACTION = 0.35

#: Throwaway-account burst. Production cadence stays conservative unless this
#: is explicitly on — LinkedIn's default 20h gap cannot prove a live pipeline.
_TEST_MIN_GAP: dict[str, int] = {"x": 2, "linkedin": 2, "reddit": 5}
_TEST_INTERVAL: dict[str, int] = {"x": 3, "linkedin": 4, "reddit": 8}
_TEST_DAILY_FLOOR = 12


def _test_cadence() -> bool:
    return os.environ.get("CONTENT_TEST_CADENCE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def min_gap_minutes(platform: str) -> int:
    if _test_cadence():
        return _TEST_MIN_GAP.get(platform, 2)
    return MIN_GAP_MINUTES.get(platform, _DEFAULT_GAP)


def base_interval_minutes(platform: str) -> int:
    if _test_cadence():
        return _TEST_INTERVAL.get(platform, 4)
    return BASE_INTERVAL_MINUTES.get(platform, _DEFAULT_INTERVAL)


def _active_hours() -> tuple[int, int]:
    try:
        start = int(os.environ.get("CONTENT_ACTIVE_HOUR_START", "8"))
        end = int(os.environ.get("CONTENT_ACTIVE_HOUR_END", "21"))
    except ValueError:
        return 8, 21
    if not 0 <= start < end <= 23:
        return 8, 21
    return start, end


def _utc_offset_minutes() -> int:
    try:
        return int(os.environ.get("CONTENT_TZ_OFFSET_MINUTES", "0"))
    except ValueError:
        return 0


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def day_key(moment: datetime | None = None) -> str:
    return _as_utc(moment or utcnow()).strftime("%Y-%m-%d")


def check_cadence(
    *,
    platform: str,
    daily_cap: int,
    posts_today: int,
    last_post_at: datetime | None,
    now: datetime | None = None,
) -> CadenceVerdict:
    moment = _as_utc(now or utcnow())
    cap = max(daily_cap, _TEST_DAILY_FLOOR) if _test_cadence() else daily_cap
    if cap <= 0:
        return CadenceVerdict(False, "daily_cap_zero")
    if posts_today >= cap:
        return CadenceVerdict(False, f"daily_cap_reached:{posts_today}/{cap}")
    if last_post_at is not None:
        gap = min_gap_minutes(platform)
        elapsed = (moment - _as_utc(last_post_at)).total_seconds() / 60.0
        if elapsed < gap:
            return CadenceVerdict(
                False, f"too_soon:{int(elapsed)}m<{gap}m"
            )
    return CadenceVerdict(True)


def next_slot(
    *,
    platform: str,
    after: datetime | None = None,
    rng: random.Random | None = None,
) -> datetime:
    """A jittered publish time inside working hours, never on a round interval."""
    roll = rng or random
    base = _as_utc(after or utcnow())
    interval = base_interval_minutes(platform)
    spread = max(1, int(interval * _JITTER_FRACTION))
    minutes = interval + roll.randint(-spread, spread)
    slot = base + timedelta(minutes=max(min_gap_minutes(platform), minutes))
    slot += timedelta(seconds=roll.randint(0, 59))

    start, end = _active_hours()
    offset = _utc_offset_minutes()
    for _ in range(8):
        local = slot + timedelta(minutes=offset)
        if start <= local.hour < end:
            return slot
        # Walk forward to the next local morning rather than clamping to it.
        if local.hour >= end:
            local = (local + timedelta(days=1)).replace(
                hour=start, minute=roll.randint(0, 59)
            )
        else:
            local = local.replace(hour=start, minute=roll.randint(0, 59))
        slot = local - timedelta(minutes=offset)
    return slot


def promo_allowed(
    *,
    workspace_id: str,
    platform: str,
    ratio: int,
    window_days: int = 30,
) -> CadenceVerdict:
    """Enforce the value:promo budget from the brief before scheduling a promo."""
    if ratio <= 0:
        return CadenceVerdict(True)
    since = utcnow() - timedelta(days=window_days)
    factory = db()
    with factory() as session:
        rows = session.execute(
            select(ContentPost.intent, func.count(ContentPost.id))
            .where(
                ContentPost.workspace_id == workspace_id,
                ContentPost.platform == platform,
                ContentPost.state == "published",
                ContentPost.published_at >= since,
            )
            .group_by(ContentPost.intent)
        ).all()
    counts = {str(kind): int(total) for kind, total in rows}
    value = counts.get("value", 0)
    promo = counts.get("promo", 0)
    if promo == 0:
        return CadenceVerdict(True)
    if value < promo * ratio:
        return CadenceVerdict(
            False, f"promo_ratio_spent:{value}value/{promo}promo<{ratio}:1"
        )
    return CadenceVerdict(True)
