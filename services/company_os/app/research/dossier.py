"""Validation research dossier — fast scan + page fetch + synthesis (+ optional GPT Researcher)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from llm_gateway import sole
from sqlalchemy import select

from app.idea.discovery_guidance import DISCOVERY_GUIDANCE
from app.research.gpt_researcher_bridge import run_gpt_researcher
from app.research.evidence_dimensions import explicit_demand_leads
from app.research.presentation import founder_markdown, founder_report, gap_items, record_gaps
from app.research.web_fetch import fetch_page_markdown
from app.signals.research import research_recon

PURPOSE = "validation.dossier"
DOCUMENT_KIND = "validation_dossier"


def _dossier_dir(venture_id: str) -> Path:
    from app.config import data_root

    d = data_root() / "ventures" / venture_id / "research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _text_of(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    msg = getattr(choices[0], "message", None)
    return str(getattr(msg, "content", "") or "")


async def _enrich_sources(sources: list[dict[str, str]], *, limit: int = 5) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    sem = asyncio.Semaphore(3)

    async def one(src: dict[str, str]) -> None:
        url = src.get("url") or ""
        if not url:
            return
        async with sem:
            try:
                body = await asyncio.to_thread(fetch_page_markdown, url)
                out.append({**src, "body_excerpt": body[:4000]})
            except Exception as exc:
                from app.core.local_mode import is_local
                from app.credits.errors import CreditError
                if is_local() and isinstance(exc, CreditError):
                    raise
                message = ("You could not read this source. Configure your self-hosted page reader, then retry."
                           if is_local() else str(exc)[:120])
                out.append({**src, "fetch_error": message})

    await asyncio.gather(*(one(s) for s in sources[:limit]))
    return out


async def _synthesize_markdown(
    idea: str,
    recon: dict[str, Any],
    enriched: list[dict[str, str]],
) -> str:
    if os.environ.get("LLM_OFFLINE", "false").lower() in {"1", "true", "yes"}:
        return _fallback_markdown(idea, recon, enriched)

    prompt = {
        "idea": idea,
        "fast_scan": {
            "summary": recon.get("summary"),
            "competitors": recon.get("competitors"),
            "demand_signals": explicit_demand_leads(recon.get("demand_signals")),
            "differentiation_score": recon.get("differentiation_score"),
            "evidence_dimensions": recon.get("evidence_dimensions"),
        },
        "sources_enriched": enriched,
    }
    system = (
        "You are a venture analyst writing an idea-validation research dossier. "
        "Use ONLY the provided evidence, never invent metrics, customers, or funding. "
        "Separate problem validation, customer demand, competition, pricing, willingness to pay, "
        "market size, and risks, but include only topics with cited findings or challenges. "
        "Search snippets are unverified leads, not confirmed customer behavior. Missing public "
        "pricing, usage limits or credentials never imply no willingness to pay or a bad idea. "
        "Describe competitors' target customers, positioning, solutions, prices, overlap and gaps "
        "only when the cited evidence actually says so; otherwise omit that claim. "
        "Lead with What we found: competitors, pricing models, market insights, demand signals "
        "and customer segments, each with sources when present. Then Your opening: confidently "
        "recommend a segment, pain, wedge, positioning and channel as your opinion, not a found fact. "
        "Address you directly. Never put your own uncertain words in a heading. "
        "Use only findings and opening sections, plus Sources (linked list). "
        "Omit empty sections and missing-evidence commentary. If nothing usable was found, "
        "write one short constructive line asking you to share a relevant link or talk to potential customers. "
        "Be specific, cite URLs inline, and keep the length proportional to the evidence."
    ) + DISCOVERY_GUIDANCE
    try:
        from app.core.local_mode import is_local
        transmit = sole.transmit
        if is_local():
            from app.credits.metered import llm as transmit
        resp = await transmit(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)[:120000]},
            ],
            purpose=PURPOSE,
            workspace_id=sole.get_active_workspace_id(),
            tier="pro",
            max_tokens=3500,
        )
        text = _text_of(resp).strip()
        if len(text) > 400:
            return founder_markdown(text, {**recon, "problem": idea})
    except Exception as exc:
        if getattr(exc, "code", None) == "local_spend_limit":
            raise
        try:
            from llm_gateway.errors import ProviderCallError

            if isinstance(exc, ProviderCallError):
                base = _fallback_markdown(idea, recon, enriched)
                return (
                    f"> **AI provider issue:** {exc.message}\n"
                    f"> Open Settings → Your LLM key to fix this, then re-run research.\n\n"
                    + base
                )
        except Exception:
            pass
    return _fallback_markdown(idea, recon, enriched)


def _fallback_markdown(
    idea: str,
    recon: dict[str, Any],
    enriched: list[dict[str, str]],
) -> str:
    sources = [{**source, "id": source.get("id") or str(index), "summary": source.get("snippet") or source.get("summary") or ""}
               for index, source in enumerate(recon.get("sources") or [])]
    display = founder_report({"sources": sources}, recon.get("evidence_dimensions") or {}, context={"problem": idea})
    if display["empty_message"] and not explicit_demand_leads(recon.get("demand_signals")):
        return display["empty_message"] + "\n"
    lines = ["# Your research findings", "", "## What we found", ""]
    lines.extend(f"- {entry['finding']} ([source]({entry['url']}))" for entry in display["findings"])
    lines.extend(["", "## Findings by topic"])
    for label, row in display["dimensions"].items():
        lines.append(f"### {label.replace('_', ' ').title()}")
        for lead in row["support"] + row["challenge"]:
            lines.append(f"- {lead['finding']} ([source]({lead['url']}))")
        lines.append("")
    lines.extend(["", "## Customer requests"])
    for s in explicit_demand_leads(recon.get("demand_signals"))[:8]:
        if isinstance(s, dict):
            lines.append(f"- {s.get('claim', '')[:240]} ([source]({s.get('source')}))")
    lines.extend(["", "## Enriched sources"])
    for e in enriched:
        lines.append(f"- [{e.get('title', e.get('url'))}]({e.get('url')})")
    return founder_markdown("\n".join(lines), {**recon, "problem": idea})


def persist_validation_dossier(venture_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write the dossier to disk *and* Postgres so ECS replicas share one report."""
    markdown = str(payload.get("markdown") or "")
    out_dir = _dossier_dir(venture_id)
    md_path = out_dir / "validation_dossier.md"
    json_path = out_dir / "validation_dossier.json"
    sidecar = {k: v for k, v in payload.items() if k != "markdown" and k != "html"}
    for value in (sidecar, sidecar.get("fast_scan"), sidecar.get("summary")):
        if isinstance(value, dict):
            record_gaps(value)
            if value.get("limitations"):
                value["gap_items"].extend(item for item in gap_items(value["limitations"]) if item not in value["gap_items"])
    if markdown:
        md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(sidecar, indent=2, default=str), encoding="utf-8")

    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.scalar(
            select(VentureDocument).where(
                VentureDocument.venture_id == venture_id,
                VentureDocument.kind == DOCUMENT_KIND,
            )
        )
        body = markdown or json.dumps(sidecar, default=str)
        prov = json.dumps(sidecar, default=str)
        if row is None:
            session.add(
                VentureDocument(
                    id=new_id("doc"),
                    venture_id=venture_id,
                    kind=DOCUMENT_KIND,
                    title="Validation research",
                    path=str(md_path),
                    body_md=body,
                    provenance_json=prov,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.body_md = body
            row.path = str(md_path)
            row.provenance_json = prov
            row.updated_at = now
        session.commit()
    return payload


def load_validation_dossier(venture_id: str) -> tuple[str | None, dict[str, Any]]:
    """Prefer the shared DB copy; fall back to the local sidecar."""
    meta: dict[str, Any] = {}
    markdown: str | None = None

    from app.db.models import VentureDocument
    from app.state import db

    factory = db()
    with factory() as session:
        from app.ventures.journey_overrides import NOTE, read as journey_choices
        if "research" in journey_choices(venture_id, session=session)["skipped"]:
            from app.db.models import Venture
            from app.intake.assets import get_site
            from app.ventures.current_idea import current_idea
            venture = session.get(Venture, venture_id)
            context = {"summary": current_idea(venture, session=session) if venture else "", "existing_site": get_site(venture_id, session=session)["site"],
                       "limitations": [NOTE], "research_skipped": True}
            return None, context
        row = session.scalar(
            select(VentureDocument).where(
                VentureDocument.venture_id == venture_id,
                VentureDocument.kind == DOCUMENT_KIND,
            )
        )
        if row is not None:
            markdown = (row.body_md or "").strip() or None
            if row.provenance_json:
                try:
                    loaded = json.loads(row.provenance_json)
                    if isinstance(loaded, dict):
                        meta = loaded
                except json.JSONDecodeError:
                    meta = {}

    md_path, json_path = _dossier_dir(venture_id) / "validation_dossier.md", _dossier_dir(
        venture_id
    ) / "validation_dossier.json"
    if json_path.is_file():
        try:
            disk = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(disk, dict):
                meta = {**disk, **meta} if meta else disk
        except json.JSONDecodeError:
            pass
    if not markdown and md_path.is_file():
        markdown = md_path.read_text(encoding="utf-8")
    if not markdown:
        from app.db.models import Venture
        from app.intake.assets import get_site
        from app.ventures.current_idea import current_idea
        with factory() as session:
            venture = session.get(Venture, venture_id)
            if venture:
                meta = {**meta, "summary": current_idea(venture, session=session),
                    "existing_site": get_site(venture_id, session=session).get("site"),
                    "limitations": ["No research evidence is saved; use current context and treat missing facts as assumptions."]}
    return markdown, meta


def thin_dossier_from_scan(venture_id: str, idea: str, recon: dict[str, Any]) -> dict[str, Any]:
    """A real scan-backed report without waiting for the 90s synthesis."""
    markdown = _fallback_markdown(idea, recon, [])
    payload = {
        "venture_id": venture_id,
        "idea": idea,
        "engine": f"{recon.get('provider') or 'unknown'}_snippet_scan",
        "mode": recon.get("mode"),
        "download_path": f"/api/ventures/{venture_id}/research/dossier.md",
        "fast_scan": recon,
        "deep_research": {"ok": False, "reason": "scan_only"},
        "enriched_source_count": 0,
        "markdown": markdown,
    }
    persist_validation_dossier(venture_id, payload)
    return payload


async def build_validation_dossier(
    *,
    venture_id: str,
    idea: str,
    budget_s: float = 90.0,
) -> dict[str, Any]:
    """Full pipeline: fast recon → fetch top pages → synthesize → optional GPT Researcher."""
    from app.core.local_mode import is_local
    recon_call = research_recon
    if is_local():
        from app.research.local_providers import research_recon as recon_call
    recon = await recon_call(idea, budget_s=min(budget_s * 0.4, 40.0))
    # Store the scan immediately so a timed-out synthesis still leaves a real report.
    thin_dossier_from_scan(venture_id, idea, recon)
    sources = list(recon.get("sources") or [])
    enriched = await _enrich_sources(sources, limit=5)

    deep: dict[str, Any] = {"ok": False, "reason": "skipped"}
    use_deep = os.environ.get("RESEARCH_ENGINE", "dossier").lower() in {
        "deep",
        "gpt_researcher",
    }
    offline = os.environ.get("LLM_OFFLINE", "false").lower() in {"1", "true", "yes"}
    if use_deep and not offline:
        deep = await run_gpt_researcher(
            f"Idea validation research for a startup: {idea}. "
            "Lead with sourced competitor, demand, pricing and customer-segment findings. "
            "Then recommend your market opening as an opinion: segment, wedge, positioning and channel. "
            "Do not include missing-evidence lists or risk sections.",
            timeout_s=min(budget_s * 0.45, 120.0),
        )

    if deep.get("ok") and deep.get("report_markdown"):
        markdown = founder_markdown(str(deep["report_markdown"]), {**recon, "problem": idea})
        engine = "gpt_researcher"
    else:
        markdown = await _synthesize_markdown(idea, recon, enriched)
        engine = "dossier_synthesis"

    payload = {
        "venture_id": venture_id,
        "idea": idea,
        "engine": engine,
        "mode": recon.get("mode"),
        "download_path": f"/api/ventures/{venture_id}/research/dossier.md",
        "fast_scan": recon,
        "deep_research": deep,
        "enriched_source_count": len(enriched),
        "markdown": markdown,
    }
    persist_validation_dossier(venture_id, payload)
    return payload
