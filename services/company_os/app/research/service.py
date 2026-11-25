"""Scoped dossier reads and durable reruns; never manufacture research success."""
from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from datetime import timezone

from sqlalchemy import select

from app.db.models import CreditReservation, Job
from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
from app.state import db
from app.core.local_mode import is_local
from app.research.errors import ResearchPreflightBlocked
from app.research.presentation import REVIEW_CONTEXT_NEXT_STEP, display_text, founder_markdown, founder_payload, founder_report, next_questions, saved_gaps


def _evidence(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def dossier_evidence_dimensions(findings: dict, review: dict | None, prior_art: dict | None = None, *, context: dict | None = None) -> dict:
    """Derived excerpt leads for a deep dossier; never infer customer voice from vendor copy."""
    from app.research.evidence_dimensions import evidence_dimensions

    cited_competitor_ids = {
        source_id
        for assessment in (review or {}).get("assessments", [])
        if str(assessment.get("category") or "").startswith("competitor_")
        for source_id in assessment.get("source_ids") or []
    }
    included = [
        finding
        for group in findings.values()
        for finding in group
        if finding.get("url") and finding.get("founder_flag") != "exclude"
        and finding.get("evidence_kind") not in {"search_lead_unverified", "name_source"} and finding.get("category") != "review_lead"
    ]
    sources = [{"id": finding["id"], "url": finding["url"], "title": finding.get("title"), "snippet": finding.get("summary") or "",
                "prior_art": finding.get("prior_art"), "repository": finding.get("repository"),
                "evidence_kind": finding.get("evidence_kind")} for finding in included]
    # Full-page text fetched by the server; other excerpts stay unverified.
    sources += [{"url": finding["url"], "snippet": extra["text"], "verified": True}
                for finding in included if finding.get("page_verified")
                for extra in finding.get("additional_excerpts") or []
                if extra.get("verified_page") and extra.get("text")]
    competitors = [{"website": finding["url"]} for finding in included if finding["id"] in cited_competitor_ids]
    return evidence_dimensions(sources, competitors, review, prior_art=prior_art, context=context)


def _public(row, session, idea_context=None, jobs=None, reservations=None, *, include_review=True):
    document = _evidence(row.summary)
    runtime = document.get("_runtime", {})
    state = row.state
    job = None
    if runtime.get("job_id"):
        job = jobs.get(runtime["job_id"]) if jobs is not None else session.get(Job, runtime["job_id"])
        if job and (job.workspace_id != row.workspace_id or job.venture_id != row.venture_id):
            job = None
        if job and job.workspace_id == row.workspace_id and job.venture_id == row.venture_id and state == "running" and job.status in {"failed_dead", "cancelled"}:
            state = "failed"
    from app.gstack.office_hours import answers_for
    if idea_context is None:
        idea_context = {}
    scope = (row.workspace_id, row.venture_id)
    if scope not in idea_context:
        latest = session.scalar(select(IdeaSession).where(
            IdeaSession.workspace_id == row.workspace_id, IdeaSession.venture_id == row.venture_id,
            IdeaSession.state != "abandoned").order_by(IdeaSession.created_at.desc()).limit(1))
        idea_context[scope] = (latest, bool(latest and any(answer["assumed"] for answer in answers_for(session, latest.id))))
    latest, assumed = idea_context[scope]
    if latest and latest.current_revision_id and row.revision_id != latest.current_revision_id:
        state = "outdated"
    operations = []
    unsettled = False
    for key, op in runtime.get("operations", {}).items():
        usage = op.get("usage", {})
        reservation = (reservations.get(usage["reservation_id"]) if reservations is not None
                       else session.get(CreditReservation, usage["reservation_id"])) if usage.get("reservation_id") else None
        unsettled |= op.get("state") in {"calling", "provider_responded", "responded", "uncertain"} or bool(
            reservation and reservation.workspace_id == row.workspace_id and reservation.status in {"active", "open", "reconciling"})
        public_operation = {"key": key, "state": op.get("state"), "provider": usage.get("provider"),
                            "credits_micro": usage.get("credits_micro"), "error_code": op.get("error_code")}
        counts = (op.get("response") or {}).get("source_counts")
        if isinstance(counts, dict):
            # Expose only code-owned numeric categories, never provider result
            # text, URLs, queries or private receipt internals.
            allowed = ("provider_rows", "parsed_rows", "scoped_out", "missing_url", "invalid_shape",
                       "unsafe_url", "off_topic", "over_limit", "admitted", "review_leads")
            public_operation["source_counts"] = {
                name: counts[name] for name in allowed
                if type(counts.get(name)) is int and 0 <= counts[name] <= 10000
            }
        operations.append(public_operation)
    # Exact allowlist, never dump runtime snapshots, reservations or account IDs.
    review = document.get("review") if include_review else None
    if isinstance(review, dict):
        from app.research.pricing import pricing_evidence_quote
        review = {**review}
        for field in ("service_checks", "competitor_checks"):
            if isinstance(review.get(field), list):
                review[field] = [
                    {**check, "pricing_quote": pricing_evidence_quote(check.get("pricing_quote"))}
                    if isinstance(check, dict) else check for check in review[field]
                ]
    summary = {"assumed_questions": assumed,
        "idea_session_id": latest.id if latest else None, "idea_revision_id": latest.current_revision_id if latest else None,
        "review": review, "review_kind": document.get("review_kind"), "validation": document.get("validation"),
        **({"startup_graveyard": document["startup_graveyard"]} if document.get("startup_graveyard") else {}),
        "coverage_gaps": document.get("coverage_gaps", []), "operations": operations,
        "error_code": document.get("error_code"), "usage": {
            "credits_micro": sum(op.get("usage", {}).get("credits_micro", 0) for op in runtime.get("operations", {}).values()),
            "has_unsettled": unsettled}}
    if not runtime:
        summary["coverage_gaps"] = [*summary["coverage_gaps"], "Legacy dossier provenance has not passed the current evidence contract."]
    if is_local():
        for operation in operations:
            operation.pop("credits_micro", None)
        summary["usage"].pop("credits_micro", None)
    summary["gap_items"] = document.get("gap_items") or []
    summary["next_questions"] = next_questions(document)
    stopped_early = bool(document.get("error_code") or state in {"failed", "cancelled"}
                         or job and job.status in {"failed_dead", "cancelled"})
    summary["stopped_early"] = stopped_early
    summary = founder_payload(summary)
    if is_local():
        from app.research.presentation import search_notice

        if notice := search_notice(document):
            summary["search_notice"] = notice
    if is_local() and any(item.kind == "system_model_context" for item in saved_gaps(document)):
        summary["review_next_step"] = REVIEW_CONTEXT_NEXT_STEP
    return {"id": row.id, "venture_id": row.venture_id, "revision_id": row.revision_id, "kind": row.kind,
            "state": state, "job_id": runtime.get("job_id"), "session_id": runtime.get("session_id"),
            "budget_calls": row.budget_calls, "calls_used": row.calls_used, "summary": summary,
            "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(), "completed_at": row.completed_at.replace(tzinfo=timezone.utc).isoformat() if row.completed_at else None}


def list_dossiers(venture_id: str) -> list[dict]:
    with db()() as session:
        rows = session.scalars(
            select(ResearchDossier)
            .where(
                ResearchDossier.venture_id == venture_id,
                ResearchDossier.kind == "deep",
            )
            .order_by(ResearchDossier.created_at.desc())
            .limit(100)
        ).all()
        documents = [_evidence(row.summary).get("_runtime", {}) for row in rows]
        job_ids = {document["job_id"] for document in documents if document.get("job_id")}
        reservation_ids = {op["usage"]["reservation_id"] for document in documents
                           for op in document.get("operations", {}).values()
                           if op.get("usage", {}).get("reservation_id")}
        jobs = {job.id: job for job in session.scalars(select(Job).where(Job.id.in_(job_ids)))} if job_ids else {}
        reservations = {reservation.id: reservation for reservation in session.scalars(
            select(CreditReservation).where(CreditReservation.id.in_(reservation_ids)))} if reservation_ids else {}
        idea_context = {}
        include_review = not is_local()
        return [_public(row, session, idea_context, jobs, reservations, include_review=include_review) for row in rows]


def _validation_strengths(session, row) -> list[str]:
    """What the idea review found strong for this dossier's idea revision (empty when none saved)."""
    from app.db.supymem_models import ValidationRun

    run = session.scalars(select(ValidationRun).where(
        ValidationRun.revision_id == row.revision_id, ValidationRun.state == "complete"
    ).order_by(ValidationRun.created_at.desc())).first() if row.revision_id else None
    try:
        report = json.loads(run.report or "{}") if run else {}
    except ValueError:
        return []
    return [str(x) for x in (report.get("strengths") or []) if isinstance(x, str)]


def get_dossier(dossier_id: str) -> dict:
    with db()() as session:
        row = session.get(ResearchDossier, dossier_id)
        if row is None:
            raise KeyError("dossier")
        body = _public(row, session)
        findings = session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == row.id).order_by(ResearchFinding.created_at, ResearchFinding.id))
        grouped = defaultdict(list)
        from app.research.deep import finding_page
        for finding in findings:
            source, evidence = finding_page(finding, _evidence(finding.evidence))
            if lead := source.get("search_lead"):
                source["search_lead"] = founder_payload(lead)
            grouped[finding.category].append({"id": finding.id, **source,
                "source": finding.source, "category": finding.category,
                "observed_at": evidence.get("observed_at"), "search_queries": evidence.get("search_queries", []),
                "search_intents": evidence.get("search_intents", []),
                "additional_excerpts": evidence.get("additional_excerpts", []),
                "evidence_kind": evidence.get("evidence_kind"), "page_verified": evidence.get("page_verified", False),
                "repository": evidence.get("repository"), "license": finding.license, "license_ok": finding.license_ok,
                "prior_art": evidence.get("prior_art"),
                "last_activity_at": finding.last_activity_at.isoformat() if finding.last_activity_at else None,
                "reuse_recommendation": finding.reuse_recommendation, "founder_flag": evidence.get("founder_flag")})
        body["findings"] = dict(grouped)
        review = body["summary"].get("review")
        if is_local() and isinstance(review, dict):
            from app.research.presentation import verbatim_page_quote

            sources = {finding["id"]: finding for group in grouped.values() for finding in group
                       if finding.get("founder_flag") != "exclude"}
            for section in ("observations", "service_checks", "competitor_checks", "customer_reviews"):
                items = review.get(section)
                if not isinstance(items, list):
                    continue
                items = [item for item in items if isinstance(item, dict)]
                for item in items:
                    for field, quote in list(item.items()):
                        if field == "quote" or field.endswith("_quote"):
                            source = sources.get(item.get("source_id"), {})
                            if not verbatim_page_quote(source, quote):
                                item[field] = None
                review[section] = [item for item in items if item.get("quote")] if section in {"observations", "customer_reviews"} else items
        # Service pricing/limits are source quotations, not synthetic catalogue rows.
        body["services"] = (body["summary"].get("review") or {}).get("service_checks", [])
        saved_prior = _evidence(row.summary).get("prior_art")
        if isinstance(saved_prior, dict):
            from app.research.deep import sources_for
            from app.research.prior_art.entities import compile_prior_art
            # Recompute against current custody. Excluding a finding immediately
            # removes its company facts, lesson citations and public profile.
            body["summary"]["prior_art"] = compile_prior_art(sources_for(row.id), body["summary"].get("review"),
                plan=_evidence(row.summary).get("_runtime", {}).get("prior_art_plan") or _evidence(row.summary).get("_runtime", {}).get("prior_art_review_plan"),
                gaps=saved_gaps(saved_prior), cost=saved_prior.get("cost"))
        context = _evidence(row.summary).get("_runtime", {}).get("snapshot", {})
        from app.db.models import Venture
        from app.ventures.current_idea import current_idea, current_revision, revision_fields
        venture = session.get(Venture, row.venture_id)
        if venture:
            fields = revision_fields(current_revision(venture, session))
            context = {**context, "problem": fields.get("problem") or current_idea(venture, session=session),
                       "customer": fields.get("customer") or context.get("customer") or ""}
        body["summary"]["evidence_dimensions"] = dossier_evidence_dimensions(
            body["findings"], body["summary"].get("review"), body["summary"].get("prior_art"), context=context
        )
        from app.research.verdict import research_verdict
        from app.ventures.stage import has_existing_customers
        body["has_customers"] = has_existing_customers(session, row.venture_id)
        strengths = _validation_strengths(session, row)
        body["summary"]["verdict"] = research_verdict(
            body["state"], body["findings"], body["summary"]["evidence_dimensions"], body["summary"].get("review"),
            strengths, has_customers=body["has_customers"])
        body["summary"]["founder_report"] = founder_report(
            body["findings"], body["summary"]["evidence_dimensions"], body["summary"].get("review"),
            body["summary"].get("prior_art"), strengths, context=context)
        for key, dimension in body["summary"]["evidence_dimensions"].items():
            display = body["summary"]["founder_report"]["dimensions"].get(key, {"support": [], "challenge": []})
            dimension.update(support=display["support"], challenge=display["challenge"])
        if isinstance(saved_prior, dict):
            questions = next_questions(_evidence(row.summary))
            for question in next_questions(saved_prior):
                if question not in questions:
                    questions.append(question)
            body["summary"]["next_questions"] = questions[:3]
            body["summary"]["coverage_gaps"] = [question["text"] for question in questions[:3]]
            body["summary"]["prior_art"] = founder_payload(body["summary"]["prior_art"])
        # Shape only founder-read prose; keep the rest (next_questions, ids, counts) for every consumer.
        body["summary"]["verdict"] = founder_payload(body["summary"]["verdict"])
        from app.research.evidence_dimensions import _PROMOTION, customer_problem_evidence
        from app.research.pricing import pricing_point
        from app.research.source_quality import relevant_search_lead, research_topic
        from app.research.prior_art.entities import product_description
        promotions = set()
        for group in body["findings"].values():
            for finding in group:
                raw = finding.get("summary") or ""
                promotion = bool(_PROMOTION.search(str(finding.get("title") or "") + " " + raw))
                if promotion:
                    promotions.add(finding["id"])
                voice = customer_problem_evidence(finding, context)
                finding["display_topic"] = ("Related products and context" if relevant_search_lead(research_topic(context), {**finding, "snippet": raw}) else "Search leads to review") if promotion else "Problem and demand" if voice else "Search leads to review" if (
                    finding.get("category") in {"problem", "community", "customer_request", "workaround", "customer_demand"}
                    or set(finding.get("search_intents") or []) & {"community", "customer_voice", "customer_request", "workaround"}) else None
                finding["display_pricing"] = pricing_point(raw, display_text(finding.get("title")), finding.get("url"))
                finding["summary"] = display_text(finding.get("summary"))
                if promotion:
                    finding["summary"] = product_description(finding["summary"])
                finding["title"] = display_text(finding.get("title"))
                for excerpt in finding.get("additional_excerpts") or []:
                    excerpt["text"] = display_text(excerpt.get("text"))
        body["summary"]["review"] = founder_payload(body["summary"].get("review"))
        review = body["summary"].get("review")
        if isinstance(review, dict):
            review["observations"] = [item for item in review.get("observations", []) if item.get("source_id") not in promotions]
        for check in [*(body["summary"].get("review") or {}).get("competitor_checks", []), *(body["summary"].get("review") or {}).get("service_checks", [])]:
            source = next((finding for group in body["findings"].values() for finding in group if finding["id"] == check.get("source_id")), None)
            if source:
                check["display_pricing"] = pricing_point(check.get("pricing_quote"), source["title"], source["url"])
        return body


def top_prior_art_lesson(body: dict) -> str | None:
    """Small owner seam for the completion message and next-step strip.

    Consumers pass the current scoped get_dossier response, never raw summary
    JSON. That read has rebuilt citations after founder exclusions.
    """
    from app.research.deep import safe_source_url
    from urllib.parse import quote
    if not isinstance(body, dict) or not isinstance(body.get("findings"), dict) or not isinstance(body.get("summary"), dict):
        return None
    prior = body["summary"].get("prior_art")
    if not isinstance(prior, dict) or not isinstance(prior.get("lessons"), list):
        return None
    sources = {finding["id"]: finding for group in body["findings"].values() if isinstance(group, list) for finding in group
               if isinstance(finding, dict) and isinstance(finding.get("id"), str) and finding.get("founder_flag") != "exclude"
               and (finding.get("page_verified") is True or finding.get("evidence_kind") in
                    {"hn_item", "hn_comment", "yc_company", "repository_metadata", "github_issue"})}
    for lesson in prior["lessons"]:
        if not isinstance(lesson, dict):
            continue
        citations = lesson.get("citations") or []
        text = lesson.get("text")
        if not isinstance(text, str) or not isinstance(citations, list) or not citations or any(not isinstance(c, dict) or c.get("finding_id") not in sources for c in citations):
            continue
        from app.research.selection import quote_options
        try:
            valid = all(type(c.get("quote_index")) is int and 0 <= c["quote_index"] < len(quote_options([
                {**sources[c["finding_id"]], "evidence": {"additional_excerpts": sources[c["finding_id"]].get("additional_excerpts", [])}}
            ])[c["finding_id"]]) for c in citations)
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            continue
        url = safe_source_url(str(sources[citations[0]["finding_id"]].get("url") or ""))
        if not url:
            continue
        text = " ".join(text.replace("\u2014", "; ").split())
        if len(text) > 360:
            text = text[:360].rsplit(" ", 1)[0] + "…"
        return "Lesson to test (inference): " + text + " [Source](" + quote(url, safe=":/?=&%#") + ")."
    return None


def set_finding_flag(venture_id: str, finding_id: str, flag: str | None) -> dict:
    if flag not in {None, "pin", "exclude"}:
        raise ValueError("founder_flag")
    with db()() as session:
        row = session.get(ResearchFinding, finding_id)
        if row is None or row.venture_id != venture_id:
            raise KeyError("finding")
        dossier = session.get(ResearchDossier, row.dossier_id, with_for_update=True)
        if dossier is None or dossier.venture_id != venture_id or dossier.workspace_id != row.workspace_id:
            raise KeyError("finding")
        row = session.scalar(select(ResearchFinding).where(ResearchFinding.id == finding_id).with_for_update().execution_options(populate_existing=True))
        evidence = _evidence(row.evidence)
        if flag is None:
            evidence.pop("founder_flag", None)
        else:
            evidence["founder_flag"] = flag
        row.evidence = json.dumps(evidence)
        session.commit()
        return {"id": row.id, "founder_flag": flag}


def export_markdown(venture_id: str, dossier_id: str) -> dict:
    body = get_dossier(dossier_id)
    if body["venture_id"] != venture_id:
        raise KeyError("dossier")
    summary = body["summary"]
    assessment = summary.get("review") or summary.get("validation") or {}
    lines = [f"# Research dossier {body['id']}", "", f"State: {body['state']}"]
    sample = any(finding.get("evidence_kind") == "sample" for group in body["findings"].values() for finding in group)
    display = summary.get("founder_report") or founder_report(body["findings"], summary.get("evidence_dimensions") or {}, assessment)
    from app.research.categorize import competitor_grid_markdown
    lines.extend(["", competitor_grid_markdown(display["competitor_grid"])])
    for title, entries in (("What we found", display["findings"]), ("What challenges the idea", display["contradictions"])):
        if entries:
            lines.extend(["", "## " + title, ""])
            lines.extend(f"- {entry['finding']} ([source]({entry['url']}))" for entry in entries)
    for text in display.get("assumptions", []):
        lines.append("Your assumption: " + text)
    for area, dimension in display["dimensions"].items():
        lines.extend(["", "## " + area.replace("_", " ").title(), ""])
        entries = dimension["support"] + dimension["challenge"]
        lines.extend(f"- {entry['finding']} ([source]({entry['url']}))" for entry in entries)
    sources = {finding["id"]: finding for group in body["findings"].values() for finding in group if finding.get("founder_flag") != "exclude"}
    for check in [*assessment.get("competitor_checks", []), *assessment.get("service_checks", [])]:
        source = sources.get(check.get("source_id"))
        if not source or not source.get("url"):
            continue
        for field in ("product_quote", "customer_quote", "positioning_quote", "pricing_quote", "limits_quote", "credentials_quote"):
            if text := display_text(check.get(field)):
                lines.append(f"- {text} ([source]({source['url']}))")
    prior_art = summary.get("prior_art")
    if prior_art:
        sources = {finding["id"]: finding for group in body["findings"].values() for finding in group}
        lines.extend(["", "## Who tried this before", ""])
        for company in prior_art["companies"]:
            lines.extend(["", "### " + company["name"]])
            if company.get("status") not in {None, "unknown"} and company.get("status_evidence"):
                lines.append("Status: " + company["status"])
            for citation in [company.get("what_they_built"), *company.get("pricing_now", []), *company.get("complaints", [])]:
                if citation and citation["finding_id"] in sources:
                    if text := display_text(citation.get("description") or citation.get("quote")):
                        lines.append(text + " (" + str(sources[citation["finding_id"]]["url"]) + ")")
        for lesson in prior_art["lessons"]:
            lines.append("- " + lesson["kind"].capitalize() + " (inference): " + lesson["text"])
            lines.extend("  Source: " + str(sources[citation["finding_id"]]["url"]) for citation in lesson["citations"])
        for person in prior_art["people"]:
            lines.append("- " + person["name"] + ": " + person["relation"] + " (" + person["public_url"] + ")")
    for category, findings in body["findings"].items():
        if category == "review_lead":
            continue
        lines.extend(["", "## " + category.replace("_", " ").title(), ""])
        for finding in findings:
            if finding.get("founder_flag") == "exclude" or finding.get("evidence_kind") in {"name_source", "search_lead_unverified"}:
                continue
            if not any(display_text(text) for text in [finding.get("summary"), *[excerpt.get("text") for excerpt in finding.get("additional_excerpts", [])]]):
                continue
            lines.extend(["- " + finding["title"], "  Source: " + str(finding["url"])])
            if finding.get("observed_at"):
                lines.append("  Observed: " + finding["observed_at"])
            if text := display_text(finding.get("summary")):
                lines.append("  Excerpt: " + text)
            for excerpt in finding.get("additional_excerpts", []):
                if text := display_text(excerpt.get("text")):
                    lines.append("  Additional excerpt: " + text)
    return {"markdown": founder_markdown("\n".join(lines), {**summary, "mode": "sample"} if sample else summary)}


def load_report(venture_id: str) -> tuple[str, str | None, dict]:
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.research.dossier import load_validation_dossier
    from app.state import MEM

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture:
            raise KeyError("venture")
        idea = venture.raw_idea or venture.name or ""
    markdown, meta = load_validation_dossier(venture_id)
    cached = MEM.get(f"research_dossier:{venture_id}")
    if isinstance(cached, dict):
        meta = {**meta, **cached}
    if is_local() or not meta.get("research_skipped"):
        completed = next((row for row in list_dossiers(venture_id)
                          if row["state"] in {"complete", "partial"} and row.get("completed_at")), None)
        if completed:
            report = get_dossier(completed["id"])
            summary = report["summary"]
            sources = [source for group in report["findings"].values() for source in group]
            mode = "sample" if any(source.get("evidence_kind") == "sample" for source in sources) else "live"
            markdown = export_markdown(venture_id, report["id"])["markdown"]
            meta = {**meta, "engine": "deep_research", "mode": mode, "depth": "deep", "limitations": summary.get("coverage_gaps", []) if mode == "sample" else [],
                    "summary": summary, "research_skipped": False, "deep_research": {"ok": True, "engine": "deep_research"},
                    "fast_scan": {"sources": sources, "summary": summary, "mode": mode, "depth": "deep",
                                  "competitors": summary["founder_report"]["competitor_grid"]["rows"],
                                  "evidence_dimensions": summary["evidence_dimensions"]}}
    return idea, markdown, meta


def review_inputs(dossier_id: str):
    """Current scoped saved evidence; never treat an unknown call as a retry."""
    from app.research.deep import sources_for
    with db()() as session:
        row = session.get(ResearchDossier, dossier_id)
        if row is None:
            raise KeyError("dossier")
        document = _evidence(row.summary)
        if row.kind != "deep" or row.state not in {"complete", "partial", "failed"} or not document.get("_runtime"):
            raise RuntimeError("research_saved_sources_unavailable")
        public = _public(row, session)
        if public["summary"]["usage"]["has_unsettled"]:
            raise RuntimeError("research_outcome_uncertain")
        from app.research.context import current_input
        idea, revision_id = current_input(session, row.venture_id)
        if idea.workspace_id != row.workspace_id or revision_id != row.revision_id:
            raise RuntimeError("research_inputs_changed")
        session.expunge(row)
    sources = [s for s in sources_for(dossier_id) if s["evidence"].get("observed_at")
        and s["evidence"].get("evidence_kind") in {"search_excerpt", "repository_metadata", "hn_item", "hn_comment", "yc_company", "github_issue", "web"}]
    if not sources:
        raise RuntimeError("research_saved_sources_unavailable")
    digest = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return row, sources, digest


def review_quote(dossier_id: str) -> dict:
    from app.credits.accounts import get_account
    from app.llm import founder_provider
    from app.research.citations import project
    from app.research.metered import synthesis_capture
    from app.research.selection import messages_for, response_schema
    from llm_gateway.provider_foundry import load_provider_config_from_env
    from llm_gateway.routing import resolve_purpose_config
    row, sources, digest = review_inputs(dossier_id)
    if type(row.workspace_id) is not str or not row.workspace_id.strip():
        raise RuntimeError("research_missing_workspace")
    runtime = _evidence(row.summary)["_runtime"]
    selected_plan = runtime.get("prior_art_plan") or runtime.get("prior_art_review_plan")
    context = {**runtime["snapshot"], **({"prior_art_query_plan": selected_plan} if selected_plan else {})}
    projected, source_map = project(sources[:50])
    resolved = founder_provider.resolve_llm_config(row.workspace_id)
    if resolved.workspace_id != row.workspace_id:
        raise RuntimeError("research_resources_changed")
    founder = resolved.source == "founder_override"
    config = founder_provider.to_provider_config(row.workspace_id, resolved_config=resolved) if founder else load_provider_config_from_env()
    if config is None:
        raise RuntimeError("research_byok_unavailable")
    config = resolve_purpose_config("research.deep", config.model_copy(deep=True))
    if is_local():
        from llm_gateway.provider_foundry import deployment_for
        return {"source_digest": digest, "source_count": len(sources), "provider": config.provider,
            "model": deployment_for(config, config.captured_tier), "max_output_tokens": 8192,
            "payer": "founder", "sufficient_now": True}
    capture = synthesis_capture(config, messages_for(context, projected), response_schema(), purpose="research.deep",
        max_tokens=8192, request_id=f"{dossier_id}:review-quote:{digest}",
        provider_identity=founder_provider.resolved_provider_identity(resolved), source_map=source_map)
    ceiling = capture["estimate"]
    account = get_account(row.venture_id)
    available = account.granted_micro - account.spent_micro - account.reserved_micro if account else None
    return {"source_digest": digest, "source_count": len(sources), "provider": config.provider, "model": capture["model_binding"]["deployment"],
        "max_output_tokens": 8192, "ceiling_micro": 0 if founder else ceiling, "available_micro": available,
        "payer": "founder" if founder else "platform_credits",
        "sufficient_now": founder or bool(account and account.status == "active" and available >= ceiling)}


def start(venture_id: str, *, previous_dossier_id: str | None = None, phase: str | None = None,
          source_digest: str | None = None, expected_revision_id: str | None = None,
          request_key: str | None = None) -> dict:
    """request_key: one founder click. A replayed POST with the same key returns the same job;
    a new click (new key) after a failed/uncertain run starts exactly one new run. Never auto-retried."""
    from app.core.jobs import enqueue
    from app.chat.conversations import capture_job_conversation
    from app.db.session import serialize_writes
    from app.db.supymem_models import FounderRequest
    from app.research.deep import PHASES
    from core.clock import utcnow
    phase = phase or "all"
    if phase not in PHASES:
        raise ValueError("research_phase_invalid")
    from app.research.local_providers import setup_reply
    blocked = setup_reply(review_only=True) if phase == "review" else setup_reply()
    if blocked is not None:
        return blocked
    with db()() as session:
        serialize_writes(session, key="research-page-choice:" + venture_id)
        previous = session.get(ResearchDossier, previous_dossier_id) if previous_dossier_id else None
        if previous_dossier_id and (not previous or previous.venture_id != venture_id):
            raise KeyError("dossier")
        if previous and previous.kind != "deep":
            raise RuntimeError("validation_requires_revalidation")
        if phase == "review":
            if previous is None:
                raise RuntimeError("research_saved_sources_unavailable")
            quote = review_quote(previous.id)
            if not source_digest or source_digest != quote["source_digest"]:
                raise RuntimeError("research_inputs_changed")
            if not quote["sufficient_now"]:
                raise ResearchPreflightBlocked("research_insufficient_credits")
        from app.research.context import current_input
        idea, revision_id = current_input(session, venture_id)
        from app.ventures.startup_review import require_complete
        from app.db.models import Venture
        try:
            require_complete(session, session.get(Venture, venture_id))
        except ValueError:
            raise ResearchPreflightBlocked("startup_review_required") from None
        if previous and (previous.workspace_id != idea.workspace_id or previous.revision_id != revision_id):
            raise RuntimeError("research_inputs_changed")
        # A reviewed request binds to the revision while holding the same lock
        # used to serialize admission. Reject before adopting or queuing work.
        if expected_revision_id is not None and revision_id != expected_revision_id:
            raise RuntimeError("research_inputs_changed")
        from app.ventures.current_idea import current_idea, revision_fields
        from app.ventures.journey_overrides import read as journey_choices
        from app.intake.assets import get_site
        from app.research.source_quality import research_topic
        fields = revision_fields(session.get(IdeaRevision, revision_id))
        from app.ventures.startup_review import effective_fields
        from app.db.models import Venture
        fields = effective_fields(session, session.get(Venture, venture_id), fields)
        if not fields.get("summary"):
            fields["summary"] = current_idea(session.get(Venture, venture_id), session=session)
        if journey_choices(venture_id, session=session)["skipped"] and not research_topic({**fields,
                "existing_site": get_site(venture_id, session=session).get("site")}):
            session.commit()
            return {"kind": "needs_you", "state": "draft", "body_md": "What should we research? Add a short description of your idea or your website, then start research.",
                    "actions": [{"label": "Add research topic", "href": f"/v/{venture_id}/idea"}], "citations": []}
        binding = {"revision_id": revision_id, "phase": phase,
                   "previous_dossier_id": previous_dossier_id, "source_digest": source_digest,
                   "_chat_conversation": capture_job_conversation(venture_id, idea.workspace_id)}
        key = f"research.deep:{venture_id}:{request_key}" if request_key else None
        receipt_id = "frq_" + hashlib.sha256(json.dumps(
            ["research.start", idea.workspace_id, venture_id, request_key]).encode()).hexdigest() if request_key else None
        receipt_text = json.dumps(binding, sort_keys=True)
        receipt = session.get(FounderRequest, receipt_id) if receipt_id else None
        if receipt and (receipt.source, receipt.workspace_id, receipt.venture_id, receipt.text) != (
                "research_start_v1", idea.workspace_id, venture_id, receipt_text):
            raise RuntimeError("research_inputs_changed")
        # Recover this exact click before newer active work, including keyed jobs admitted before receipts existed.
        requested = session.get(Job, receipt.related_id) if receipt else (
            session.scalar(select(Job).where(Job.dedupe_key == key)) if key else None)
        if receipt and requested is None:
            raise RuntimeError("research_request_unavailable")
        if requested:
            payload = json.loads(requested.payload_json)
            payload["phase"] = payload.get("phase") or "all"
            if (requested.kind, requested.workspace_id, requested.venture_id) != (
                    "research.deep", idea.workspace_id, venture_id):
                raise RuntimeError("job_dedupe_scope_mismatch")
            if payload.get("_chat_conversation") != binding["_chat_conversation"]:
                raise RuntimeError("job_conversation_scope_mismatch")
            if any(payload.get(field) != value for field, value in binding.items()
                   if field != "_chat_conversation"):
                raise RuntimeError("research_inputs_changed")
        jid = requested.id if requested else None
        # Concurrent double clicks serialize on this idea and recover active work.
        active = session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == "research.deep",
            Job.workspace_id == idea.workspace_id,
            Job.status.in_(["scheduled", "retry_wait", "leased"])).order_by(Job.created_at.desc())).all() if jid is None else []
        for job in active:
            payload = json.loads(job.payload_json)
            if (payload.get("revision_id") == revision_id
                    and (payload.get("phase") or "all") == phase
                    and payload.get("previous_dossier_id") == previous_dossier_id
                    and payload.get("source_digest") == source_digest):
                if payload.get("_chat_conversation") != binding["_chat_conversation"]:
                    raise RuntimeError("job_conversation_scope_mismatch")
                jid = job.id
                break
        if jid is None:
            jid = enqueue(session, "research.deep", {"session_id": idea.id,
                "workspace_id": idea.workspace_id, "venture_id": idea.venture_id, **binding},
                workspace_id=idea.workspace_id, venture_id=idea.venture_id, dedupe_key=key)
        if receipt_id and receipt is None:
            now = utcnow()
            session.add(FounderRequest(id=receipt_id, workspace_id=idea.workspace_id, venture_id=venture_id,
                source="research_start_v1", text=receipt_text, intent="research.start", classification="founder_approved",
                outcome="applied", related_id=jid, created_at=now, resolved_at=now))
        session.commit()
        return {"job_id": jid, "previous_dossier_id": previous_dossier_id}


def request_research_stop(venture_id: str, job_id: str, workspace_id: str, actor: str) -> bool:
    from app.authz.workspace import subject_person_id
    from app.core.jobs import request_cancel
    from app.db.models import Workspace

    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        if not workspace or workspace.owner_id != subject_person_id(actor):
            raise KeyError("job")
        if not session.scalar(select(Job.id).where(Job.id == job_id, Job.kind == "research.deep",
                Job.venture_id == venture_id, Job.workspace_id == workspace_id)):
            raise KeyError("job")
        stopped = request_cancel(session, job_id)
        session.commit()
        return stopped


def continue_failed(venture_id: str, job_id: str) -> dict:
    from app.credits.recovery import continue_job
    from app.db.session import serialize_writes
    from app.tenancy import current_or_none
    from app.research.local_providers import setup_reply
    blocked = setup_reply()
    if blocked is not None:
        return blocked
    with db()() as session:
        serialize_writes(session, key="research-page-choice:" + venture_id)
        job = session.get(Job, job_id, with_for_update=True)
        if not job or job.kind != "research.deep":
            raise KeyError(job_id)
        scope = current_or_none()
        result = continue_job(session, job, venture_id=venture_id, actor_subject=scope.actor if scope else None)
        session.commit()
        return {"job_id": result}


def rerun(dossier_id: str, phase: str | None = None, source_digest: str | None = None,
          request_key: str | None = None) -> dict:
    body = get_dossier(dossier_id)
    return start(body["venture_id"], previous_dossier_id=dossier_id, phase=phase, source_digest=source_digest,
                 request_key=request_key)
