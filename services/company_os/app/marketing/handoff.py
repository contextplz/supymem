"""Research → ContentBrief → MarketingPlan handoff.

Maps venture onboarding / market research into the content brief shape the
strategist already consumes, then optionally compiles the first week of jobs.
"""

from __future__ import annotations

import json
from typing import Any

from app.content import brief as brief_store
from app.marketing.strategist import create_plan_from_brief


def _str_list(value: Any, *, limit: int = 12) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = (
                item.get("text")
                or item.get("name")
                or item.get("claim")
                or item.get("title")
                or item.get("summary")
            )
        else:
            text = item
        s = str(text or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _competitors(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item.strip():
            out.append(
                {
                    "name": item.strip(),
                    "url": "",
                    "positioning": "",
                    "strengths": [],
                    "weaknesses": [],
                    "pricing": "",
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("company") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "url": str(item.get("url") or "").strip(),
                "positioning": str(
                    item.get("positioning") or item.get("summary") or ""
                ).strip(),
                "strengths": _str_list(item.get("strengths"), limit=6),
                "weaknesses": _str_list(item.get("weaknesses"), limit=6),
                "pricing": str(item.get("pricing") or "").strip(),
            }
        )
    return out[:8]


def _claims(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item.strip():
            out.append({"text": item.strip(), "evidence": "venture research", "source": ""})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("claim") or item.get("summary") or "").strip()
        if not text:
            continue
        out.append(
            {
                "text": text[:400],
                "evidence": str(item.get("evidence") or item.get("source") or "venture research")[
                    :240
                ],
                "source": str(item.get("url") or item.get("source") or "")[:240],
            }
        )
    return out[:20]


def _load_venture_bundle(venture_id: str, workspace_id: str) -> dict[str, Any]:
    """Best-effort load of onboarding MEM + research dossier for a venture."""
    from fastapi import HTTPException

    from app.db.models import Venture
    from app.state import db

    bundle: dict[str, Any] = {"venture_id": venture_id}
    with db()() as session:
        row = session.get(Venture, venture_id)
        if row is None or row.workspace_id != workspace_id:
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Not found."}})
        bundle["name"] = row.name
        bundle["raw_idea"] = row.raw_idea
        bundle["workspace_id"] = row.workspace_id

    try:
        from app.state import MEM

        mem_brief = MEM.get(f"brief:{venture_id}") or {}
        if isinstance(mem_brief, dict):
            bundle["mem_brief"] = mem_brief
            if isinstance(mem_brief.get("research"), dict):
                bundle["research"] = mem_brief["research"]
    except Exception:
        pass

    try:
        from app.config import data_root

        research_dir = data_root() / "ventures" / venture_id / "research"
        dossier = research_dir / "validation_dossier.md"
        if dossier.exists():
            bundle["dossier_md"] = dossier.read_text(encoding="utf-8")[:8000]
        recon = research_dir / "recon.json"
        if recon.exists():
            bundle["recon"] = json.loads(recon.read_text(encoding="utf-8"))
    except Exception:
        pass

    return bundle


def brief_dict_from_research(
    *,
    research: dict[str, Any] | None = None,
    venture: dict[str, Any] | None = None,
    product_name: str | None = None,
    one_liner: str | None = None,
) -> dict[str, Any]:
    """Map research / venture signals into a ContentBrief-shaped dict."""
    research = research or {}
    venture = venture or {}
    mem = venture.get("mem_brief") if isinstance(venture.get("mem_brief"), dict) else {}
    classification = (
        mem.get("classification")
        if isinstance(mem.get("classification"), dict)
        else {}
    )

    name = (
        product_name
        or venture.get("name")
        or classification.get("name")
        or "Company OS"
    )
    idea = str(one_liner or venture.get("raw_idea") or research.get("summary") or "").strip()
    if not idea:
        idea = f"An AI operator for {name}."

    diffs = _str_list(research.get("strongest_for")) or _str_list(
        research.get("differentiators")
    )
    if not diffs and research.get("summary"):
        diffs = [str(research["summary"])[:180]]

    pains = _str_list(research.get("strongest_against")) or _str_list(
        research.get("demand_signals")
    )
    if not pains:
        pains = ["No time to run marketing while building"]

    audience = str(
        classification.get("audience") or classification.get("domain") or "B2B founders"
    )
    claims = _claims(research.get("claims") or research.get("demand_signals"))
    if not claims and idea:
        claims = [{"text": idea[:240], "evidence": "venture idea", "source": ""}]

    template = brief_store.TEMPLATE
    return {
        "product": {
            "name": str(name)[:120],
            "one_liner": idea[:280],
            "category": str(
                classification.get("domain")
                or classification.get("build_archetype")
                or template["product"]["category"]
            ),
            "url": str(
                (venture.get("url") or (mem.get("product") or {}).get("url") or "")
            ).strip()
            or template["product"]["url"],
            "differentiators": diffs[:8] or list(template["product"]["differentiators"]),
            "proof": _str_list(research.get("sources"), limit=6),
        },
        "icp": {
            "segments": [
                {
                    "name": audience[:80],
                    "role": "Founder or Head of Growth",
                    "company_size": "1-50",
                    "industry": str(classification.get("domain") or "B2B SaaS"),
                    "geography": "US, EU, India",
                    "pains": pains[:8],
                    "triggers": ["Just raised", "First GTM hire", "Launched"],
                    "objections": ["AI marketing feels spammy", "Tried tools, no results"],
                    "watering_holes": ["r/SaaS", "r/startups", "linkedin"],
                }
            ]
        },
        "competitors": _competitors(research.get("competitors"))
        or list(template["competitors"]),
        "voice": dict(template["voice"]),
        "claims": claims,
        "cadence": dict(template["cadence"]),
        "visual_language": {
            "colors": ["#0B1F17", "#E8F0EC", "#1B4D3E"],
            "wordmark": str(name)[:40],
            "accent": "#1B4D3E",
        },
    }


def create_brief_from_venture(
    *,
    workspace_id: str,
    venture_id: str,
    research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a ContentBrief derived from venture research."""
    bundle = _load_venture_bundle(venture_id, workspace_id)
    research_blob = research or bundle.get("research") or bundle.get("recon") or {}
    data = brief_dict_from_research(research=research_blob, venture=bundle)
    return brief_store.save(
        workspace_id=workspace_id,
        data=data,
        source_name=f"research:{venture_id}",
    )


def create_plan_from_research(
    *,
    workspace_id: str,
    venture_id: str | None = None,
    research: dict[str, Any] | None = None,
    horizon_days: int = 30,
    compile_days: int = 7,
    activate: bool = True,
    replace_brief: bool = True,
) -> dict[str, Any]:
    """Full handoff: research → brief → strategist plan + compiled jobs."""
    brief_out: dict[str, Any] | None = None
    if replace_brief or brief_store.active(workspace_id) is None:
        if venture_id:
            brief_out = create_brief_from_venture(
                workspace_id=workspace_id,
                venture_id=venture_id,
                research=research,
            )
        elif research is not None:
            data = brief_dict_from_research(research=research)
            brief_out = brief_store.save(
                workspace_id=workspace_id,
                data=data,
                source_name="research:inline",
            )
        elif brief_store.active(workspace_id) is None:
            raise ValueError("no_research_or_brief")

    plan_out = create_plan_from_brief(
        workspace_id=workspace_id,
        horizon_days=horizon_days,
        compile_days=compile_days,
        activate=activate,
    )
    return {
        **plan_out,
        "brief": brief_out or brief_store.active(workspace_id),
        "handoff": "research→brief→plan",
        "venture_id": venture_id,
    }
