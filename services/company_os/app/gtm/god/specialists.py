"""Specialist runners. Automate what exists; instruct humans for the rest.

Never harvest leads. Never send LinkedIn.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app.gtm.god.catalog import FORBIDDEN_KINDS
from app.gtm.god.review import review_artifact
from app.intake.naming import slug_for_email


class SpecialistError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def run_specialist(
    kind: str,
    *,
    workspace_id: str,
    plan: dict[str, Any],
    artifacts: dict[str, Any],
    venture_id: str | None = None,
    inbox: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if kind in FORBIDDEN_KINDS:
        raise SpecialistError("forbidden_kind", 409)
    fn = {
        "talk_in_person": _talk_in_person,
        "research_icp": _research_icp,
        "competitors": _competitors,
        "positioning": _positioning,
        "brand_voice": _brand_voice,
        "site": _site,
        "domain": _domain,
        "launch_site": _launch_site,
        "launch_posts": _launch_posts,
        "seo": _seo,
        "mailbox": _mailbox,
        "prospects": _prospects,
        "outreach_drafts": _outreach_drafts,
        "review": _review,
    }.get(kind)
    if fn is None:
        raise SpecialistError("unknown_kind")
    return fn(
        workspace_id=workspace_id,
        plan=plan,
        artifacts=artifacts,
        venture_id=venture_id,
        inbox=inbox or [],
    )


def _idea(plan: dict[str, Any]) -> str:
    return str(plan.get("idea") or "").strip()


def _problem(plan: dict[str, Any]) -> str:
    return str(plan.get("problem") or _idea(plan)).strip()


def _name(plan: dict[str, Any], artifacts: dict[str, Any]) -> str:
    brand = artifacts.get("brand") or {}
    return str(brand.get("wordmark") or _idea(plan).split()[0] or "venture")[:40]


def _talk_in_person(**kwargs: Any) -> dict[str, Any]:
    idea = _idea(kwargs["plan"])
    instruction = (
        f"This week, talk to 5 people who live the problem behind: {idea}. "
        "Ask what they did last time it happened, who owned it, and what it cost. "
        "Do not pitch. Paste quotes back into Outreach when you have them."
    )
    return {
        "status": "blocked_human",
        "human_instruction": instruction,
        "output": {"channel": "in_person", "target_conversations": 5},
        "messages": [
            {
                "to": "icp_researcher",
                "body": "Field agent is collecting quotes. Keep ICP tagged assumption until quotes land.",
            }
        ],
    }


def _research_icp(*, workspace_id: str, plan: dict[str, Any], artifacts: dict[str, Any], **_: Any) -> dict[str, Any]:
    from app.gtm.relationships.service import patch_onboarding

    brief = plan.get("brief") or {}
    who = ((brief.get("who") or {}).get("text")) or _guess_who(plan)
    problem = _problem(plan)
    enrich = _llm_enrich("research_icp", plan, artifacts, workspace_id) or {}
    icp = {
        "who": str(enrich.get("who") or who),
        "role": str(enrich.get("role") or ("founder" if "founder" in who.lower() else "economic buyer")),
        "company_type": str(enrich.get("company_type") or "developer-tool or B2B product company"),
        "trigger": str(enrich.get("trigger") or "public complaint about the broken workflow"),
        "workflow": str(enrich.get("workflow") or problem),
        "status_quo": str(enrich.get("status_quo") or "spreadsheets, Slack search, or a tool they already resent"),
        "who_says_no": str(enrich.get("who_says_no") or "Anyone without budget or who is not in the workflow"),
        "tag": "assumption",
        "source": "llm" if enrich else "deterministic",
    }
    patch_onboarding(
        workspace_id,
        {
            "who": who,
            "roles": [icp["role"]],
            "excludes": ["gmail.com", "yahoo.com", "student"],
            "sender_geo": "US",
            "outbound_mode": "human_clipboard",
            "voice": {"write_like": "short, quote them, no pitch, no calendar link"},
        },
    )
    return {
        "status": "done",
        "output": {"icp": icp},
        "artifacts": {"icp": icp},
        "messages": [
            {
                "to": "positioning_agent",
                "body": f"ICP draft (assumption): {who}. Do not treat as validated.",
            }
        ],
    }


def _llm_enrich(kind: str, plan: dict[str, Any], artifacts: dict[str, Any], workspace_id: str) -> dict[str, Any] | None:
    from app.gtm.god.llm import json_chat

    idea = _idea(plan)
    problem = _problem(plan)
    return json_chat(
        purpose="gtm.god_research",
        system=(
            "You are a specialist GTM agent. Be specific and concrete. No puffery "
            "(seamless, revolutionary, best-in-class, AI-powered). Never invent "
            "customer counts or harvested lead lists. Return JSON only."
        ),
        user=(
            f"Task: {kind}\nIdea: {idea}\nProblem: {problem}\n"
            f"Artifacts: { {k: artifacts.get(k) for k in ('icp', 'competitors', 'positioning', 'brand', 'site') } }\n"
            "For research_icp return {who, role, company_type, trigger, workflow, status_quo, who_says_no}.\n"
            "For competitors return {competitors: [{name, positioning}] } with 3 status-quo or named rivals.\n"
            "For positioning return {hero, villain, wise_advisor, inciting_event, value_prop}.\n"
            "For launch_posts return {posts: [{angle, body}]} two short founder posts, no bot will send them."
        ),
        workspace_id=workspace_id,
    )


def _guess_who(plan: dict[str, Any]) -> str:
    blob = f"{_idea(plan)} {_problem(plan)}".lower()
    if "dev" in blob or "engineer" in blob:
        return "Engineering leads at 20-200 person product companies who cannot find the last decision"
    return "Operators who feel this problem weekly and currently patch it with a spreadsheet"


def _competitors(*, plan: dict[str, Any], artifacts: dict[str, Any], workspace_id: str = "", **_: Any) -> dict[str, Any]:
    idea = _idea(plan)
    enrich = _llm_enrich("competitors", plan, artifacts, workspace_id) or {}
    raw = enrich.get("competitors") if isinstance(enrich.get("competitors"), list) else None
    if raw:
        competitors = [
            {
                "name": str(c.get("name") or "status quo"),
                "positioning": str(c.get("positioning") or "status quo"),
                "tag": "assumption",
            }
            for c in raw[:4]
            if isinstance(c, dict)
        ]
    else:
        names = ["The spreadsheet they already have", "Doing nothing / tribal knowledge"]
        words = [w for w in idea.replace(",", " ").split() if w[:1].isupper() and len(w) > 3]
        if words:
            names.insert(0, f"Incumbent tools around {words[0]}")
        competitors = [{"name": n, "positioning": "status quo", "tag": "assumption"} for n in names[:4]]
    return {
        "status": "done",
        "output": {"competitors": competitors},
        "artifacts": {"competitors": competitors},
        "messages": [
            {
                "to": "positioning_agent",
                "body": "Competitors are status-quo sketches, not a scraped database.",
            }
        ],
    }


def _positioning(
    *,
    plan: dict[str, Any],
    artifacts: dict[str, Any],
    inbox: list[dict[str, str]] | None = None,
    workspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    icp = artifacts.get("icp") or {}
    comps = artifacts.get("competitors") or []
    villain = (comps[0]["name"] if comps else "the spreadsheet")
    hero = icp.get("who") or "the person who feels this weekly"
    enrich = _llm_enrich("positioning", plan, artifacts, workspace_id) or {}
    story = {
        "hero": str(enrich.get("hero") or hero),
        "villain": str(enrich.get("villain") or villain),
        "wise_advisor": str(enrich.get("wise_advisor") or _idea(plan)[:80]),
        "inciting_event": str(enrich.get("inciting_event") or icp.get("trigger") or "the workflow breaks in public"),
        "value_prop": str(
            enrich.get("value_prop")
            or f"For {hero}, replace {villain} with a first look they can try this week."
        ),
        "tag": "assumption",
        "source": "llm" if enrich else "deterministic",
    }
    judged = review_artifact(story)
    if not judged["ok"]:
        story["value_prop"] = f"For {hero}, show a working page and a reason to talk."
        judged = review_artifact(story)
    heard = [m for m in (inbox or []) if m.get("from") == "icp_researcher"]
    note = heard[-1]["body"] if heard else "No ICP note yet; treating story as assumption."
    return {
        "status": "done" if judged["ok"] else "failed",
        "output": {"positioning": story, "review": judged, "heard": note},
        "artifacts": {"positioning": story},
        "messages": [
            {
                "to": "brand_agent",
                "body": f"Story locked as assumption. Villain is {villain}. Heard: {note}",
            }
        ],
    }


def _brand_voice(*, plan: dict[str, Any], artifacts: dict[str, Any], workspace_id: str, **_: Any) -> dict[str, Any]:
    from app.content.brief import save as save_brief
    from app.firstlook.brandkit import build_brandkit

    idea = _idea(plan)
    brand = build_brandkit(idea, venture_name=_name(plan, artifacts), variant_index=0)
    payload = brand.to_dict() if hasattr(brand, "to_dict") else dict(brand)
    voice = {
        "tone": "direct, concrete, no hype",
        "person": "first",
        "reading_level": "plain",
        "banned_phrases": ["seamless", "revolutionary", "best-in-class", "AI-powered"],
    }
    icp = artifacts.get("icp") or {}
    comps = artifacts.get("competitors") or []
    try:
        save_brief(
            workspace_id=workspace_id,
            source_name="gtm_god",
            data={
                "product": {
                    "name": payload.get("wordmark") or idea[:40],
                    "one_liner": idea[:140],
                    "category": "early venture",
                    "url": "",
                    "differentiators": [
                        (artifacts.get("positioning") or {}).get("value_prop") or idea
                    ],
                },
                "icp": {
                    "segments": [
                        {
                            "name": icp.get("who") or "early buyers",
                            "role": icp.get("role") or "founder",
                            "pains": [icp.get("workflow") or _problem(plan)],
                            "triggers": [icp.get("trigger") or "workflow broke"],
                        }
                    ]
                },
                "competitors": [
                    {"name": c.get("name") or "status quo"} for c in comps[:5]
                ]
                or [{"name": "spreadsheet"}],
                "voice": voice,
                "claims": [],
            },
        )
    except Exception as exc:  # noqa: BLE001 — content brief is best-effort
        payload["brief_error"] = str(exc)[:200]
    return {
        "status": "done",
        "output": {"brand": payload, "voice": voice},
        "artifacts": {"brand": payload, "voice": voice},
        "messages": [
            {"to": "site_composer", "body": "Brand kit ready. Compose the first look."},
            {"to": "domain_agent", "body": "Use the same wordmark when reserving a preview domain."},
        ],
    }


def _site(
    *,
    workspace_id: str,
    plan: dict[str, Any],
    artifacts: dict[str, Any],
    venture_id: str | None,
    **_: Any,
) -> dict[str, Any]:
    from app.core.local_mode import is_local

    if is_local():
        from app.landing.service import prepare_page_build

        brief = {"idea": _idea(plan), "problem": _problem(plan), "brand": artifacts.get("brand") or {},
            "positioning": artifacts.get("positioning") or {}, "icp": artifacts.get("icp") or {},
            "competitors": artifacts.get("competitors") or []}
        order = prepare_page_build(venture_id, brief, workspace_id=workspace_id)
        return {"status": "blocked_human", "output": {"build": order, "brief": brief},
            "artifacts": {"site_brief": brief, "site_build": order},
            "human_instruction": "Open Builds to follow your page build and review its checks before publishing.",
            "messages": []}
    from app.firstlook.brandkit import build_brandkit
    from app.firstlook.compose import compose_html
    from app.firstlook.copy import copy_hash, fallback_copy
    from app.firstlook.serve import write_site
    from app.firstlook import versions as versions_mod

    idea = _idea(plan)
    pos = artifacts.get("positioning") or {}
    brand = build_brandkit(idea, venture_name=_name(plan, artifacts), variant_index=0)
    research = {"competitors": artifacts.get("competitors") or []}
    copy = fallback_copy(idea, audience=(artifacts.get("icp") or {}).get("who"), research=research)
    if pos.get("value_prop"):
        copy["subhead"] = str(pos["value_prop"])[:140]
    version = versions_mod.next_version(workspace_id)
    html, template_name, _plan = compose_html(
        brand=brand,
        copy=copy,
        idea_text=idea,
        version=version,
        stage="first_look",
        interest_action=f"/w/{workspace_id}/interest",
        variant_index=0,
        archetype=None,
        logo_url=None,
    )
    path, url = write_site(
        html,
        workspace_id=workspace_id,
        venture_id=venture_id,
        version=version,
        prefer_http=True,
    )
    digest = hashlib.sha256(html.encode()).hexdigest()
    view = versions_mod.persist(
        workspace_id=workspace_id,
        stage="first_look",
        template=template_name,
        archetype="first_look",
        brandkit_hash=brand.hash,
        copy_hash=copy_hash(copy),
        copy=copy,
        brandkit=brand.to_dict(),
        variant_index=0,
        path=str(path),
        url=url,
        sha256=digest,
        fallback=True,
        published=False,
    )
    site = {"url": view.url, "path": view.path, "version": view.version, "id": view.id}
    return {
        "status": "done",
        "output": site,
        "artifacts": {"site": site},
        "messages": [
            {"to": "launch_agent", "body": f"First look is at {view.url}."},
            {"to": "content_agent", "body": f"Use {view.url} in the launch post. Do not invent traffic numbers."},
        ],
    }


def _domain(*, plan: dict[str, Any], artifacts: dict[str, Any], **_: Any) -> dict[str, Any]:
    name = _name(plan, artifacts)
    slug = slug_for_email(name)
    host = f"{slug}.preview.localhost"
    instruction = (
        f"Preview hostname reserved: {host}. To own a real domain, buy {slug}.com "
        "(or the closest available) at your registrar and point DNS here. Software will not charge a card."
    )
    domain = {"host": host, "url": f"http://{host}", "simulated": True, "suggested": f"{slug}.com"}
    return {
        "status": "blocked_human",
        "human_instruction": instruction,
        "output": domain,
        "artifacts": {"domain": domain},
        "messages": [
            {"to": "site_composer", "body": f"Domain preview is {host}. Pair it with the first look."},
        ],
    }


def _launch_site(*, workspace_id: str, artifacts: dict[str, Any], **_: Any) -> dict[str, Any]:
    from app.db.models import SiteVersion
    from app.state import db
    from sqlalchemy import select

    site = artifacts.get("site") or {}
    with db()() as session:
        row = session.scalars(
            select(SiteVersion)
            .where(SiteVersion.workspace_id == workspace_id)
            .order_by(SiteVersion.version.desc())
        ).first()
        if row is None:
            return {"status": "failed", "output": {"reason": "no_site"}}
        row.published = 1
        session.commit()
        url = row.url
    launched = {"published": True, "url": url or site.get("url")}
    return {
        "status": "done",
        "output": launched,
        "artifacts": {"launched": launched},
        "messages": [{"to": "content_agent", "body": "Site is marked published. Draft the launch post now."}],
    }


def _launch_posts(*, workspace_id: str, plan: dict[str, Any], artifacts: dict[str, Any], **_: Any) -> dict[str, Any]:
    from app.content.brief import active
    from app.content.director import deterministic_topics

    brief = active(workspace_id) or {
        "product": {"name": _name(plan, artifacts), "one_liner": _idea(plan), "differentiators": []},
        "icp": {"segments": [{"name": "early buyers", "pains": [_problem(plan)], "objections": [], "triggers": []}]},
        "competitors": artifacts.get("competitors") or [],
    }
    topics = deterministic_topics(brief, 3)
    url = (artifacts.get("site") or {}).get("url") or (artifacts.get("launched") or {}).get("url")
    enrich = _llm_enrich("launch_posts", plan, artifacts, workspace_id) or {}
    posts = []
    raw_posts = enrich.get("posts") if isinstance(enrich.get("posts"), list) else []
    for item in raw_posts[:2]:
        if not isinstance(item, dict):
            continue
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        if "linkedin" in body.lower() and "yourself" not in body.lower():
            body += "\n\nNo bot will post this. Copy it and send it yourself."
        posts.append({"angle": item.get("angle"), "body": body, "channel": "human_social", "source": "llm"})
    if not posts:
        for topic in topics[:2]:
            body = (
                f"{topic.get('hook') or topic.get('angle')}\n\n"
                f"We put a first look up{' at ' + url if url else ''}. "
                "No bot will post this to LinkedIn. Copy it and send it yourself."
            )
            posts.append({"angle": topic.get("angle"), "body": body, "channel": "human_social", "source": "deterministic"})
    instruction = (
        "Copy the launch note into LinkedIn, X, or HN yourself. "
        "The content engine stays in dry-run unless you explicitly arm it. We will not auto-post to LinkedIn."
    )
    return {
        "status": "blocked_human",
        "human_instruction": instruction,
        "output": {"posts": posts},
        "artifacts": {"launch_posts": posts},
        "messages": [{"to": "seo_agent", "body": "Launch copy is ready. Add title/description next."}],
    }


def _seo(*, artifacts: dict[str, Any], **_: Any) -> dict[str, Any]:
    site = artifacts.get("site") or {}
    path = site.get("path")
    title = (artifacts.get("positioning") or {}).get("value_prop") or "First look"
    desc = (artifacts.get("icp") or {}).get("who") or "Early venture first look"
    snippet = {
        "title": str(title)[:70],
        "description": str(desc)[:160],
        "canonical": site.get("url"),
        "robots": "index,follow",
    }
    if path:
        target = Path(str(path)).parent / "seo.json"
        try:
            target.write_text(json.dumps(snippet, indent=2), encoding="utf-8")
            snippet["file"] = str(target)
        except OSError:
            pass
    return {
        "status": "done",
        "output": snippet,
        "artifacts": {"seo": snippet},
        "messages": [{"to": "mailbox_agent", "body": "SEO notes written. Company mail is next, before any sequences."}],
    }


def _mailbox(*, plan: dict[str, Any], artifacts: dict[str, Any], venture_id: str | None, **_: Any) -> dict[str, Any]:
    name = _name(plan, artifacts)
    slug = slug_for_email(name)
    addr = f"{slug}@companyos.local"
    domain = (artifacts.get("domain") or {}).get("suggested")
    instruction = (
        f"Local mailbox identity: {addr}. "
        f"When {domain or 'your domain'} is live, create hello@ there, add SPF/DKIM, then connect Gmail in Connections. "
        "Do not buy a warmup pool. Sequences only leave from the founder's own mailbox."
    )
    mailbox = {"address": addr, "simulated": True}
    return {
        "status": "blocked_human",
        "human_instruction": instruction,
        "output": mailbox,
        "artifacts": {"mailbox": mailbox},
        "messages": [
            {
                "to": "prospect_agent",
                "body": "Mailbox identity exists. Do not gather purchased lists. Wait for paste or first-party connectors.",
            }
        ],
    }


def _prospects(*, workspace_id: str, **_: Any) -> dict[str, Any]:
    from app.gtm.relationships.service import list_queue

    q = list_queue(workspace_id)
    items = q.get("items") or []
    if items:
        return {
            "status": "done",
            "output": {"count": len(items), "source": "existing_queue"},
            "artifacts": {"prospect_count": len(items)},
            "messages": [{"to": "draft_agent", "body": f"{len(items)} people already in the queue."}],
        }
    instruction = (
        "We will not invent names or scrape LinkedIn. Paste a job post, a public thread, "
        "or a profile you opened yourself in Outreach, or connect Slack/GitHub/Gmail on Connections."
    )
    return {
        "status": "blocked_human",
        "human_instruction": instruction,
        "output": {"count": 0, "harvest": False},
        "messages": [{"to": "draft_agent", "body": "No prospects yet. Waiting on founder paste."}],
    }


def _outreach_drafts(*, workspace_id: str, **_: Any) -> dict[str, Any]:
    from app.gtm.relationships.service import create_draft, list_queue

    q = list_queue(workspace_id)
    drafts = []
    for item in (q.get("items") or [])[:5]:
        cid = item.get("id")
        if not cid:
            continue
        try:
            d = create_draft(workspace_id, cid, "connect")
        except Exception as exc:  # noqa: BLE001
            drafts.append({"contact_id": cid, "error": str(exc)[:160]})
            continue
        drafts.append(
            {
                "contact_id": cid,
                "blocked": d.get("blocked"),
                "personalize": d.get("personalize"),
                "id": d.get("id"),
            }
        )
    instruction = (
        "Copy each allowed draft yourself. Mark sent only after you actually sent it. "
        "Software will not log into LinkedIn."
    )
    return {
        "status": "blocked_human" if drafts else "done",
        "human_instruction": instruction if drafts else None,
        "output": {"drafts": drafts, "linkedin_send": False},
        "artifacts": {"outreach_drafts": drafts},
        "messages": [{"to": "reviewer", "body": "Drafts are human-clipboard only."}],
    }


def _review(*, artifacts: dict[str, Any], **_: Any) -> dict[str, Any]:
    judged = review_artifact(artifacts)
    return {
        "status": "done" if judged["ok"] else "failed",
        "output": judged,
        "messages": [{"to": "god", "body": "Review complete." if judged["ok"] else "Review failed: " + ",".join(judged["flags"])}],
    }
