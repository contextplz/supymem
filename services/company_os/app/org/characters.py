"""Named employees for the venture — first names, titles, and desk charges.

Each role maps to a person the founder can @mention. Duplicate roles get a
spare first name so two builders are never both "Cole".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MENTION = re.compile(r"@([A-Za-z][\w.-]*)")
_SPEAKER_MARK = re.compile(
    r"^<!--employee:(?P<name>[^:]+):(?P<title>[^:]*):(?P<id>[^>]*)-->\s*",
    re.I,
)

# Given name, job title, what they own, how they talk.
_CAST: dict[str, tuple[str, str, str, str]] = {
    "orchestrator": (
        "Ren",
        "Team lead",
        "Keep the team on one plan and surface what needs you.",
        "Direct. Speaks for the team when nobody else is named.",
    ),
    "architect": (
        "Priya",
        "Architect",
        "Lock the technical shape before we overbuild.",
        "Precise. Pushes back on vague scope.",
    ),
    "builder": (
        "Cole",
        "Builder",
        "Ship the product and the first-look site.",
        "Practical. Talks in shipping terms.",
    ),
    "reviewer": (
        "Jonah",
        "Reviewer",
        "Catch defects before they reach users.",
        "Skeptical. Asks for evidence.",
    ),
    "qa": (
        "Ade",
        "QA",
        "Prove the product actually works.",
        "Blunt about breakage.",
    ),
    "designer": (
        "Lina",
        "Designer",
        "Make the product look like a real company.",
        "Visual. Hates generic copy-paste UI.",
    ),
    "copywriter": (
        "Theo",
        "Copywriter",
        "Write landing copy people will actually read.",
        "Short sentences. No fluff.",
    ),
    "growth": (
        "Nia",
        "Growth",
        "Find demand and turn it into signups.",
        "Numbers first, then the story.",
    ),
    "channel_operator": (
        "Omar",
        "Channel op",
        "Publish on the channels we actually armed.",
        "Operational. Asks what to post next.",
    ),
    "support": (
        "Eva",
        "Support",
        "Answer inbound and file what users actually said.",
        "Warm, then specific.",
    ),
    "analyst": (
        "Kai",
        "Analyst",
        "Keep metrics honest and fresh.",
        "Calm. Flags missing data instead of inventing it.",
    ),
    "payments_specialist": (
        "Maren",
        "Payments",
        "Make charging money actually work.",
        "Careful. Won't pretend Stripe is live if it isn't.",
    ),
    "compliance": (
        "Sasha",
        "Compliance",
        "Keep us on the right side of policy.",
        "Won't rubber-stamp risk.",
    ),
    "integrator": (
        "Dev",
        "Integrator",
        "Connect the tools this company actually uses.",
        "Concrete. Lists what's wired vs missing.",
    ),
    "maintainer": (
        "Quinn",
        "Maintainer",
        "Keep the live company from rotting.",
        "Quiet unless something's on fire.",
    ),
    "pricing": (
        "Ivy",
        "Pricing",
        "Test what people will pay.",
        "Curious. Hates vanity metrics.",
    ),
    "researcher": (
        "Maya",
        "Researcher",
        "Find demand, competitors, and whether this is worth building.",
        "Sourced. Cites what we actually looked up.",
    ),
    "research": (
        "Maya",
        "Researcher",
        "Find demand, competitors, and whether this is worth building.",
        "Sourced. Cites what we actually looked up.",
    ),
    "founder_concierge": (
        "Samira",
        "Concierge",
        "Keep you unblocked and tell you the next useful move.",
        "Helpful, not ceremonial.",
    ),
    "concierge": (
        "Samira",
        "Concierge",
        "Keep you unblocked and tell you the next useful move.",
        "Helpful, not ceremonial.",
    ),
    "waitlist_ops": (
        "Bo",
        "Waitlist ops",
        "Capture interest and keep the list honest.",
        "Counts people, not vibes.",
    ),
    "ops": (
        "Bo",
        "Ops",
        "Keep the operational loop moving.",
        "Counts people, not vibes.",
    ),
    "coding_dispatcher": (
        "Ellis",
        "Coding dispatcher",
        "Hand real build work to the local coding agents.",
        "Talks in work orders.",
    ),
    "engineering": (
        "Ellis",
        "Engineer",
        "Turn the plan into working software.",
        "Talks in work orders.",
    ),
    "outreach": (
        "Nia",
        "Outreach",
        "Talk to the people this product is for.",
        "Numbers first, then the story.",
    ),
    "relationship_manager": (
        "Nia",
        "Relationship manager",
        "Turn warm signals into real conversations and customers.",
        "Congrats first. Pitch last.",
    ),
    "signal_scout": (
        "Nia",
        "Signal scout",
        "Find who just became worth talking to.",
        "Evidence before enthusiasm.",
    ),
    "lead_researcher": (
        "Maya",
        "Lead researcher",
        "Explain why this person, why this company, why now.",
        "One concrete hook or nothing.",
    ),
    "gtm_analyst": (
        "Kai",
        "GTM analyst",
        "Keep the acquisition funnel honest.",
        "Calm. Flags missing data instead of inventing it.",
    ),
    "sales": (
        "Nia",
        "Outreach",
        "Talk to the people this product is for.",
        "Numbers first, then the story.",
    ),
    "build": (
        "Cole",
        "Builder",
        "Ship the product and the first-look site.",
        "Practical. Talks in shipping terms.",
    ),
    "review": (
        "Jonah",
        "Reviewer",
        "Catch defects before they reach users.",
        "Skeptical. Asks for evidence.",
    ),
}

_SPARES = ("Alex", "Jordan", "Riley", "Casey", "Morgan", "Avery", "Drew", "Sky")

# Pack roles (research, concierge, build, …) and catalog roles (researcher, …)
# collapse to one seat so Maya is not hired twice.
_ROLE_CANON: dict[str, str] = {
    "research": "researcher",
    "researcher": "researcher",
    "concierge": "founder_concierge",
    "founder_concierge": "founder_concierge",
    "build": "builder",
    "builder": "builder",
    "ops": "waitlist_ops",
    "waitlist_ops": "waitlist_ops",
    "review": "reviewer",
    "reviewer": "reviewer",
    "engineering": "coding_dispatcher",
    "coding_dispatcher": "coding_dispatcher",
    "sales": "outreach",
    "outreach": "outreach",
    "relationship_manager": "relationship_manager",
    "signal_scout": "signal_scout",
    "lead_researcher": "lead_researcher",
    "gtm_analyst": "gtm_analyst",
    "support": "support",
    "orchestrator": "orchestrator",
    "qa": "qa",
    "growth": "growth",
    "pricing": "pricing",
}

_WORK_ROLE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("research", "competitor", "market", "demand", "dossier", "interview"), "researcher"),
    (("landing", "site", "build", "ship", "product", "page"), "builder"),
    (("waitlist", "signup", "lead"), "waitlist_ops"),
    (("price", "pricing", "paid", "offer"), "pricing"),
    (("email", "outreach", "linkedin", "cold"), "outreach"),
    (("review", "qa", "test", "gate"), "reviewer"),
    (("code", "repo", "dispatch", "work order", "engineering"), "coding_dispatcher"),
    (("support", "inbound"), "support"),
)


@dataclass(frozen=True)
class Character:
    given_name: str
    title: str
    charge: str
    voice: str
    role: str


def _norm_role(role: str | None) -> str:
    return (role or "specialist").strip().lower().replace("-", "_").replace(" ", "_")


def canonical_role(role: str | None) -> str:
    key = _norm_role(role)
    return _ROLE_CANON.get(key, key)


def _looks_like_job_title(name: str | None, role: str) -> bool:
    raw = (name or "").strip()
    if not raw:
        return True
    n = raw.lower().replace("_", " ")
    r = role.replace("_", " ")
    if n == r or n.startswith(r + " "):
        return True
    needles = (
        "engineer",
        "researcher",
        "concierge",
        "builder",
        "reviewer",
        "dispatcher",
        "orchestrator",
        "specialist",
        "operator",
        "analyst",
        "copywriter",
    )
    return any(word in n for word in needles)


def character_for(role: str | None, agent_id: str | None = None, existing_name: str | None = None) -> Character:
    key = _norm_role(role)
    base = _CAST.get(key) or (
        "Ari",
        key.replace("_", " ").title() or "Teammate",
        "Help this venture ship.",
        "Plainspoken.",
    )
    given, title, charge, voice = base
    if existing_name and not _looks_like_job_title(existing_name, key):
        given = existing_name.split()[0]
    elif agent_id:
        # Stable spare name when the same role is hired more than once.
        digest = sum(ord(ch) for ch in str(agent_id))
        if digest % 5 == 0 and key in {"builder", "channel_operator", "integrator"}:
            given = _SPARES[digest % len(_SPARES)]
    return Character(given_name=given, title=title, charge=charge, voice=voice, role=key)


def enrich_agent(agent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(agent, dict):
        return agent
    key = _norm_role(str(agent.get("role") or ""))
    person = character_for(agent.get("role"), str(agent.get("id") or ""), agent.get("name"))
    out = dict(agent)
    out["name"] = person.given_name
    stored_title = str(agent.get("title") or "")
    out["title"] = (
        person.title
        if _looks_like_job_title(stored_title, key)
        else (stored_title or person.title)
    )
    out["charge"] = agent.get("charge") or person.charge
    out["voice"] = person.voice
    return out


def enrich_org(org: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(org or {})
    agents = [enrich_agent(a) for a in (data.get("agents") or [])]
    data["agents"] = agents
    data["agent_count"] = len(agents)
    return data


def _mention_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def match_mention(text: str, agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Route @Maya, @Research, @Design — exact first, then a 2+ char prefix."""
    hit = _MENTION.search(text or "")
    if not hit or not agents:
        return None
    needle = _mention_token(hit.group(1))
    if len(needle) < 2:
        return None
    exact: list[dict[str, Any]] = []
    prefix: list[dict[str, Any]] = []
    for agent in agents:
        name = _mention_token(str(agent.get("name") or ""))
        role = _mention_token(_norm_role(str(agent.get("role") or "")))
        title = _mention_token(str(agent.get("title") or ""))
        aliases = {token for token in (name, role, title) if token}
        if needle in aliases:
            exact.append(agent)
            continue
        if any(token.startswith(needle) or needle.startswith(token) for token in aliases):
            prefix.append(agent)
    return (exact or prefix or [None])[0]


def default_speaker(agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not agents:
        person = character_for("orchestrator", "office-lead")
        return {
            "id": "office-lead",
            "name": person.given_name,
            "title": person.title,
            "role": "orchestrator",
            "charge": person.charge,
            "status": "active",
        }
    for preferred in ("orchestrator", "founder_concierge", "concierge"):
        for agent in agents:
            if _norm_role(str(agent.get("role") or "")) == preferred:
                return agent
    working = [a for a in agents if str(a.get("status") or "").lower() in {"active", "running"}]
    return (working or agents)[0]


def owner_for_work(
    title: str,
    tags: list[Any] | None,
    agents: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Pick the named employee who should own this piece of work."""
    rows = [a for a in (agents or []) if isinstance(a, dict)]
    blob = f"{title} {' '.join(str(t) for t in (tags or []))}".lower()
    wanted = "orchestrator"
    for needles, role in _WORK_ROLE_HINTS:
        if any(needle in blob for needle in needles):
            wanted = role
            break
    target = canonical_role(wanted)
    for agent in rows:
        if canonical_role(str(agent.get("role") or "")) == target:
            return enrich_agent(agent)
    speaker = default_speaker(rows)
    return enrich_agent(speaker) if speaker else None


def encode_assignee(agent: dict[str, Any] | None) -> str | None:
    if not agent:
        return None
    name = str(agent.get("name") or "").replace(":", " ")
    role = str(agent.get("role") or "").replace(":", " ")
    agent_id = str(agent.get("id") or "").replace(":", " ")
    return f"assignee:{name}:{role}:{agent_id}"


def decode_assignee(tags: list[Any] | None) -> dict[str, str]:
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.startswith("assignee:"):
            continue
        parts = tag.split(":", 3)
        return {
            "owner_name": parts[1] if len(parts) > 1 else "",
            "owner_role": parts[2] if len(parts) > 2 else "",
            "owner_id": parts[3] if len(parts) > 3 else "",
        }
    return {}


def strip_assignee_tags(tags: list[Any] | None) -> list[str]:
    return [
        str(tag)
        for tag in (tags or [])
        if not (isinstance(tag, str) and tag.startswith("assignee:"))
    ]


def founder_report(body: str, manager: dict[str, Any] | None) -> str:
    from app.org.manager import DEFAULT_MANAGER_NAME

    names = {DEFAULT_MANAGER_NAME, "manager", "the manager"}
    if manager and (manager.get("id") == "manager" or manager.get("role") == "manager") and manager.get("name"):
        names.add(str(manager["name"]))
    _, prose = parse_speaker(body)
    # ponytail: leading internal salutations only; structured report recipients for embedded salutations.
    greeting = re.match(r"^\s*(?:(?:hi|hello|hey|dear|to)\s+)?(?:" + "|".join(re.escape(name) for name in names)
                        + r")[,:!]\s*", prose, re.I)
    if not greeting:
        return body
    prefix = body[:len(body) - len(prose)]
    return prefix + "Here is the report for you:\n\n" + prose[greeting.end():]


def stamp_speaker(body: str, agent: dict[str, Any] | None, *, preserve_exact: bool = False) -> str:
    if not preserve_exact:
        body = founder_report(body or "", agent)
    # Normalize em/en dashes on every stamped chat reply (assistant + handlers).
    try:
        from app.chat.assistant import normalize_assistant_dashes

        body = (body or "") if preserve_exact else normalize_assistant_dashes(body or "")
    except Exception:  # noqa: BLE001
        body = body or ""
    if not agent:
        return body
    name = str(agent.get("name") or "Teammate")
    title = str(agent.get("title") or agent.get("role") or "teammate")
    agent_id = str(agent.get("id") or "")
    mark = f"<!--employee:{name}:{title}:{agent_id}-->"
    if (body or "").lstrip().startswith("<!--employee:"):
        return body
    return f"{mark}\n{body}"


def parse_speaker(body: str | None) -> tuple[dict[str, str] | None, str]:
    text = body or ""
    match = _SPEAKER_MARK.match(text)
    if not match:
        return None, text
    return (
        {
            "name": match.group("name"),
            "title": match.group("title"),
            "id": match.group("id"),
        },
        text[match.end() :],
    )
