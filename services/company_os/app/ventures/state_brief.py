"""Shared venture state brief for chat and other agents.

Assembled from real rows only. Hosted briefs read shared stores on every call.
Never invents metrics. Never includes connection emails or secrets.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from sqlalchemy import select

from app.state import MEM, db

BRIEF_CHAR_BUDGET = 4_000
_CACHE_TTL_S = 20.0
_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_SECRETISH = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|bearer\s+[a-z0-9._\-]+)\b"
)


def invalidate_venture_state_brief(venture_id: str | None = None) -> None:
    """Drop cached brief(s). Call after writes that change founder-visible state."""
    with _cache_lock:
        if not venture_id:
            _cache.clear()
            return
        for key in [key for key in _cache if key[0] == str(venture_id)]:
            _cache.pop(key, None)


def get_venture_state_brief(
    venture_id: str, *, force: bool = False, mark_visit: bool = False
) -> dict[str, Any]:
    """Return a bounded venture state brief. Safe to call from chat and agents."""
    vid = str(venture_id or "").strip()
    if not vid:
        return _empty_brief("")
    from app.db.models import Venture
    from app.tenancy import bind, current_or_none

    factory = db()
    with factory() as session:
        venture = session.get(Venture, vid)
        if not venture:
            return _empty_brief(vid)
        workspace_id = venture.workspace_id
    scope = current_or_none()
    if scope and (scope.workspace_id, scope.venture_id) != (workspace_id, vid):
        raise PermissionError("venture_brief_scope_mismatch")
    with bind(workspace_id=workspace_id, venture_id=vid, actor=scope.actor if scope else "system"):
        return _get_scoped_brief(vid, factory, force=force, mark_visit=mark_visit)


def _get_scoped_brief(vid, factory, *, force, mark_visit):
    from app.tenancy import current
    scope = current()
    cache_key = (vid, scope.workspace_id, scope.actor)
    now = time.monotonic()
    # Local invalidation cannot reach other API/worker replicas. PostgreSQL
    # briefs must observe corrections, forgets and current work immediately.
    shared = factory.kw["bind"].dialect.name == "postgresql"
    if not force and not shared:
        with _cache_lock:
            hit = _cache.get(cache_key)
            if hit and hit[0] > now:
                brief = dict(hit[1])
                if mark_visit:
                    _touch_visit(vid)
                return brief
    brief = assemble_venture_state_brief(vid)
    if not shared:
        with _cache_lock:
            _cache[cache_key] = (now + _CACHE_TTL_S, dict(brief))
    if mark_visit:
        _touch_visit(vid)
    return brief


def assemble_venture_state_brief(venture_id: str) -> dict[str, Any]:
    """Build the brief from live stores. Each section is present or explicitly absent."""
    from app.db.models import (
        ActivityLine,
        Approval,
        ContentPost,
        GtmEmailAccount,
        GtmGodPlan,
        IdeaBrief,
        Venture,
        VentureDocument,
        Workspace,
    )
    from app.db.supymem_models import BuildRun, ExecutionPlan, IdeaRevision, IdeaSession, PlanTask, ResearchDossier
    from app.ventures.current_idea import current_idea as idea_text, current_revision, revision_fields, latest_founder_statement
    from app.ventures.lifecycle import is_paused

    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            return _empty_brief(venture_id)
        workspace_id = ven.workspace_id
        idea = {
            "name": ven.name or "",
            "stage": ven.stage or "",
            "autonomy": ven.autonomy_level or "L1",
            "paused": False,
            "raw_idea": _clip(idea_text(ven, session=session), 400),
            "idea_summary": _clip(idea_text(ven, session=session, for_display=True), 400),
            "audience": None,
            "confirmed": False,
        }
        current_idea = session.scalars(select(IdeaSession).where(
            IdeaSession.workspace_id == workspace_id,
            IdeaSession.venture_id == venture_id,
            IdeaSession.state != "abandoned",
        ).order_by(IdeaSession.updated_at.desc()).limit(1)).first()
        if current_idea:
            idea["confirmed"] = bool(current_idea.state == "confirmed" and
                                     current_idea.current_revision_id == current_idea.confirmed_revision_id)
            revision = session.get(IdeaRevision, current_idea.current_revision_id) if current_idea.current_revision_id else None
            if revision and revision.session_id == current_idea.id:
                try:
                    structured = json.loads(revision.structured or "{}")
                except (TypeError, ValueError):
                    structured = {}
                if isinstance(structured, dict) and structured.get("project_intent") == "personal":
                    idea["project_intent"] = "personal"
            elif not revision:
                # Entry saves intent before the first clarification revision exists.
                workspace = session.get(Workspace, workspace_id)
                try:
                    settings = json.loads(workspace.settings_json or "{}") if workspace else {}
                except (TypeError, ValueError):
                    settings = {}
                if isinstance(settings, dict) and settings.get("project_intent") == "personal":
                    idea["project_intent"] = "personal"
        try:
            idea["paused"] = bool(is_paused(venture_id))
        except Exception:  # noqa: BLE001
            idea["paused"] = False
        brief_row = session.scalars(
            select(IdeaBrief)
            .where(IdeaBrief.venture_id == venture_id)
            .order_by(IdeaBrief.created_at.desc())
            .limit(1)
        ).first()
        latest_revision = current_revision(ven, session)
        latest_fields = revision_fields(latest_revision)
        if not latest_revision:
            intake_row = session.get(VentureDocument, f"intake_decision:{ven.id}")
            try:
                intake = json.loads(intake_row.body_md or "{}") if intake_row and intake_row.venture_id == ven.id and intake_row.kind == "intake_decision" else {}
            except (TypeError, ValueError):
                intake = {}
            intake = intake if isinstance(intake, dict) else {}
            latest_fields = intake.get("prefill_answers", {})
        from app.ventures.startup_review import problem_is_request
        idea.update({key: _clip(value, 400) for key in ("problem", "offer", "desired_outcome")
                     if isinstance(value := latest_fields.get(key), str) and value.strip()
                     and (key != "problem" or not problem_is_request(value))})
        founder_statement = latest_founder_statement(latest_revision, session)
        from app.intake.smart import condense_input
        creation_seed = bool(latest_revision and latest_revision.source == "founder" and (
            (latest_revision.rev == 1 and not founder_statement and not latest_fields.get("_corrects_revision_id"))
            or (founder_statement and " ".join(founder_statement.split())
                == " ".join(condense_input(ven.raw_idea or "").split()))))
        # An unchanged creation seed does not invalidate its classifier. A
        # newer unparsed correction still must not inherit the old audience.
        audience = None if founder_statement else latest_fields.get("customer") or latest_fields.get("audience")
        if isinstance(audience, str) and audience.strip():
            idea["audience"] = _clip(audience, 200)
        elif (not latest_revision or creation_seed) and brief_row and brief_row.audience:
            idea["audience"] = _clip(str(brief_row.audience), 200)

        research = _research_section(session, venture_id)
        research["plan_ready"] = False
        if current_idea and idea["confirmed"] and current_idea.confirmed_revision_id:
            dossiers = session.scalars(select(ResearchDossier).where(
                ResearchDossier.workspace_id == workspace_id,
                ResearchDossier.venture_id == venture_id,
                ResearchDossier.revision_id == current_idea.confirmed_revision_id,
                ResearchDossier.kind == "deep",
                ResearchDossier.state.in_(["complete", "partial"]),
            ).order_by(ResearchDossier.created_at.desc())).all()
            for dossier in dossiers:
                try:
                    summary = json.loads(dossier.summary or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(summary, dict) and summary.get("review_kind") in {"model_assessment", "retrieved_sources_only"}:
                    research["plan_ready"] = True
                    break
        plan = _plan_section(session, ExecutionPlan, PlanTask, venture_id)
        builds = _builds_section(session, BuildRun, venture_id, workspace_id)
        website = _website_section(workspace_id, venture_id)
        from app.chat.service_guides import snapshot as service_snapshot
        product_services = service_snapshot(session, venture_id)
        analytics = _analytics_section(session, venture_id, workspace_id)
        gtm = _gtm_section(session, GtmGodPlan, workspace_id, venture_id)
        content = _content_section(session, ContentPost, workspace_id, venture_id)
        from app.chat.recovery import founder_drafts

        content = (content + founder_drafts(session, venture_id))[:8]
        connections = _connections_section(session, GtmEmailAccount, workspace_id)
        device = _device_section(session, VentureDocument, venture_id, workspace_id)
        credits = _credits_section(venture_id, workspace_id)
        approvals = _approvals_section(session, Approval, workspace_id)
        recent_activity = _activity_section(session, ActivityLine, venture_id)
        from app.ventures.startup_review import enabled
        from app.ventures.founder_facts import read as read_facts
        founder_facts = read_facts(session, session.get(Venture, venture_id)) if enabled(session, venture_id) else {}

    from app.ventures.founder_memory import memory_section_for_brief

    memory = memory_section_for_brief(venture_id)

    attention = _attention(
        venture_id,
        workflow=_workflow(venture_id, workspace_id, idea["paused"]),
        builds=builds,
        device=device,
        approvals=approvals,
        recent_activity=recent_activity,
    )

    brief = {
        "founder_facts": founder_facts,
        "venture_id": venture_id,
        "workspace_id": workspace_id,
        "idea": idea,
        "research": research,
        "plan": plan,
        "builds": builds,
        "website": website,
        "product_services": product_services,
        "analytics": analytics,
        "gtm": gtm,
        "content_drafts": content,
        "connections": connections,
        "device_work": device,
        "credits": credits,
        "pending_approvals": approvals,
        "memory": memory,
        "attention": attention,
    }
    from app.ventures.journey_overrides import NOTE, read as journey_choices
    brief["journey_overrides"] = journey_choices(venture_id)
    if brief["journey_overrides"]["skipped"]:
        brief["research"]["limitations"] = [NOTE]
    from app.pagebuilder.api import enabled as page_builder_enabled
    from app.ventures.next_step import founder_next_step, next_step

    # Chat answers the founder: with the one-click page builder, the plan is optional there too
    # (automation keeps next_step's ordering and never reads this brief field).
    brief["next_step"] = founder_next_step(venture_id) if page_builder_enabled() else next_step(venture_id)
    from app.experiments.ledger import summary_lines

    brief["experiments"] = summary_lines(venture_id)
    from app.ventures.idea_page import context_line

    brief["idea_page"] = context_line(venture_id)
    return _bound_brief(brief)


def render_brief_for_llm(brief: dict[str, Any]) -> str:
    """Compact plain-text brief for the assistant system/user context."""
    lines: list[str] = []
    facts = brief.get("founder_facts") or {}
    if facts:
        lines.append("Founder facts (source-backed statements, not verified demand or action authority): "
            + json.dumps({field: {"value": fact["value"][:160], "source": fact.get("source"),
                "confidence": fact.get("confidence")} for field, fact in facts.items() if fact.get("value")}, ensure_ascii=False))
    idea = brief.get("idea") or {}
    lines.append(
        f"Venture: {idea.get('name') or 'unknown'} "
        f"(stage={idea.get('stage') or '?'}, autonomy={idea.get('autonomy') or '?'}, "
        f"paused={bool(idea.get('paused'))})"
    )
    if idea.get("raw_idea"):
        lines.append(f"Idea: {idea['raw_idea']}")
    if idea.get("project_intent") == "personal":
        lines.append("Project intent: personal or hobby. Default to a small useful build and do not push startup goals, but if the founder asks about customers, go-to-market or funding, help with that: Supymem supports it.")
    if idea.get("audience"):
        lines.append(f"ICP / audience: {idea['audience']}")
    else:
        lines.append("ICP / audience: not recorded")
    if brief.get("idea_page"):
        lines.append(str(brief["idea_page"]) + " (the founder-visible Idea page; newer saved facts win if they differ)")

    # Institutional memory is context, never current effect or consent authority.
    memory = brief.get("memory") or {}
    prefs = memory.get("preferences") or []
    decisions = memory.get("decisions") or []
    if memory.get("availability") == "unavailable":
        pass  # Missing recall is neither a broken-memory claim nor proof of no saved preferences.
    elif prefs:
        lines.append(
            "Founder preferences (saved statements, subject to current controls): "
            + "; ".join(str(p.get("statement") or "") for p in prefs if p.get("statement"))
        )
    elif memory.get("omitted_count", 0):
        lines.append("Founder preferences: omitted from this bounded context; saved memory is incomplete here.")
    else:
        lines.append("Founder preferences: none saved")
    constraints = memory.get("constraints") or []
    if constraints:
        lines.append("Saved constraints: " + "; ".join(str(row["statement"]) for row in constraints))
    if memory.get("omitted_count", 0):
        lines.append(f"Venture memory context omitted {memory['omitted_count']} complete items; retrieve their source before deciding.")
    if decisions:
        lines.append(
            "Saved decisions: "
            + "; ".join(str(d.get("statement") or "") for d in decisions if d.get("statement"))
        )

    # Existing source-backed INSIGHT statements remain reports, never effect or consent authority.
    insight_chars = insight_omitted = 0
    for row in memory.get("other") or []:
        if not isinstance(row, dict) or row.get("kind") != "insight":
            continue
        statement, evidence = row.get("statement"), row.get("evidence")
        if (not isinstance(statement, str) or not statement.strip() or not row.get("author")
                or not isinstance(evidence, list) or not evidence
                or any(not isinstance(ref, str) or not re.fullmatch(r"chat:[A-Za-z0-9_-]{1,128}", ref) for ref in evidence)):
            insight_omitted += 1
            continue
        line = ("Founder report (sources: " + ", ".join(evidence)
                + "; context only, current idea and controls win, no verified effect or approval): " + statement)
        if insight_chars + len(line) + 1 > 700:
            insight_omitted += 1
            continue
        lines.append(line)
        insight_chars += len(line) + 1
    if insight_omitted:
        lines.append(f"Founder reports omitted {insight_omitted} complete items; retrieve their source before deciding.")

    # Exact source/QA facts precede generic history and survive the brief budget.
    source = (brief.get("device_work") or {}).get("latest_coding_source")
    if source:
        from app.chat.coding_source_status import describe
        lines.append(f"Latest saved coding source: {source.get('coding_provider')}; "
                     f"order={source.get('id')}; source SHA-256={source.get('source_sha256')}. "
                     + describe(source))

    research = brief.get("research") or {}
    if research.get("present"):
        comps = ", ".join(research.get("competitors") or []) or "none named"
        lines.append(
            f"Research: present ({research.get('engine') or 'saved'}); competitors: {comps}"
        )
        if research.get("dossier_state"):
            lines.append(
                f"Saved deep research: state={research['dossier_state']}; "
                f"qualified sources reviewed={research.get('source_count', 0)} "
                f"(plus {research.get('lead_count', 0)} review leads, not counted as sources; "
                "say 'N sources reviewed' using the first number, never the total); "
                f"full pages verified={research.get('page_verified_count', 0)}. "
                "Other findings are search excerpts or repository metadata, not checked full pages."
            )
            if research.get("verified_titles"):
                lines.append("Verified source titles: " + "; ".join(research["verified_titles"]))
        if research.get("summary"):
            lines.append(f"Research note: {research['summary']}")
    else:
        lines.append("Research: none saved yet")
    lines.extend(research.get("limitations") or [])

    plan = brief.get("plan") or {}
    if plan.get("present"):
        lines.append(
            f"Plan: v{plan.get('version')} state={plan.get('state')}; "
            f"summary={plan.get('summary') or 'n/a'}; "
            f"current_step={plan.get('current_step') or 'n/a'}"
        )
    else:
        lines.append("Plan: none")

    builds = brief.get("builds") or {}
    last = builds.get("last")
    if last:
        fail = last.get("failure_reason") or last.get("error_code") or ""
        retry = last.get("retry_policy") or "manual"
        lines.append(
            f"Builds: last state={last.get('state')} credits_consumed={last.get('credits_consumed')}"
            + (f" failure={fail}" if fail else "")
            + f" retry_policy={retry}"
        )
    elif not builds.get("page"):
        lines.append("Builds: no runs")
    page = builds.get("page")
    if page:
        lines.append(f"Landing page: recorded build state={page.get('state')}"
                     + (f"; {page['failure_reason']}" if page.get("failure_reason") else ""))

    website = brief.get("website") or {}
    lines.append(
        f"Website: url={website.get('url') or 'none'}; "
        f"live={bool(website.get('live'))}"
        f"{' (the founder own site, not checked by Supymem)' if website.get('external') else ''}; "
        f"managed_waitlist_open={bool(website.get('waitlist_enabled'))}; "
        f"managed_waitlist_url={website.get('waitlist_url') if website.get('waitlist_enabled') else 'none'}; "
        f"public_submission_verified={bool(website.get('waitlist_submission_verified'))}; "
        f"consented_email_requests={website.get('waitlist_count')} "
        "(not verified email owners or active product users)"
    )

    services = brief.get("product_services") or {}
    lines.append(f"Founder-chosen product services: {services.get('choices') or {}}; "
                 + (f"hosted product URL={services.get('product_url')} verified by Supymem "
                    f"{str(services.get('verified_at'))[:10]} (HTTP 200, title {services.get('title')!r}); "
                    "sign-in and database unverified; "
                    if services.get("verified") else
                    f"founder-reported product URL={services.get('product_url') or 'none'}; "
                    "app, database and sign-in unverified; ")
                 + "separate from the managed landing page.")

    analytics = brief.get("analytics") or {}
    if analytics.get("available"):
        counts = analytics.get("counts") or {}
        lines.append(
            "Founder product analytics: browser-reported event totals, not unique or verified users; "
            + "; ".join(f"{key}={int(counts[key])}" for key in analytics.get("steps", []) if key in counts)
            + ". Payment events do not establish revenue."
        )
    elif analytics.get("state") == "empty":
        lines.append("Founder product analytics: no recorded browser events in the selected app's funnel; "
                     "an empty funnel does not prove zero site traffic or zero users.")
    else:
        lines.append("Founder product analytics: unavailable or no app selected; no metric claim.")

    gtm = brief.get("gtm") or {}
    if gtm.get("present"):
        lines.append(
            f"GTM diagnosis: skill={gtm.get('skill') or '?'}; "
            f"bottleneck={gtm.get('bottleneck') or '?'}; "
            f"now={', '.join(gtm.get('now_items') or []) or 'n/a'}"
        )
    else:
        lines.append("GTM diagnosis: none saved")

    drafts = brief.get("content_drafts") or []
    if drafts:
        lines.append(
            "Content drafts: "
            + "; ".join(
                f"{d.get('platform') or '?'} ({d.get('state') or '?'})" for d in drafts[:5]
            )
        )
    else:
        lines.append("Content drafts: none")

    conns = brief.get("connections") or {}
    gmail = conns.get("gmail") or {}
    if gmail.get("connected"):
        allowed = ", ".join(gmail.get("allowed") or []) or "read status only"
        lines.append(f"Gmail: connected; allowed={allowed}")
    else:
        lines.append("Gmail: not connected")
    other = conns.get("other") or []
    if other:
        lines.append(
            "Other connections: "
            + ", ".join(f"{c.get('provider')}={c.get('status')}" for c in other[:6])
        )

    device = brief.get("device_work") or {}
    recent = device.get("recent") or []
    if recent:
        bits = []
        for row in recent[:4]:
            detail = f"{row.get('title') or 'Mac task'} [{row.get('status') or '?'}]"
            if row.get("status") == "stopped":
                outcome = ("reviewed outcome independently verified" if row.get("task_verified")
                           else "goal outcome not verified")
                if row.get("browser_task"):
                    count = row.get("recorded_clicks", 0)
                    detail += f"; {count} server-recorded click{'s' if count != 1 else ''}"
                if row.get("coding_provider"):
                    detail += f"; coding provider {row['coding_provider']}"
                    if row.get("source_state") == "stored":
                        detail += "; exact local source stored in versioned custody; see separate source-bound QA status"
                    else:
                        detail += "; local source and QA not attested by this order"
                detail += f"; {outcome}; review {row.get('review_href')}"
            bits.append(detail)
        lines.append("Recent Mac / device work: " + "; ".join(bits))
    else:
        lines.append("Recent Mac / device work: none")

    credits = brief.get("credits") or {}
    if credits.get("available") is not None:
        lines.append(
            f"Credits: available≈{credits.get('available')} status={credits.get('status') or '?'}"
        )
    else:
        lines.append("Credits: unavailable")

    approvals = brief.get("pending_approvals") or []
    if approvals:
        lines.append(
            "Pending approvals: "
            + ", ".join(str(a.get("kind") or a.get("id") or "item") for a in approvals[:5])
        )
    else:
        lines.append("Pending approvals: none")

    step = brief.get("next_step") or {}
    if step.get("title"):
        lines.append(f"Next step: {step['title']} ({step.get('key')})")

    lines.extend(str(x) for x in (brief.get("experiments") or [])[:7])

    att = brief.get("attention") or {}
    lines.append("Attention, needs you: " + (_join_items(att.get("needs_you")) or "none"))
    lines.append("Attention, running: " + (_join_items(att.get("running")) or "none"))
    lines.append(
        "Attention, done since last visit: "
        + (_join_items(att.get("done_since_last_visit")) or "none")
    )

    # Protect preference lines from the char budget: keep head (prefs) intact.
    pref_block_end = 0
    for i, line in enumerate(lines):
        if (line.startswith("Founder preferences") or line.startswith("Saved decisions")
                or line.startswith("Saved constraints") or line.startswith("Venture memory")
                or line.startswith("Founder report") or line.startswith("Latest saved coding source")):
            pref_block_end = i + 1
    head = lines[: max(pref_block_end, 1)]
    tail = lines[max(pref_block_end, 1) :]
    text = "\n".join(head + tail)
    if len(text) > BRIEF_CHAR_BUDGET:
        head_text = "\n".join(head)
        budget_left = max(200, BRIEF_CHAR_BUDGET - len(head_text) - 20)
        tail_text = "\n".join(tail)
        if len(tail_text) > budget_left:
            tail_text = tail_text[:budget_left].rstrip() + "\n...(truncated)"
        text = head_text + ("\n" + tail_text if tail_text else "")
    return _scrub(text)


def _join_items(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return "; ".join(str(i) for i in items if i)[:500]


def _empty_brief(venture_id: str) -> dict[str, Any]:
    return _bound_brief(
        {
            "venture_id": venture_id,
            "workspace_id": None,
            "idea": {
                "name": "",
                "stage": "",
                "autonomy": "L1",
                "paused": False,
                "raw_idea": None,
                "audience": None,
                "confirmed": False,
            },
            "research": {"present": False},
            "plan": {"present": False},
            "builds": {"last": None},
            "website": {"url": None, "live": False, "waitlist_count": 0},
            "analytics": {"available": False},
            "gtm": {"present": False},
            "content_drafts": [],
            "connections": {"gmail": {"connected": False, "allowed": []}, "other": []},
            "device_work": {"recent": [], "latest_coding_source": None},
            "credits": {"available": None, "status": None},
            "pending_approvals": [],
            "memory": {"preferences": [], "decisions": [], "other": []},
            "attention": {
                "needs_you": [],
                "running": [],
                "done_since_last_visit": [],
            },
        }
    )


def _bound_brief(brief: dict[str, Any]) -> dict[str, Any]:
    """Ensure the rendered form stays under budget; trim large string fields."""
    rendered = render_brief_for_llm(brief)
    brief = dict(brief)
    brief["_rendered"] = rendered
    brief["_chars"] = len(rendered)
    return brief


def _clip(value: str, n: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _scrub(text: str) -> str:
    cleaned = _EMAIL_RE.sub("[redacted-email]", text)
    cleaned = _SECRETISH.sub("[redacted]", cleaned)
    return cleaned


def _touch_visit(venture_id: str) -> None:
    from core.clock import utcnow

    MEM[f"founder_last_seen:{venture_id}"] = utcnow().isoformat() + "Z"


def _last_visit(venture_id: str):
    from datetime import datetime, timedelta, timezone

    raw = MEM.get(f"founder_last_seen:{venture_id}")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc) - timedelta(hours=24)


def _workflow(venture_id: str, workspace_id: str | None, paused: bool) -> dict[str, Any]:
    if not workspace_id:
        return {}
    try:
        from app.ventures.run_status import snapshot

        return snapshot(venture_id, workspace_id, paused=paused) or {}
    except Exception:  # noqa: BLE001
        return {}


def _research_section(session, venture_id: str) -> dict[str, Any]:
    from app.config import data_root
    from app.chat.steer import founder_competitors
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession, ResearchDossier, ResearchFinding
    from app.ventures.next_step import current_research_report, research_report_usable

    named = []
    try:
        named = list(founder_competitors(venture_id) or [])
    except Exception:  # noqa: BLE001
        named = []
    dossier = MEM.get(f"research_dossier:{venture_id}") or {}
    venture = session.get(Venture, venture_id)
    saved = session.scalars(
        select(ResearchDossier)
        .where(ResearchDossier.venture_id == venture_id, ResearchDossier.kind == "deep",
               ResearchDossier.workspace_id == venture.workspace_id)
        .order_by(ResearchDossier.created_at.desc())
        .limit(1)
    ).first()
    idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
        IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
        .order_by(IdeaSession.updated_at.desc(), IdeaSession.id.desc()).limit(1))
    saved = current_research_report(session, idea) or saved
    md_path = data_root() / "ventures" / venture_id / "research" / "validation_dossier.md"
    present = research_report_usable(session, saved)
    summary = None
    source_count = 0
    total_count = 0
    page_verified_count = 0
    verified_titles = []
    if saved:
        try:
            document = json.loads(saved.summary or "{}")
        except (TypeError, ValueError):
            document = {}
        if isinstance(document, dict):
            assessment = document.get("review") or document.get("validation") or {}
            if isinstance(assessment, dict):
                summary = _clip(str(assessment.get("overview") or ""), 240) or None
        findings = session.scalars(
            select(ResearchFinding).where(ResearchFinding.dossier_id == saved.id)
        ).all()
        from app.research.verdict import qualified_count

        source_count = 0
        for finding in findings:
            try:
                evidence = json.loads(finding.evidence or "{}")
            except (TypeError, ValueError):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            # Same rule as the verdict card: review leads and unverified search leads are not sources.
            source_count += qualified_count({"_": [{"evidence_kind": evidence.get("evidence_kind"),
                                                    "category": finding.category,
                                                    "founder_flag": evidence.get("founder_flag")}]})
            total_count += 1
            if isinstance(evidence, dict) and evidence.get("page_verified") is True:
                page_verified_count += 1
                if len(verified_titles) < 3:
                    verified_titles.append(_clip(finding.title, 100))
    if isinstance(dossier, dict):
        cached_summary = dossier.get("summary")
        if isinstance(cached_summary, str) and cached_summary.strip():
            summary = summary or _clip(cached_summary, 240)
    if not summary and md_path.is_file():
        try:
            summary = _clip(md_path.read_text(encoding="utf-8"), 240)
            present |= bool(summary)
        except OSError:
            summary = None
    comps = [str(n) for n in named[:5]]
    if not comps and isinstance(dossier, dict):
        for c in (dossier.get("competitors") or [])[:5]:
            if isinstance(c, dict) and c.get("name"):
                comps.append(str(c["name"]))
            elif isinstance(c, str):
                comps.append(c)
    return {
        "present": present,
        "engine": ("deep" if saved else None) or (dossier.get("engine") if isinstance(dossier, dict) else None) or (
            "file" if md_path.is_file() else None
        ),
        "dossier_state": saved.state if saved else None,
        "source_count": source_count,
        "lead_count": total_count - source_count,
        "page_verified_count": page_verified_count,
        "verified_titles": verified_titles,
        "competitors": comps,
        "summary": summary,
    }


def _plan_section(session, ExecutionPlan, PlanTask, venture_id: str) -> dict[str, Any]:
    from app.db.supymem_models import PlanMilestone

    plan = session.scalars(
        select(ExecutionPlan)
        .where(ExecutionPlan.venture_id == venture_id)
        .order_by(ExecutionPlan.version.desc())
        .limit(1)
    ).first()
    if not plan:
        return {"present": False}
    step = session.scalars(
        select(PlanTask)
        .where(
            PlanTask.venture_id == venture_id,
            PlanTask.state.in_(("pending", "ready", "running", "blocked")),
            PlanMilestone.id == PlanTask.milestone_id,
            PlanMilestone.plan_id == plan.id,
            PlanMilestone.venture_id == venture_id,
        )
        .order_by(PlanMilestone.order.asc(), PlanTask.order.asc())
        .limit(1)
    ).first()
    return {
        "present": True,
        "id": plan.id,
        "version": plan.version,
        "state": plan.state,
        "summary": _clip(plan.summary or plan.goal_first_paid_user or "", 240),
        "current_step": _clip(step.title, 160) if step else None,
    }


def _builds_section(session, BuildRun, venture_id: str, workspace_id: str) -> dict[str, Any]:
    from app.credits.units import micro_to_credits
    from app.db.models import Job
    from app.pagebuilder import api

    builds = {"last": None}
    page = session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == venture_id,
        Job.workspace_id == workspace_id).order_by(Job.created_at.desc()).limit(1)).first()
    if page:
        view = api._view(page)
        builds["page"] = {"id": page.id, "state": view["state"], "failure_reason": view.get("error_reason")}

    run = session.scalars(
        select(BuildRun)
        .where(BuildRun.venture_id == venture_id)
        .order_by(BuildRun.created_at.desc())
        .limit(1)
    ).first()
    if not run:
        return builds
    spec: dict[str, Any] = {}
    try:
        spec = json.loads(run.spec or "{}")
        if not isinstance(spec, dict):
            spec = {}
    except (TypeError, ValueError):
        spec = {}
    reason = (
        run.exit_summary
        or run.error_code
        or spec.get("failure_reason")
        or spec.get("error")
        or spec.get("last_error")
    )
    credits = None
    try:
        credits = float(micro_to_credits(int(run.credits_used or 0)))
    except Exception:  # noqa: BLE001
        credits = None
    lifecycle = spec.get("lifecycle") if isinstance(spec.get("lifecycle"), dict) else {}
    review = lifecycle.get("review") if isinstance(lifecycle.get("review"), dict) else {}
    return {
        **builds,
        "last": {
            "id": run.id,
            "state": run.state,
            "closed": bool(run.closed_at),
            "review_status": review.get("status"),
            "error_code": run.error_code,
            "failure_reason": _clip(str(reason), 240) if reason else None,
            "credits_consumed": credits,
            # Builds only restart after an explicit founder approve/retry in product.
            # Do not tell the model a worker will auto-retry the build.
            "retry_policy": "manual",
        }
    }


def _website_section(workspace_id: str | None, venture_id: str) -> dict[str, Any]:
    website = MEM.get(f"website:{venture_id}") or {}
    url = website.get("url") if isinstance(website, dict) else None
    live = bool(isinstance(website, dict) and (website.get("published") or website.get("live")))
    if workspace_id:
        from app.db.supymem_models import Deployment, FounderApp

        with db()() as session:
            deployment = session.scalar(
                select(Deployment).join(FounderApp, Deployment.app_id == FounderApp.id)
                .where(Deployment.workspace_id == workspace_id,
                       Deployment.venture_id == venture_id,
                       Deployment.target == "supymem_static",
                       Deployment.state == "live",
                       Deployment.build_artifact_id.is_not(None),
                       FounderApp.workspace_id == workspace_id,
                       FounderApp.venture_id == venture_id,
                       FounderApp.deleted_at.is_(None))
                .order_by(Deployment.version.desc())
            )
            if deployment and deployment.url:
                url, live = deployment.url, True
    external = None
    if not live:
        try:
            from app.chat.recovery import external_site

            external = external_site(venture_id)
        except Exception:  # noqa: BLE001
            external = None
        if external:
            url, live = external["url"], True
    count = None
    waitlist_enabled = False
    waitlist_url = None
    submission_verified = False
    if workspace_id:
        try:
            from app.conversion.waitlist import status

            waitlist = status(venture_id, workspace_id)
            count = int(waitlist["total"])
            waitlist_enabled = bool(waitlist["enabled"])
            waitlist_url = waitlist["url"] if waitlist_enabled else None
            submission_verified = bool(waitlist["submission_verified"])
        except Exception:  # noqa: BLE001
            pass
    return {"url": url, "live": live, "external": bool(external), "waitlist_count": count,
            "waitlist_enabled": waitlist_enabled, "waitlist_url": waitlist_url,
            "waitlist_submission_verified": submission_verified}


def _analytics_section(session, venture_id: str, workspace_id: str) -> dict[str, Any]:
    """Use the founder dashboard's app event source, never operator telemetry."""
    try:
        from app.db.supymem_models import FounderApp
        from app.product_analytics.service import funnel

        app = session.scalars(select(FounderApp).where(
            FounderApp.venture_id == venture_id,
            FounderApp.workspace_id == workspace_id,
            FounderApp.deleted_at.is_(None),
            FounderApp.kind != "local_hermes",
        ).limit(1)).first()
        if app is None:
            return {"available": False, "state": "no_app"}
        result = funnel(app.id)
        counts = result.get("counts") if isinstance(result, dict) else None
        steps = result.get("steps") if isinstance(result, dict) else None
        if (not isinstance(counts, dict) or not isinstance(steps, list)
                or len(steps) > 12 or any(not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,40}", key)
                                       or type(counts.get(key)) is not int or counts[key] < 0 for key in steps)):
            return {"available": False, "state": "unavailable"}
        selected = {key: counts[key] for key in steps}
        return {"available": any(selected.values()), "state": "recorded" if any(selected.values()) else "empty",
                "counts": selected, "steps": steps, "app_id": app.id,
                "measurement_basis": "unverified_browser_event_counts_not_unique_people"}
    except Exception:  # noqa: BLE001
        return {"available": False, "state": "unavailable"}


def _gtm_section(session, GtmGodPlan, workspace_id: str | None, venture_id: str) -> dict[str, Any]:
    if not workspace_id:
        return {"present": False}
    plan = session.scalars(
        select(GtmGodPlan)
        .where(GtmGodPlan.workspace_id == workspace_id,
               GtmGodPlan.venture_id == venture_id)
        .order_by(GtmGodPlan.updated_at.desc())
        .limit(1)
    ).first()
    if not plan:
        return {"present": False}
    try:
        diagnosis = json.loads(plan.diagnosis_json or "{}")
    except (TypeError, ValueError):
        diagnosis = {}
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    now_items: list[str] = []
    for key in ("now", "now_moves", "next_actions"):
        raw = diagnosis.get(key)
        if isinstance(raw, list):
            for item in raw[:3]:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("skill") or item.get("name")
                    if title:
                        now_items.append(_clip(str(title), 80))
                elif isinstance(item, str) and item.strip():
                    now_items.append(_clip(item, 80))
    evidence_gaps = diagnosis.get("evidence_gaps")
    if not isinstance(evidence_gaps, list):
        evidence_gaps = []
    return {
        "present": True,
        "skill": _clip(str(diagnosis.get("skill") or diagnosis.get("diagnosis_skill") or ""), 80)
        or None,
        "bottleneck": _clip(
            str(diagnosis.get("bottleneck") or diagnosis.get("primary_bottleneck") or ""), 1200
        )
        or None,
        "evidence_gaps": [_clip(item, 1200) for item in evidence_gaps[:2] if isinstance(item, str)],
        "input_provenance": "founder_reported",
        "now_items": now_items[:3],
    }


def _content_section(session, ContentPost, workspace_id: str | None, venture_id: str) -> list[dict[str, Any]]:
    if not workspace_id:
        return []
    from app.content.generation import post_query
    rows = session.scalars(
        post_query(workspace_id, venture_id)
        .where(
            ContentPost.workspace_id == workspace_id,
            ContentPost.state.in_(("draft", "approved", "refused")),
        )
        .order_by(ContentPost.updated_at.desc())
        .limit(5)
    ).all()
    out = []
    for row in rows:
        out.append(
            {
                "id": row.id,
                "platform": row.platform,
                "state": row.state,
                "title": _clip(row.title or "", 80) or None,
            }
        )
    return out


def _connections_section(session, GtmEmailAccount, workspace_id: str | None) -> dict[str, Any]:
    """Booleans and allowlists only — never account emails."""
    gmail = {"connected": False, "allowed": []}
    other: list[dict[str, Any]] = []
    if not workspace_id:
        return {"gmail": gmail, "other": other}
    from app.connections import mailboxes
    refs = mailboxes.list_read_mailboxes(session, workspace_id)
    if refs:
        # Product policy: connected mailbox may draft; outbound send is not approved by default.
        gmail = {
            "connected": True,
            "allowed": ["read connection status", "draft outreach (no send)"],
        }
    if mailboxes.enabled():
        gmail["mailboxes"] = [{"mailbox_id": ref["mailbox_id"], "status": ref["status"]} for ref in refs]
    try:
        from app.connectors.service import list_connections

        for view in list_connections(workspace_id)[:8]:
            provider = str(getattr(view, "provider", "") or "")
            status = str(getattr(view, "status", "") or "")
            if provider.lower() in {"gmail", "composio_gmail"}:
                if not mailboxes.enabled() and (status == "active" or status == "connected"):
                    gmail["connected"] = True
                continue
            other.append({"provider": provider, "status": status})
    except Exception:  # noqa: BLE001
        pass
    return {"gmail": gmail, "other": other}


def _device_item(row, venture_id: str) -> dict[str, Any]:
    from app.ventures.device_attention import pending_mac_reconciliation

    try:
        body = json.loads(row.provenance_json or "{}")
    except (TypeError, ValueError):
        body = {}
    # The order state is authoritative. Older auxiliary status fields can
    # remain "reviewed" after an order has been cancelled or stopped.
    status = "needs_you" if pending_mac_reconciliation(body) else body.get("state") or body.get("status") or "unknown"
    native_stop = body.get("native_stop") if isinstance(body.get("native_stop"), dict) else {}
    verified = bool(status == "stopped" and body.get("claimed_at") and body.get("closed_at")
                    and native_stop.get("processes_stopped") is True
                    and native_stop.get("task_verified") is True
                    and native_stop.get("verification_kind")
                    and not body.get("cancelled_at")
                    and not body.get("stop_reconciliation_required")
                    and not body.get("navigation_effect_reconciliation")
                    and not body.get("startup_reconciliation"))
    invocations = body.get("invocations") if isinstance(body.get("invocations"), dict) else {}
    recorded_clicks = sum(
        1 for invocation in invocations.values()
        if isinstance(invocation, dict)
        and isinstance(invocation.get("effect"), dict)
        and invocation["effect"].get("state") == "returned"
    )
    item = {
        "id": row.id,
        "title": _clip(row.title or "Mac task", 120),
        "status": str(status),
        "approved": bool(body.get("approved_at") or body.get("claimed_at")),
        "executed": bool(body.get("claimed_at")),
        "task_verified": verified,
        "verification_kind": native_stop.get("verification_kind") if verified else None,
        "recorded_clicks": recorded_clicks if status == "stopped" else 0,
        "recorded_effects": recorded_clicks if status == "stopped" else 0,
        "settled_micro": body.get("settled_micro") if status == "stopped" else None,
        "review_href": f"/v/{venture_id}/device-work-orders/{row.id}",
    }
    manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else {}
    ceiling = manifest.get("credit_ceiling_micro")
    if type(ceiling) is int and 0 <= ceiling <= 1_000_000:
        item["credit_ceiling_micro"] = ceiling
    actions = manifest.get("actions") if isinstance(manifest.get("actions"), list) else []
    desktop = manifest.get("desktop_verification") if isinstance(manifest.get("desktop_verification"), dict) else {}
    if str(desktop.get("founder_goal") or "").strip():
        item["request"] = _clip(desktop["founder_goal"], 140)
    item["browser_task"] = "browser.read" in actions
    item["terminal_task"] = "terminal.execute" in actions
    coding = manifest.get("coding_verification") if isinstance(manifest.get("coding_verification"), dict) else {}
    provider = coding.get("provider")
    if "editor.edit" in actions and provider in {"cursor", "codex", "claude", "opencode"}:
        item["coding_provider"] = provider
    return item


def _device_section(session, VentureDocument, venture_id: str, workspace_id: str) -> dict[str, Any]:
    from app.db.supymem_models import CodingSourcePreflight, CodingSourceReceipt

    # Stream the ordered history: a fixed page can contain only unexecuted
    # reviews and hide the most recent claimed receipt from chat.
    rows = session.scalars(
        select(VentureDocument)
        .where(
            VentureDocument.venture_id == venture_id,
            VentureDocument.kind == "device_work_order_v1",
        )
        # A late stop or credit reconciliation updates an older order. Chat's
        # "latest task" must refer to when the task was created, not when a
        # receipt last changed.
        .order_by(VentureDocument.created_at.desc(), VentureDocument.id.desc())
    ).yield_per(100)
    recent = []
    latest_browser = None
    latest_executed = None
    latest_executed_by_kind: dict[str, dict[str, Any]] = {}
    projected = {}
    for row in rows:
        item = _device_item(row, venture_id)
        if len(projected) < 100:
            projected[row.id] = item
        if latest_browser is None and item["browser_task"]:
            latest_browser = item
        if latest_executed is None and item["approved"] and item["executed"]:
            latest_executed = item
        if item["approved"] and item["executed"]:
            for kind, matches in (
                ("browser", item["browser_task"]),
                ("terminal", item["terminal_task"]),
                (str(item.get("coding_provider") or ""), bool(item.get("coding_provider"))),
            ):
                if matches and kind not in latest_executed_by_kind:
                    latest_executed_by_kind[kind] = item
        if len(recent) < 6:
            recent.append(item)
    coding_order_ids = [item["id"] for item in recent if item.get("coding_provider")]
    if coding_order_ids:
        source_states = {order_id: (state, sha256) for order_id, state, sha256 in session.execute(
            select(CodingSourceReceipt.order_id, CodingSourceReceipt.state, CodingSourceReceipt.sha256).where(
                CodingSourceReceipt.venture_id == venture_id,
                CodingSourceReceipt.workspace_id == workspace_id,
                CodingSourceReceipt.order_id.in_(coding_order_ids),
            )
        ).all()}
        contract_checks = {order_id: (source_sha256, status, report_sha256, report_json) for
                           order_id, source_sha256, status, report_sha256, report_json in session.execute(
            select(CodingSourcePreflight.order_id, CodingSourcePreflight.source_sha256,
                   CodingSourcePreflight.status, CodingSourcePreflight.report_sha256,
                   CodingSourcePreflight.report_json).where(
                CodingSourcePreflight.venture_id == venture_id,
                CodingSourcePreflight.workspace_id == workspace_id,
                CodingSourcePreflight.order_id.in_(coding_order_ids),
            )
        ).all()}
        for item in recent:
            if item["id"] in source_states:
                item["source_state"], sha256 = source_states[item["id"]]
                if item["source_state"] == "stored":
                    item["source_sha256"] = sha256
                    checked = contract_checks.get(item["id"])
                    if checked and checked[0] == sha256:
                        item["source_contract_status"] = checked[1]
                        item["source_contract_report_sha256"] = checked[2]
                        try:
                            contract = json.loads(checked[3])
                        except (TypeError, ValueError):
                            contract = {}
                        if (isinstance(contract, dict)
                                and contract.get("runtime") in {"static_landing_v1", "python_product_v1", "unsupported_node_review_shell"}):
                            item["source_contract_runtime"] = contract["runtime"]
    # Recent Mac activity is deliberately short. A saved coding source must
    # remain discoverable after unrelated browser, terminal or navigation
    # orders push it out of the recent summary.
    source = session.scalar(select(CodingSourceReceipt).where(
        CodingSourceReceipt.venture_id == venture_id,
        CodingSourceReceipt.workspace_id == workspace_id,
        CodingSourceReceipt.state == "stored",
    ).order_by(CodingSourceReceipt.stored_at.desc(), CodingSourceReceipt.order_id.desc()).limit(1))
    latest_source = None
    if source is not None:
        item = projected.get(source.order_id)
        if item is None:
            row = session.get(VentureDocument, source.order_id)
            if row is not None and row.venture_id == venture_id and row.kind == "device_work_order_v1":
                item = _device_item(row, venture_id)
        if item is not None and item.get("coding_provider") == source.provider:
            latest_source = dict(item)
            latest_source["source_state"] = "stored"
            latest_source["source_sha256"] = source.sha256
            check = session.get(CodingSourcePreflight, source.order_id)
            if (check is not None and check.venture_id == venture_id
                    and check.workspace_id == workspace_id
                    and check.app_id == source.app_id and check.source_sha256 == source.sha256):
                latest_source["source_contract_status"] = check.status
                latest_source["source_contract_report_sha256"] = check.report_sha256
                try:
                    contract = json.loads(check.report_json)
                except (TypeError, ValueError):
                    contract = {}
                if (isinstance(contract, dict)
                        and contract.get("runtime") in {"static_landing_v1", "python_product_v1", "unsupported_node_review_shell"}):
                    latest_source["source_contract_runtime"] = contract["runtime"]
            from app.chat.coding_source_status import snapshot as coding_snapshot
            latest_source["source_qa"] = coding_snapshot(session, source)
            recent = [latest_source if item["id"] == source.order_id else item for item in recent]
    if (latest_executed is not None and latest_source is not None
            and latest_executed["id"] == latest_source["id"]):
        latest_executed = latest_source
    if latest_source is not None:
        provider = str(latest_source.get("coding_provider") or "")
        if (provider in latest_executed_by_kind
                and latest_executed_by_kind[provider]["id"] == latest_source["id"]):
            latest_executed_by_kind[provider] = latest_source
    return {"recent": recent, "latest_browser": latest_browser,
            "latest_executed": latest_executed,
            "latest_executed_by_kind": latest_executed_by_kind,
            "latest_coding_source": latest_source}


def _credits_section(venture_id: str, workspace_id: str | None) -> dict[str, Any]:
    try:
        from app.credits.accounts import get_account, _credits_number

        account = get_account(venture_id)
        if not account:
            return {"available": None, "status": None}
        return {
            "available": _credits_number(account.granted_micro - account.spent_micro - account.reserved_micro),
            "status": account.status,
        }
    except Exception:  # noqa: BLE001
        return {"available": None, "status": None}


def _approvals_section(session, Approval, workspace_id: str | None) -> list[dict[str, Any]]:
    if not workspace_id:
        return []
    rows = session.scalars(
        select(Approval)
        .where(Approval.workspace_id == workspace_id, Approval.status == "pending")
        .order_by(Approval.created_at.desc())
        .limit(5)
    ).all()
    return [
        {
            "id": row.id,
            "kind": getattr(row, "kind", None) or getattr(row, "capability", None) or "approval",
        }
        for row in rows
    ]


def _activity_section(session, ActivityLine, venture_id: str) -> list[dict[str, Any]]:
    since = _last_visit(venture_id)
    rows = session.scalars(
        select(ActivityLine)
        .where(ActivityLine.venture_id == venture_id)
        .order_by(ActivityLine.seq.desc())
        .limit(30)
    ).all()
    out = []
    for row in rows:
        ts = getattr(row, "ts", None)
        if ts is not None:
            try:
                if ts.tzinfo is None:
                    from datetime import timezone

                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
            except Exception:  # noqa: BLE001
                pass
        status = str(getattr(row, "status", "") or "").lower()
        text = _clip(getattr(row, "text", None) or "", 120)
        if not text:
            continue
        if status in {"done", "completed", "succeeded", "failed", "cancelled"} or not status:
            out.append({"text": text, "status": getattr(row, "status", None)})
        if len(out) >= 5:
            break
    return out


def _attention(
    venture_id: str,
    *,
    workflow: dict[str, Any],
    builds: dict[str, Any],
    device: dict[str, Any],
    approvals: list[dict[str, Any]],
    recent_activity: list[dict[str, Any]],
) -> dict[str, list[str]]:
    needs: list[str] = []
    running: list[str] = []
    done: list[str] = []

    action = (workflow or {}).get("action") or {}
    if (workflow or {}).get("state") == "needs_you" and action.get("label"):
        needs.append(str(action["label"]))
    for row in device.get("recent") or []:
        status = str(row.get("status") or "").lower()
        title = str(row.get("title") or "Mac task")
        if status in {"reviewed", "approved", "ready", "needs_you", "pending_review"}:
            needs.append(f"Mac: {title} ({status})")
        elif status in {"claimed", "running", "in_progress"}:
            running.append(f"Mac: {title}")
        elif status in {"completed", "succeeded", "failed", "cancelled"}:
            done.append(f"Mac: {title} ({status})")
    last = (builds or {}).get("last")
    if last and str(last.get("review_status") or "") == "rejected":
        needs.append("Latest build has changes requested: review saved feedback and build evidence")
    elif last and str(last.get("state") or "") in {"failed", "cancelled"}:
        reason = last.get("failure_reason") or last.get("error_code") or "failed"
        needs.append(f"Last build failed: {reason}")
    for appr in approvals[:3]:
        needs.append(f"Pending approval: {appr.get('kind') or appr.get('id')}")
    if (workflow or {}).get("state") in {"queued", "working"}:
        activity = str((workflow or {}).get("activity") or "Company work running")
        # Product string "Waiting for a worker" means a queued company job, not build auto-retry.
        if activity.casefold() == "waiting for a worker":
            activity = "Company job waiting for the runner"
        running.append(activity)
    for act in recent_activity[:5]:
        done.append(str(act.get("text")))

    def _uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= 5:
                break
        return out

    return {
        "needs_you": _uniq(needs),
        "running": _uniq(running),
        "done_since_last_visit": _uniq(done),
    }
