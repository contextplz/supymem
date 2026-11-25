"""Plan catalogue. Seats are per-workspace (docs/supymem/07 §2)."""

from __future__ import annotations

from typing import TypedDict


class TierSpec(TypedDict):
    seats: int
    features: tuple[str, ...]
    inr_paise: int
    usd_cents: int
    label: str
    allowance_credits: int
    live_ventures: int
    hosted_apps: int


TIERS: dict[str, TierSpec] = {
    "free": {
        "seats": 1,
        "features": ("local_app", "credits_meter"),
        "inr_paise": 0,
        "usd_cents": 0,
        "label": "Free",
        "allowance_credits": 0,
        "live_ventures": 0,
        "hosted_apps": 0,
    },
    "founder": {
        "seats": 3,
        "features": (
            "local_app",
            "credits_meter",
            "hosted_backend",
            "hosted_apps",
            "harness_research",
            "cloud_agents",
            "ai_proxy",
            "team_seats",
        ),
        "inr_paise": 249900,
        "usd_cents": 2900,
        "label": "Founder",
        "allowance_credits": 2500,
        "live_ventures": 1,
        "hosted_apps": 1,
    },
    "team": {
        "seats": 10,
        "features": (
            "local_app",
            "credits_meter",
            "hosted_backend",
            "hosted_apps",
            "harness_research",
            "cloud_agents",
            "ai_proxy",
            "team_seats",
            "priority_support",
        ),
        "inr_paise": 799900,
        "usd_cents": 9500,
        "label": "Team",
        "allowance_credits": 8000,
        "live_ventures": 3,
        "hosted_apps": 5,
    },
}

# Alias so existing "pro" checkout/tests resolve to Founder (₹2,499).
TIERS["pro"] = TIERS["founder"]

FEATURES = frozenset(
    {
        "local_app",
        "credits_meter",
        "hosted_backend",
        "hosted_apps",
        "harness_research",
        "cloud_agents",
        "ai_proxy",
        "team_seats",
        "priority_support",
    }
)

TRIAL_DAYS = 14
OFFLINE_GRACE_DAYS = 7
