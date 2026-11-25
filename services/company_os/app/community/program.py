"""First 30-day community program compiler."""

from __future__ import annotations

from typing import Any

from app.community.plan import CommunityIdentity, CommunityThesis, ObjectiveMix, Program30d


def generate_program_30d(
    thesis: CommunityThesis,
    research: dict[str, Any],
    identity: CommunityIdentity,
    objectives: ObjectiveMix,
) -> Program30d:
    customers = int(research.get("paying_customers") or 0)
    invites = min(50, max(25, customers or 25))
    seg = thesis.primary_segment or {}
    pains = [str(p) for p in (seg.get("pains") or [])] or ["the weekly craft job"]
    features = [str(f) for f in (research.get("features") or [])] or ["the core workflow"]
    objections = [str(o) for o in (seg.get("objections") or [])] or ["migration risk"]
    persona = str(seg.get("role") or "member")
    channels = {c["slug"] for c in identity.channels}

    rituals: list[dict[str, Any]] = []
    if objectives.education >= 35 or objectives.archetype == "dev_tool":
        rituals.append(
            {
                "id": "daily_craft_prompt",
                "cadence": "daily",
                "weekday": None,
                "channel": "help" if "help" in channels else "intros",
                "objective": "education",
                "prompt_template": f"{{persona}} Monday: show the ugly version of {{pain}}.",
                "binder": {"pain": pains[0], "persona": persona},
                "owner": "community_host",
                "success_metric": ">=3 unique replies",
            }
        )

    rituals.extend(
        [
            {
                "id": "weekly_help_ama",
                "cadence": "weekly",
                "weekday": "tue",
                "channel": "office-hours" if "office-hours" in channels else "help",
                "objective": "education",
                "prompt_template": "Office hours on objection: {objection}",
                "binder": {"objection": objections[0]},
                "owner": "founder",
                "success_metric": ">=5 unique members",
            },
            {
                "id": "weekly_artifact",
                "cadence": "weekly",
                "weekday": "thu",
                "channel": "playbooks" if "playbooks" in channels else "help",
                "objective": "education",
                "prompt_template": "Drop a template/snippet for {feature} that kills {pain}",
                "binder": {"feature": features[0], "pain": pains[0]},
                "owner": "champion",
                "success_metric": ">=3 artifacts",
            },
            {
                "id": "weekly_wins",
                "cadence": "weekly",
                "weekday": "fri",
                "channel": "ship" if "ship" in channels else "intros",
                "objective": "networking",
                "prompt_template": "Win of the week tied to the job: {pain}",
                "binder": {"pain": pains[0]},
                "owner": "community_host",
                "success_metric": ">=5 win posts",
            },
            {
                "id": "biweekly_teardown",
                "cadence": "biweekly",
                "weekday": "wed",
                "channel": "playbooks" if "playbooks" in channels else "help",
                "objective": "education",
                "prompt_template": "Founder teardown of a competitor workflow (product in last 20%)",
                "binder": {"pain": pains[0]},
                "owner": "founder",
                "success_metric": ">=10 engaged members",
            },
            {
                "id": "monthly_live",
                "cadence": "monthly",
                "weekday": None,
                "channel": "office-hours" if "office-hours" in channels else "welcome",
                "objective": objectives.primary,
                "prompt_template": "60-min live on one trigger; recap becomes Loop-3 content",
                "binder": {"pain": pains[0]},
                "owner": "founder",
                "success_metric": "recap posted with insight cite",
            },
        ]
    )

    weeks = [
        {
            "n": 1,
            "theme": f"Founding 50 do {pains[0]} in public",
            "rituals": ["weekly_help_ama", "weekly_artifact"],
        },
        {
            "n": 2,
            "theme": f"Teach {features[0]} as the job",
            "rituals": ["weekly_artifact", "weekly_wins"],
        },
        {
            "n": 3,
            "theme": f"Member-to-member on {pains[0]}",
            "rituals": ["weekly_wins", "biweekly_teardown"],
        },
        {
            "n": 4,
            "theme": "Ritual lock + champion ask",
            "rituals": ["weekly_help_ama", "monthly_live"],
        },
    ]

    return Program30d(
        kpis={
            "first_action_d3": 0.60,
            "return_d14": 0.40,
            "contrib_d30": 0.20,
            "activation_7d": "0.25-0.40",
            "note": "Optimize activation/contribution — not vanity member count",
        },
        weeks=weeks,
        rituals=rituals,
        invites_target=invites,
        public_launch=False,
        staff_post_share_min=0.70,
    )
