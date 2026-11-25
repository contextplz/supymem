"""God decides the Now move. Not a fixed pipeline; stage + evidence + automation bias."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.gtm.god.brief import assumption_count
from app.gtm.god.catalog import AGENTS, FORBIDDEN_KINDS


@dataclass(frozen=True)
class Evidence:
    users_count: int = 0
    icp_ready: bool = False
    positioning_ready: bool = False
    site_url: str | None = None
    domain: str | None = None
    launched: bool = False
    launch_posts: int = 0
    seo_ready: bool = False
    mailbox: str | None = None
    gmail_connected: bool = False
    prospect_count: int = 0


@dataclass(frozen=True)
class SpawnSpec:
    kind: str
    horizon: str
    depends_on_kinds: tuple[str, ...] = ()
    human: bool = False


@dataclass(frozen=True)
class Diagnosis:
    bottleneck: str
    skill: str
    rationale: str
    now: list[SpawnSpec] = field(default_factory=list)
    next: list[SpawnSpec] = field(default_factory=list)
    later: list[SpawnSpec] = field(default_factory=list)
    llm_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        def dump(items: list[SpawnSpec]) -> list[dict[str, Any]]:
            return [
                {
                    "kind": i.kind,
                    "horizon": i.horizon,
                    "depends_on_kinds": list(i.depends_on_kinds),
                    "human": i.human,
                    "skill": AGENTS.get(i.kind, {}).get("skill"),
                    "owner": AGENTS.get(i.kind, {}).get("owner"),
                }
                for i in items
            ]

        return {
            "bottleneck": self.bottleneck,
            "skill": self.skill,
            "rationale": self.rationale,
            "llm_used": self.llm_used,
            "now": dump(self.now),
            "next": dump(self.next),
            "later": dump(self.later),
        }


def diagnose(brief: dict[str, Any], evidence: Evidence) -> Diagnosis:
    """Decide the single bottleneck, then spawn automations around it.

    Prefers spinning specialist agents over asking a human, except LinkedIn
    send (never), harvest (never), in-person talks, live social, and real
    registrar/DNS.
    """

    heavy = bool(brief.get("assumption_heavy")) or assumption_count(brief) >= 3
    users = int(brief.get("users_count") or evidence.users_count or 0)

    if users <= 0 and heavy and not evidence.icp_ready:
        return Diagnosis(
            bottleneck="Pain and ICP are still assumptions. Talking to people beats a clever homepage.",
            skill="talk-to-users",
            rationale=(
                "God chose field conversations as the Now move, and still spins "
                "ICP/competitor agents so research is not blocked on the founder."
            ),
            now=[
                SpawnSpec("talk_in_person", "now", human=True),
                SpawnSpec("research_icp", "now"),
                SpawnSpec("competitors", "now"),
                SpawnSpec("review", "now", depends_on_kinds=("research_icp", "competitors")),
            ],
            next=[
                SpawnSpec("positioning", "next", depends_on_kinds=("research_icp",)),
                SpawnSpec("brand_voice", "next", depends_on_kinds=("positioning",)),
            ],
            later=[
                SpawnSpec("site", "later", depends_on_kinds=("brand_voice",)),
                SpawnSpec("domain", "later", depends_on_kinds=("brand_voice",)),
            ],
        )

    if not evidence.icp_ready:
        return Diagnosis(
            bottleneck="No named ICP, so nothing else can be sharp.",
            skill="who-is-this-for",
            rationale="Automate ICP and competitor research first.",
            now=[
                SpawnSpec("research_icp", "now"),
                SpawnSpec("competitors", "now"),
                SpawnSpec("review", "now", depends_on_kinds=("research_icp", "competitors")),
            ],
            next=[
                SpawnSpec("positioning", "next", depends_on_kinds=("research_icp",)),
                SpawnSpec("brand_voice", "next", depends_on_kinds=("positioning",)),
            ],
            later=_highway_later(),
        )

    if not evidence.positioning_ready:
        return Diagnosis(
            bottleneck="ICP exists but the story is generic.",
            skill="positioning-and-story",
            rationale="Positioning and brand voice before a site, so the page is not filler.",
            now=[
                SpawnSpec("positioning", "now"),
                SpawnSpec("brand_voice", "now", depends_on_kinds=("positioning",)),
                SpawnSpec("review", "now", depends_on_kinds=("brand_voice",)),
            ],
            next=[
                SpawnSpec("site", "next", depends_on_kinds=("brand_voice",)),
                SpawnSpec("domain", "next", depends_on_kinds=("brand_voice",)),
            ],
            later=_launch_later(),
        )

    if not evidence.site_url or not evidence.domain:
        deps: tuple[str, ...] = ()
        now = []
        if not evidence.site_url:
            now.append(SpawnSpec("site", "now", depends_on_kinds=deps))
        if not evidence.domain:
            now.append(SpawnSpec("domain", "now", depends_on_kinds=deps))
        now.append(SpawnSpec("review", "now", depends_on_kinds=("site", "domain")))
        return Diagnosis(
            bottleneck="Need a demo site and a reserved domain at the same time.",
            skill="the-homepage",
            rationale="God runs site compose and domain reservation in parallel.",
            now=now,
            next=[
                SpawnSpec("launch_site", "next", depends_on_kinds=("site", "domain")),
                SpawnSpec("launch_posts", "next", depends_on_kinds=("site",)),
            ],
            later=_after_launch(),
        )

    if not evidence.launched or evidence.launch_posts <= 0:
        now = []
        if not evidence.launched:
            now.append(SpawnSpec("launch_site", "now"))
        if evidence.launch_posts <= 0:
            now.append(SpawnSpec("launch_posts", "now"))
        return Diagnosis(
            bottleneck="The site exists but nobody has been told.",
            skill="launch-it",
            rationale="Launch the page and draft social posts in the same tick. Founder posts LinkedIn themselves.",
            now=now,
            next=[
                SpawnSpec("seo", "next"),
                SpawnSpec("mailbox", "next"),
            ],
            later=[
                SpawnSpec("prospects", "later", depends_on_kinds=("mailbox",), human=True),
                SpawnSpec("outreach_drafts", "later", depends_on_kinds=("prospects",)),
            ],
        )

    if not evidence.seo_ready or not evidence.mailbox:
        now = []
        if not evidence.seo_ready:
            now.append(SpawnSpec("seo", "now"))
        if not evidence.mailbox:
            now.append(SpawnSpec("mailbox", "now"))
        return Diagnosis(
            bottleneck="Launched, but discovery and company mail are not set.",
            skill="founder-led-content",
            rationale="SEO automation plus a local mailbox. Real DNS stays a human step.",
            now=now,
            next=[
                SpawnSpec("prospects", "next", depends_on_kinds=("mailbox",), human=True),
            ],
            later=[
                SpawnSpec("outreach_drafts", "later", depends_on_kinds=("prospects",)),
            ],
        )

    if evidence.prospect_count <= 0:
        return Diagnosis(
            bottleneck="No first-party prospects. We will not invent names.",
            skill="first-50-users",
            rationale="Instruct the founder to paste evidence or connect Gmail/Slack. Harvest is forbidden.",
            now=[SpawnSpec("prospects", "now", human=True)],
            next=[SpawnSpec("outreach_drafts", "next", depends_on_kinds=("prospects",))],
            later=[],
        )

    return Diagnosis(
        bottleneck="Prospects exist. Drafts go to the human clipboard. LinkedIn is never sent by software.",
        skill="founder-led-sales",
        rationale="Draft agent prepares notes. Founder copies and sends. Gmail sequences only if their mailbox is connected.",
        now=[SpawnSpec("outreach_drafts", "now")],
        next=[],
        later=[],
    )


def assert_spawn_legal(kind: str) -> None:
    if kind in FORBIDDEN_KINDS:
        raise ValueError("forbidden_kind")


def _highway_later() -> list[SpawnSpec]:
    return [
        SpawnSpec("site", "later", depends_on_kinds=("brand_voice",)),
        SpawnSpec("domain", "later", depends_on_kinds=("brand_voice",)),
        SpawnSpec("launch_site", "later", depends_on_kinds=("site", "domain")),
        SpawnSpec("launch_posts", "later", depends_on_kinds=("site",)),
    ]


def _launch_later() -> list[SpawnSpec]:
    return [
        SpawnSpec("launch_site", "later", depends_on_kinds=("site", "domain")),
        SpawnSpec("launch_posts", "later", depends_on_kinds=("site",)),
        SpawnSpec("seo", "later"),
        SpawnSpec("mailbox", "later"),
    ]


def _after_launch() -> list[SpawnSpec]:
    return [
        SpawnSpec("seo", "later"),
        SpawnSpec("mailbox", "later"),
        SpawnSpec("prospects", "later", human=True),
        SpawnSpec("outreach_drafts", "later", depends_on_kinds=("prospects",)),
    ]


def diagnose_live(brief: dict[str, Any], evidence: Evidence, *, workspace_id: str | None = None) -> Diagnosis:
    """Deterministic diagnosis, optionally rewritten by the live model."""
    fallback = diagnose(brief, evidence)
    from app.gtm.god.llm import json_chat

    allowed = sorted(k for k in AGENTS if k not in FORBIDDEN_KINDS)
    payload = json_chat(
        purpose="gtm.god_diagnose",
        workspace_id=workspace_id,
        system=(
            "You are GTM God for a developer-tool company. You decide the bottleneck "
            "from the idea, not a rigid checklist. Prefer spinning automation agents. "
            "Never harvest leads, scrape LinkedIn, or send LinkedIn. Return JSON only."
        ),
        user=(
            "Brief:\n"
            f"{brief}\n\n"
            "Evidence:\n"
            f"{_evidence_blob(evidence)}\n\n"
            f"Allowed specialist kinds: {allowed}\n"
            "Forbidden: harvest_leads, discover_leads, linkedin_send, linkedin_scrape, unipile_send.\n"
            "Return JSON with keys: bottleneck (string), skill (string), rationale (string), "
            "now/next/later (arrays of {kind, horizon, depends_on_kinds, human}). "
            "horizon must be now, next, or later. Prefer automations. Include talk_in_person "
            "in now when users_count is 0 and the ICP is still an assumption."
        ),
    )
    if not payload:
        return fallback
    bottleneck = str(payload.get("bottleneck") or fallback.bottleneck)[:400]
    skill = str(payload.get("skill") or fallback.skill)[:80]
    rationale = str(payload.get("rationale") or fallback.rationale)[:800]
    # Keep the executable highway. The model names the bottleneck; it cannot
    # drop automations or introduce harvest/LinkedIn send.
    return Diagnosis(
        bottleneck=bottleneck,
        skill=skill,
        rationale=rationale,
        now=fallback.now,
        next=fallback.next,
        later=fallback.later,
        llm_used=True,
    )


def _evidence_blob(evidence: Evidence) -> dict[str, Any]:
    return {
        "users_count": evidence.users_count,
        "icp_ready": evidence.icp_ready,
        "positioning_ready": evidence.positioning_ready,
        "site_url": evidence.site_url,
        "domain": evidence.domain,
        "launched": evidence.launched,
        "launch_posts": evidence.launch_posts,
        "seo_ready": evidence.seo_ready,
        "mailbox": evidence.mailbox,
        "gmail_connected": evidence.gmail_connected,
        "prospect_count": evidence.prospect_count,
    }


def _specs(raw: Any, *, default_horizon: str) -> list[SpawnSpec]:
    if not isinstance(raw, list):
        return []
    out: list[SpawnSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind in FORBIDDEN_KINDS or kind not in AGENTS or kind in seen:
            continue
        horizon = str(item.get("horizon") or default_horizon).strip()
        if horizon not in {"now", "next", "later"}:
            horizon = default_horizon
        deps_raw = item.get("depends_on_kinds") or []
        deps = tuple(
            str(d)
            for d in deps_raw
            if str(d) in AGENTS and str(d) not in FORBIDDEN_KINDS
        )
        out.append(
            SpawnSpec(
                kind=kind,
                horizon=horizon,
                depends_on_kinds=deps,
                human=bool(item.get("human") or AGENTS[kind].get("mode") == "human"),
            )
        )
        seen.add(kind)
    return out
