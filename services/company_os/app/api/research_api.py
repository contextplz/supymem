"""Research dossier download, HTML preview, and PDF export."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.config import data_root
from app.research.service import load_report
from app.research.evidence_dimensions import explicit_demand_leads
from app.research.report_render import markdown_to_html, markdown_to_pdf_bytes
from app.research.presentation import founder_markdown, founder_payload, next_questions

router = APIRouter(tags=["research"])


def _dossier_paths(venture_id: str) -> tuple[Path, Path]:
    base = data_root() / "ventures" / venture_id / "research"
    return base / "validation_dossier.md", base / "validation_dossier.json"


def _hydrate_files(venture_id: str, markdown: str, meta: dict) -> Path:
    """Make download routes work even when this replica never wrote the sidecar."""
    md_path, json_path = _dossier_paths(venture_id)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    if markdown and not md_path.is_file():
        md_path.write_text(markdown, encoding="utf-8")
    if meta and not json_path.is_file():
        json_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return md_path


def _public_meta(venture_id: str, idea: str, markdown: str | None, meta: dict) -> dict:
    questions = next_questions(meta)
    # Without a report, the saved limitation is the founder's reason and next action (e.g. add a topic).
    unavailable_reasons = [] if markdown else list(meta.get("limitations") or [])
    if markdown:
        markdown = founder_markdown(markdown, meta)
    meta = founder_payload(meta)
    scan = _scan_block(meta)
    if isinstance(scan.get("summary"), dict):
        scan["summary"] = {**scan["summary"], "next_questions": questions,
                           "coverage_gaps": [question["text"] for question in questions]}
    fast = meta.get("fast_scan") if isinstance(meta.get("fast_scan"), dict) else scan
    return {
        "available": bool(markdown),
        "limitations": unavailable_reasons or meta.get("limitations") or [],
        "next_questions": questions,
        "title": f"Validation research: {idea[:80]}" if idea else "Validation research",
        "idea": idea,
        "engine": meta.get("engine"),
        "mode": meta.get("mode") or scan.get("mode"),
        "scan": scan,
        "fast_scan": fast,
        "queries": scan.get("queries") or [],
        "sources": scan.get("sources") or [],
        "summary": scan.get("summary") or meta.get("summary"),
        "competitors": scan.get("competitors") or [],
        "elapsed_ms": (fast or {}).get("elapsed_ms") or meta.get("elapsed_ms"),
        "provider": (fast or {}).get("provider") or scan.get("provider") or meta.get("provider"),
        "depth": (fast or {}).get("depth") or meta.get("depth"),
        "preview_path": f"/api/ventures/{venture_id}/research/dossier.html" if markdown else None,
        "download_path": f"/api/ventures/{venture_id}/research/dossier.md" if markdown else None,
        "pdf_path": f"/api/ventures/{venture_id}/research/dossier.pdf" if markdown else None,
        "markdown": markdown,
        "html": markdown_to_html(
            markdown,
            title="Validation research",
            subtitle=(idea[:160] if idea else None),
        )
        if markdown
        else None,
    }


def _scan_block(meta: dict) -> dict:
    """Surface the recon that produced the dossier: the queries run, what they found, and why.

    The sidecar stores the whole recon dict under ``fast_scan``; without this the console
    can only show the rendered markdown and has no way to prove the research was real.
    """
    scan = meta.get("fast_scan")
    if not isinstance(scan, dict):
        scan = {}
    deep = meta.get("deep_research")
    if not isinstance(deep, dict):
        deep = {}
    return {
        "queries": [q for q in (scan.get("queries") or []) if q],
        "sources": [s for s in (scan.get("sources") or []) if s],
        "competitors": scan.get("competitors") or [],
        "demand_signals": explicit_demand_leads(scan.get("demand_signals")),
        "evidence_dimensions": scan.get("evidence_dimensions") or {},
        "summary": scan.get("summary"),
        "mode": scan.get("mode"),
        "reason": scan.get("reason"),
        "provider": scan.get("provider"),
        "elapsed_ms": scan.get("elapsed_ms"),
        "depth": scan.get("depth"),
        "differentiation_score": scan.get("differentiation_score"),
        "strongest_for": scan.get("strongest_for"),
        "strongest_against": scan.get("strongest_against"),
        "changes_verdict": scan.get("changes_verdict"),
        "enriched_source_count": meta.get("enriched_source_count"),
        "deep_research": {
            "ok": bool(deep.get("ok")),
            "reason": deep.get("reason"),
            "engine": deep.get("engine"),
        },
    }


def _load_report(venture_id: str) -> tuple[str, str | None, dict]:
    try:
        return load_report(venture_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found"}}) from exc


def _load_markdown(venture_id: str) -> tuple[str, dict]:
    _idea, markdown, meta = _load_report(venture_id)
    if not markdown:
        return "# Research not run\n\nContinue from your saved context or add a research topic.\n", meta
    _hydrate_files(venture_id, markdown, meta)
    return founder_markdown(markdown, meta), meta


@router.get("/ventures/{venture_id}/research/dossier")
def get_dossier_meta(venture_id: str):
    idea, markdown, meta = _load_report(venture_id)
    if markdown:
        _hydrate_files(venture_id, markdown, meta)
        return _public_meta(venture_id, idea, markdown, meta)
    return _public_meta(venture_id, idea, None, meta)


@router.get("/ventures/{venture_id}/research/dossier.md")
def download_dossier_md(venture_id: str):
    markdown, _meta = _load_markdown(venture_id)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="validation-research-{venture_id}.md"'},
    )


@router.get("/ventures/{venture_id}/research/dossier.html")
def preview_dossier_html(venture_id: str):
    idea, markdown, meta = _load_report(venture_id)
    markdown = founder_markdown(markdown, meta) if markdown else "# Research not run\n\nContinue from your saved context or add a research topic.\n"
    html_doc = markdown_to_html(
        markdown,
        title="Validation research",
        subtitle=idea[:160],
    )
    return HTMLResponse(html_doc)


@router.get("/ventures/{venture_id}/research/dossier.pdf")
def download_dossier_pdf(venture_id: str):
    idea, markdown, meta = _load_report(venture_id)
    markdown = founder_markdown(markdown, meta) if markdown else "# Research not run\n\nContinue from your saved context or add a research topic.\n"
    pdf = markdown_to_pdf_bytes(
        markdown,
        title="Validation research",
        subtitle=idea[:160],
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="validation-research-{venture_id}.pdf"'
        },
    )


class ResearchStartBody(BaseModel):
    failed_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    # One founder click; a replay with the same key returns the same job, never a second run.
    request_key: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/ventures/{venture_id}/research/dossier/regenerate")
def regenerate_dossier(venture_id: str, body: ResearchStartBody | None = None):
    """Compatibility start route; all research uses the durable confirmed flow."""
    from app.research.service import start
    try:
        if body and body.failed_job_id:
            from app.research.service import continue_failed
            return continue_failed(venture_id, body.failed_job_id)
        return start(venture_id, expected_revision_id=body.expected_revision_id if body else None,
                     request_key=body.request_key if body else None)
    except (RuntimeError, KeyError) as exc:
        if str(exc) == "startup_review_required":
            raise HTTPException(409, detail={"error": {"code": "startup_review_required",
                "message": "Review your answers and priorities before starting research.", "href": f"/v/{venture_id}/context"}}) from None
        if str(exc) == "research_confirmation_required":
            from app.chat.execute import _validation_first
            return _validation_first(venture_id)
        raise HTTPException(409, detail={"error": {"code": str(exc)}}) from exc
