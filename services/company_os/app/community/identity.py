"""Generate CommunityIdentity + objective mix from ICP when build_owned."""

from __future__ import annotations

from typing import Any

from app.community.plan import CommunityIdentity, CommunityThesis, ObjectiveMix

_CRAFT_NOUNS = (
    "Guild", "Forge", "Lab", "Circle", "Yard", "Studio", "League",
    "Collective", "Workshop", "Council", "Room", "Crew",
)
# "Circle" banned as default product-community name — keep in table but avoid as first pick
_BANNED_DEFAULT = {"Circle"}


def objective_mix(thesis: CommunityThesis, research: dict[str, Any]) -> ObjectiveMix:
    seg = thesis.primary_segment or {}
    depth = str(seg.get("technical_depth") or "").lower()
    dim = {d.id: d.score for d in thesis.dimensions}
    customers = int(research.get("paying_customers") or 0)
    holes = [str(h).lower() for h in (seg.get("watering_holes") or [])]
    self_serve = bool(seg.get("self_serve"))
    buying = bool(seg.get("buying_committee"))
    trace: list[str] = ["base 20/20/20/10"]

    edu, net, adopt, acq = 20, 20, 20, 10

    if depth == "high" or len(research.get("features") or []) >= 3:
        edu += 20
        trace.append("+20 education (depth/features)")
    if depth == "low":
        edu -= 10
        net += 10
        trace.append("low depth → networking")
    if dim.get("D3", 0) >= 4:
        net += 15
        trace.append("+15 networking (identity)")
    if self_serve and dim.get("D4", 0) >= 3:
        adopt += 20
        trace.append("+20 adoption (self-serve)")
    if buying:
        adopt -= 15
        edu += 10
        net += 5
        trace.append("buying committee → enablement")
    if str(research.get("pmf_signal") or "") == "strong" and customers >= 200:
        acq += 10
        trace.append("+10 acquisition unlocked")
    if customers < 50:
        acq -= 10
        edu += 10
        trace.append("early stage → education over acquisition")

    tech_holes = any(any(t in h for t in ("github", "hn", "stackoverflow", "discord")) for h in holes)
    social_holes = any(any(t in h for t in ("instagram", "tiktok", "linkedin", "creator")) for h in holes)
    if tech_holes:
        edu += 10
        adopt += 10
        trace.append("technical watering holes")
    if social_holes:
        net += 15
        acq += 10
        trace.append("social watering holes")

    # Normalize to 100
    total = max(edu + net + adopt + acq, 1)
    edu = round(100 * edu / total)
    net = round(100 * net / total)
    adopt = round(100 * adopt / total)
    acq = 100 - edu - net - adopt

    if thesis.verdict != "build_owned" or customers < 200:
        if acq > 15:
            spill = acq - 15
            acq = 15
            edu += spill
            trace.append("cap acquisition≤15")
    if acq > 25:
        spill = acq - 25
        acq = 25
        edu += spill
        trace.append("cap acquisition≤25 first 90d")
    if dim.get("D4", 0) >= 4 and edu < 25:
        need = 25 - edu
        edu = 25
        adopt = max(0, adopt - need)
        trace.append("education floor 25")
    if not self_serve and adopt > 20:
        spill = adopt - 20
        adopt = 20
        edu += spill
        trace.append("adoption cap without self-serve")

    # Re-fix after caps
    total = max(edu + net + adopt + acq, 1)
    edu = round(100 * edu / total)
    net = round(100 * net / total)
    adopt = round(100 * adopt / total)
    acq = 100 - edu - net - adopt

    scores = {
        "education": edu,
        "networking": net,
        "product_adoption": adopt,
        "acquisition": acq,
    }
    ordered = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
    primary, secondary = ordered[0], ordered[1]

    # Archetype
    role = str(seg.get("role") or "").lower()
    if depth == "high" or "engineer" in role or "dev" in role:
        archetype = "dev_tool"
    elif any(x in role for x in ("creator", "photographer", "freelancer")):
        archetype = "prosumer"
    else:
        archetype = "b2b_saas"

    return ObjectiveMix(
        education=edu,
        networking=net,
        product_adoption=adopt,
        acquisition=acq,
        primary=primary,
        secondary=secondary,
        archetype=archetype,
        signal_trace=trace,
    )


def generate_identity(
    thesis: CommunityThesis,
    research: dict[str, Any],
    objectives: ObjectiveMix | None = None,
) -> CommunityIdentity:
    objs = objectives or objective_mix(thesis, research)
    seg = thesis.primary_segment or {}
    role = str(seg.get("role") or "practitioners").strip()
    product = str(research.get("product_name") or "Product").strip()
    pains = [str(p) for p in (seg.get("pains") or [])]
    recurring = pains[0] if pains else f"get better at {role}"

    # Craft name from role
    role_token = role.split()[0] if role else "Builders"
    role_token = role_token[:1].upper() + role_token[1:]
    craft = next((n for n in _CRAFT_NOUNS if n not in _BANNED_DEFAULT), "Guild")
    if objs.archetype == "dev_tool":
        craft = "Lab"
    elif objs.archetype == "prosumer":
        craft = "Studio"
    name = f"{role_token} {craft}"
    rejected: list[str] = []
    product_community = f"{product} Community"
    if name.lower() == product_community.lower():
        rejected.append(name)
        name = f"{role_token} {craft}"
    if product.lower() in name.lower():
        rejected.append(name)
        name = f"{role_token} Guild"

    if objs.acquisition >= 20:
        anti = "lead-gen group"
    elif objs.product_adoption >= 35:
        anti = "support ticket queue"
    else:
        anti = "another Slack to mute"

    channels = [
        {"slug": "welcome", "purpose": "Orient + rules", "objective": "networking"},
        {"slug": "intros", "purpose": "Who you are + craft", "objective": "networking"},
        {"slug": "help", "purpose": "Peer + host help", "objective": "education"},
        {"slug": "product", "purpose": "Product scoped (SLA, not tickets)", "objective": "product_adoption"},
    ]
    if objs.education >= 30:
        channels.append({"slug": "playbooks", "purpose": "Share reusable playbooks", "objective": "education"})
    if objs.networking >= 30 and objs.archetype == "prosumer":
        channels.append({"slug": "collab", "purpose": "Member collabs", "objective": "networking"})
    if objs.product_adoption >= 30:
        channels.append({"slug": "ship", "purpose": "I used X to do Y", "objective": "product_adoption"})
    if objs.education >= 35 or objs.archetype == "dev_tool":
        channels.append({"slug": "office-hours", "purpose": "Live / async AMA", "objective": "education"})
    channels = channels[:7]

    roles = [
        {"slug": "founder", "purpose": "Host + approve"},
        {"slug": "community_host", "purpose": "Rituals + drafts (bot/agent)"},
        {"slug": "champion", "purpose": "Seed energy for 4 weeks"},
    ]

    archetypes = [
        {
            "name": role_token,
            "source_persona": role,
            "join_hook": f"Join peers who {recurring}",
            "first_win_72h": "Post one intro + one artifact in #help or #playbooks",
            "contribution_ask_d30": "Own one weekly prompt reply for champions",
        }
    ]

    rules = [
        "Be useful to a peer before pitching a product.",
        "No cold DMs. Opt-in / transactional only.",
        "Critique work, not people. Assume good intent.",
        "Draft-then-approve for staff/agent posts in week 1–2.",
        "Member messages are for serving members — not silent training data.",
    ]

    return CommunityIdentity(
        name=name,
        positioning=f"A home for {role}s who care about {recurring}",
        description=(
            f"{name} is hosted by {product} but named for the craft. "
            f"Primary objective: {objs.primary}."
        ),
        anti_position=anti,
        rules=rules,
        member_archetypes=archetypes,
        channels=channels,
        roles=roles,
        name_rejected=rejected,
    )
