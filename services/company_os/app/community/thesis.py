"""Heuristic community thesis evaluator — gates + scored dimensions."""

from __future__ import annotations

import re
from typing import Any

from app.community.plan import (
    CommunityThesis,
    CommunityVerdict,
    DimensionScore,
    GateResult,
    research_from_payload,
)

_WEEKLY = re.compile(
    r"\b(weekly|daily|every\s+day|every\s+week|pipeline|forecast|debug|oncall|"
    r"client|standup|ritual|recurring|cadence|monday|sprint)\b",
    re.I,
)
_PEER = re.compile(
    r"\b(peer|playbook|nobody to ask|what do (others|peers)|war stor|"
    r"template|how do you|community|slack|discord)\b",
    re.I,
)
_IDENTITY = re.compile(
    r"\b(ops|engineer|sre|revops|founder|indie|creator|designer|dev|"
    r"photographer|marketer|growth|cs lead|staff)\b",
    re.I,
)
_VAGUE_ROLE = re.compile(
    r"\b(decision.?maker|smb|enterprise|anyone|users?|customers?|mid.?market)\b",
    re.I,
)

_DIMS = (
    ("D1", "Recurring problem", 0.15),
    ("D2", "Definable group", 0.12),
    ("D3", "Identity strength", 0.15),
    ("D4", "Edu/tech depth", 0.12),
    ("D5", "Peer-to-peer value", 0.15),
    ("D6", "Audience potential", 0.08),
    ("D7", "Seed density", 0.13),
    ("D8", "Gathering gap", 0.10),
)

# First failed gate wins for fail_action priority
_GATE_PRIORITY = ("G6", "G1", "G2", "G7", "G3", "G4", "G5")


def _primary_segment(bundle: dict[str, Any]) -> dict[str, Any]:
    segs = [s for s in (bundle.get("icp_segments") or []) if isinstance(s, dict)]
    if not segs:
        return {
            "name": "unknown",
            "role": "",
            "pains": [],
            "watering_holes": [],
            "objections": [],
            "triggers": [],
            "industry": "",
            "company_size": "",
            "technical_depth": None,
            "buying_committee": False,
            "self_serve": False,
        }

    def richness(s: dict[str, Any]) -> int:
        holes = s.get("watering_holes") or []
        role = str(s.get("role") or s.get("name") or "")
        return len(holes) * 10 + len(role.split())

    return max(segs, key=richness)


def _pains(seg: dict[str, Any]) -> list[str]:
    raw = seg.get("pains") or []
    return [str(p) for p in raw if str(p).strip()]


def _seed_proxy(bundle: dict[str, Any]) -> float:
    customers = float(bundle.get("paying_customers") or 0)
    mau = float(bundle.get("monthly_active_users") or 0)
    audience = float(bundle.get("content_audience") or 0)
    return max(customers, mau * 0.05, audience * 0.02)


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def evaluate_thesis(payload: dict[str, Any] | None = None) -> CommunityThesis:
    bundle = research_from_payload(payload)
    seg = _primary_segment(bundle)
    pains = _pains(seg)
    role = str(seg.get("role") or seg.get("name") or "").strip()
    holes = [str(h) for h in (seg.get("watering_holes") or []) if str(h).strip()]
    depth = str(seg.get("technical_depth") or "").lower() or None
    hours = bundle.get("founder_hours_per_week")
    if hours is None:
        hours = 3
    pmf = str(bundle.get("pmf_signal") or "").lower() or None
    core_jobs = int(bundle.get("core_job_completions") or 0)
    seed = _seed_proxy(bundle)
    invite25 = bool(bundle.get("founder_will_invite_25"))
    buying = bool(seg.get("buying_committee"))

    weekly_pains = [p for p in pains if _WEEKLY.search(p)]
    peer_pains = [p for p in pains if _PEER.search(p)]

    # --- Gates ---
    gates: list[GateResult] = []

    g1_pass = len(weekly_pains) >= 2 or (len(pains) >= 2 and len(weekly_pains) >= 1)
    if len(pains) >= 2 and all(_WEEKLY.search(p) for p in pains[:2]):
        g1_pass = True
    # Softer: ≥2 pains and at least one weekly-ish
    if len(pains) >= 2 and weekly_pains:
        g1_pass = True
    elif len(pains) >= 2:
        # still fail if none look recurring
        g1_pass = False
    else:
        g1_pass = False
    gates.append(
        GateResult(
            id="G1",
            label="Recurring problem",
            passed=g1_pass,
            fail_action=CommunityVerdict.DO_NOT_BUILD,
            evidence=f"weekly_ish_pains={len(weekly_pains)} total_pains={len(pains)}",
        )
    )

    role_words = _word_count(role)
    vague = bool(_VAGUE_ROLE.search(role)) and not _IDENTITY.search(role)
    g2_pass = bool(role) and role_words <= 6 and not vague and not (
        buying and not _IDENTITY.search(role)
    )
    gates.append(
        GateResult(
            id="G2",
            label="Named tribe",
            passed=g2_pass,
            fail_action=(
                CommunityVerdict.CAB_NOT_COMMUNITY
                if buying
                else CommunityVerdict.DO_NOT_BUILD
            ),
            evidence=f"role={role!r} words={role_words} vague={vague}",
        )
    )

    g3_pass = seed >= 25 or invite25
    gates.append(
        GateResult(
            id="G3",
            label="Seed density",
            passed=g3_pass,
            fail_action=CommunityVerdict.PRE_COMMUNITY,
            evidence=f"seed_proxy={seed:.1f} invite25={invite25}",
        )
    )

    g4_pass = int(hours) >= 5
    gates.append(
        GateResult(
            id="G4",
            label="Founder time",
            passed=g4_pass,
            fail_action=CommunityVerdict.PARTICIPATE_EXISTING,
            evidence=f"founder_hours_per_week={hours}",
        )
    )

    competitor_owns = False
    for c in bundle.get("competitors") or []:
        if not isinstance(c, dict):
            continue
        strengths = " ".join(str(x) for x in (c.get("strengths") or [])).lower()
        name = str(c.get("name") or "").lower()
        if "community" in strengths or "discord" in strengths:
            # Incumbent community signal
            if any("discord" in h.lower() and any(ch.isdigit() for ch in h) for h in holes):
                competitor_owns = True
            if "10k" in strengths or "10000" in strengths or "owned community" in strengths:
                competitor_owns = True
        if "discord" in name and "community" in strengths:
            competitor_owns = True
    # Also fail if a single watering hole is an incumbent mega-Discord
    for h in holes:
        hl = h.lower()
        if "discord" in hl and any(x in hl for x in ("10k", "10000", "50k", "100k")):
            competitor_owns = True
    g5_pass = not competitor_owns
    gates.append(
        GateResult(
            id="G5",
            label="Competitor occupancy",
            passed=g5_pass,
            fail_action=CommunityVerdict.PARTICIPATE_EXISTING,
            evidence=f"competitor_owns={competitor_owns} holes={holes[:3]}",
        )
    )

    g6_pass = (pmf is not None and pmf != "none") or core_jobs >= 10 or seed >= 25
    # Explicit none fails even with seed unless core jobs
    if pmf == "none" and core_jobs < 10:
        g6_pass = False
    gates.append(
        GateResult(
            id="G6",
            label="PMF floor",
            passed=g6_pass,
            fail_action=CommunityVerdict.DO_NOT_BUILD,
            evidence=f"pmf={pmf!r} core_job_completions={core_jobs} seed={seed:.1f}",
        )
    )

    g7_pass = bool(peer_pains) or any(
        "playbook" in p.lower() or "peer" in p.lower() or "template" in p.lower()
        for p in pains
    )
    gates.append(
        GateResult(
            id="G7",
            label="Peer-to-peer value",
            passed=g7_pass,
            fail_action=CommunityVerdict.CAB_NOT_COMMUNITY,
            evidence=f"peer_pains={len(peer_pains)}",
        )
    )

    # --- Dimensions 0-5 ---
    dims: list[DimensionScore] = []

    d1 = 0
    if weekly_pains:
        d1 = 3
    if len(weekly_pains) >= 2:
        d1 = 4
    if any(_WEEKLY.search(p) and "daily" in p.lower() for p in pains):
        d1 = 5
    if not pains:
        d1 = 0
    dims.append(
        DimensionScore(
            "D1", "Recurring problem", 0.15, d1,
            evidence=weekly_pains[:3] or pains[:2],
            research_paths=["icp_segments.pains"],
        )
    )

    d2 = 0
    if role and not vague:
        d2 = 3
    if role and seg.get("industry") and not vague:
        d2 = 4
    if role and seg.get("industry") and (seg.get("company_size") or depth):
        d2 = 5
    if vague or not role:
        d2 = 1 if role else 0
    dims.append(
        DimensionScore(
            "D2", "Definable group", 0.12, d2,
            evidence=[role or "(missing)", str(seg.get("industry") or "")],
            research_paths=["icp_segments.role"],
        )
    )

    labels: list[str] = []
    for p in bundle.get("personas") or []:
        if isinstance(p, dict):
            labels.extend(str(x) for x in (p.get("identity_labels") or []))
    d3 = 1 if role else 0
    if _IDENTITY.search(role):
        d3 = 3
    if labels or any(_IDENTITY.search(h) for h in holes):
        d3 = 4
    if labels and _IDENTITY.search(role):
        d3 = 5
    dims.append(
        DimensionScore(
            "D3", "Identity strength", 0.15, d3,
            evidence=labels[:3] or [role],
            research_paths=["personas.identity_labels", "icp_segments.role"],
        )
    )

    d4 = {"low": 1, "med": 3, "medium": 3, "high": 5}.get(depth or "", 2 if bundle.get("features") else 0)
    if any("hard" in str(o).lower() or "complex" in str(o).lower() for o in (seg.get("objections") or [])):
        d4 = max(d4, 4)
    dims.append(
        DimensionScore(
            "D4", "Edu/tech depth", 0.12, d4,
            evidence=[f"technical_depth={depth}"],
            research_paths=["icp_segments.technical_depth"],
        )
    )

    d5 = 1 if pains else 0
    if peer_pains:
        d5 = 4
    if len(peer_pains) >= 2:
        d5 = 5
    elif g7_pass and d5 < 3:
        d5 = 3
    dims.append(
        DimensionScore(
            "D5", "Peer-to-peer value", 0.15, d5,
            evidence=peer_pains[:3] or pains[:2],
            research_paths=["icp_segments.pains"],
        )
    )

    # Audience: prefer mid tribe; mass consumer / tiny both low
    d6 = 3
    if seed < 5 and not invite25:
        d6 = 1
    elif seed >= 200 or float(bundle.get("content_audience") or 0) >= 1000:
        d6 = 4
    if vague or role.lower() in {"users", "customers", "smb"}:
        d6 = 1
    dims.append(
        DimensionScore(
            "D6", "Audience potential", 0.08, d6,
            evidence=[f"seed_proxy={seed:.1f}"],
            research_paths=["paying_customers", "content_audience"],
        )
    )

    d7 = 0
    if seed >= 1:
        d7 = 1
    if seed >= 25:
        d7 = 3
    if seed >= 100:
        d7 = 4
    if seed >= 200 or float(bundle.get("content_audience") or 0) >= 1000:
        d7 = 5
    dims.append(
        DimensionScore(
            "D7", "Seed density", 0.13, d7,
            evidence=[f"seed_proxy={seed:.1f}"],
            research_paths=["paying_customers", "monthly_active_users", "content_audience"],
        )
    )

    d8 = 2
    if holes and g5_pass:
        d8 = 4
    if holes and not competitor_owns:
        d8 = 3
    if holes and g5_pass and not any("discord" in h.lower() for h in holes):
        d8 = 4  # fragmented LI/Reddit — gathering gap
    if competitor_owns:
        d8 = 0
    if not holes:
        d8 = 2
    dims.append(
        DimensionScore(
            "D8", "Gathering gap", 0.10, d8,
            evidence=holes[:3] or ["(no watering holes)"],
            research_paths=["icp_segments.watering_holes"],
        )
    )

    raw = sum((d.score / 5.0) * d.weight for d in dims)
    thesis_score = int(round(raw * 100))

    failed = {g.id: g for g in gates if not g.passed}
    verdict: str
    motion: str
    if failed:
        first = next((gid for gid in _GATE_PRIORITY if gid in failed), next(iter(failed)))
        verdict = failed[first].fail_action or CommunityVerdict.DO_NOT_BUILD
    elif thesis_score >= 70:
        verdict = CommunityVerdict.BUILD_OWNED
    elif thesis_score >= 55:
        verdict = CommunityVerdict.PRE_COMMUNITY
    elif thesis_score >= 40:
        verdict = CommunityVerdict.PARTICIPATE_EXISTING
    else:
        verdict = CommunityVerdict.DO_NOT_BUILD

    # Motion picker
    dim_map = {d.id: d.score for d in dims}
    if verdict == CommunityVerdict.BUILD_OWNED:
        motion = "invite_only_founding_50"
    elif dim_map.get("D4", 0) >= 4 and dim_map.get("D7", 0) < 3:
        motion = "founder_office_hours"
    elif dim_map.get("D3", 0) >= 3 and holes:
        motion = "watering_hole_residency"
    elif 8 <= int(bundle.get("paying_customers") or 0) <= 40:
        motion = "customer_slack_cab"
    elif verdict == CommunityVerdict.CAB_NOT_COMMUNITY:
        motion = "customer_slack_cab"
    else:
        motion = "founder_led_content"

    blocking = [d.id for d in dims if d.score < 3]
    top = sorted(dims, key=lambda d: d.score * d.weight, reverse=True)[:3]
    if verdict == CommunityVerdict.BUILD_OWNED:
        because = (
            f"Build because {', '.join(t.label for t in top)}. "
            f"Seed≈{seed:.0f}. Mode=invite-only founding 50."
        )
        rescore = 90
    else:
        because = (
            f"Don't build yet ({verdict}). Do {motion} for 60 days, re-score. "
            f"Blocking dims: {', '.join(blocking) or 'none'}. Score={thesis_score}."
        )
        rescore = 60

    return CommunityThesis(
        thesis_score=thesis_score,
        verdict=verdict,
        because=because,
        gates=gates,
        dimensions=dims,
        blocking_dimension_ids=blocking,
        motion=motion,
        rescore_after_days=rescore,
        primary_segment={
            "name": seg.get("name"),
            "role": role,
            "pains": pains[:5],
            "watering_holes": holes[:5],
            "technical_depth": depth,
            "buying_committee": buying,
            "self_serve": bool(seg.get("self_serve")),
        },
    )


def sample_build_research() -> dict[str, Any]:
    """Fixture that should clear gates and score ≥70."""
    return {
        "product_name": "ForecastForge",
        "one_liner": "RevOps forecast hygiene for B2B SaaS",
        "category": "b2b_saas",
        "paying_customers": 80,
        "monthly_active_users": 200,
        "content_audience": 1500,
        "founder_hours_per_week": 8,
        "pmf_signal": "early",
        "features": ["pipeline review", "forecast snapshot", "tool sprawl map"],
        "icp_segments": [
            {
                "name": "RevOps leads",
                "role": "RevOps lead",
                "industry": "B2B SaaS",
                "company_size": "21-200",
                "technical_depth": "med",
                "self_serve": True,
                "pains": [
                    "weekly forecast miss after Monday pipeline review",
                    "daily tool sprawl across CRM and sheets",
                    "nobody to ask for peer playbooks on forecast hygiene",
                ],
                "triggers": ["quarter close", "new CROs"],
                "objections": ["too hard to migrate"],
                "watering_holes": ["r/salesops", "RevOps LinkedIn"],
            }
        ],
        "personas": [
            {
                "name": "Maya",
                "identity_labels": ["RevOps", "sales ops"],
            }
        ],
        "competitors": [
            {"name": "Clari", "strengths": ["enterprise forecasting"], "weaknesses": ["expensive"]}
        ],
    }


def sample_dont_build_research() -> dict[str, Any]:
    """Fixture that should fail gates / score low."""
    return {
        "product_name": "AI Widget",
        "one_liner": "Generic AI tool",
        "paying_customers": 8,
        "founder_hours_per_week": 2,
        "pmf_signal": "none",
        "core_job_completions": 2,
        "icp_segments": [
            {
                "name": "buyers",
                "role": "decision makers at mid-market companies",
                "pains": ["buy software once"],
                "watering_holes": [],
                "buying_committee": True,
            }
        ],
        "competitors": [],
        "personas": [],
    }
