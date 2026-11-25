"""One server-side answer to "what should the founder do next?" for a venture.

Ported from apps/console/src/lib/companyJourney.ts (same ordering). Chat, the
status bar, Cockpit and Now all read this; nothing here starts work.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select

from app.state import db

EXECUTABLE_PLAN_STATES = {"approved", "executing", "completed", "confirmed"}
ACTIVE_JOB = ("scheduled", "pending", "leased", "running", "retry_wait")


def research_source_hashes(session, dossier) -> list[str]:
    from app.db.supymem_models import ResearchFinding
    from app.tasks.skills import source_hash

    if not dossier or dossier.state not in {"complete", "partial"}:
        return []
    try:
        summary = json.loads(dossier.summary or "{}")
        if summary.get("error_code"):
            return []
    except (ValueError, TypeError, AttributeError):
        return []
    hashes = set()
    for finding in session.scalars(select(ResearchFinding).where(
            ResearchFinding.dossier_id == dossier.id, ResearchFinding.workspace_id == dossier.workspace_id,
            ResearchFinding.venture_id == dossier.venture_id)):
        try:
            evidence = json.loads(finding.evidence or "{}")
            if (finding.category != "review_lead" and evidence.get("founder_flag") != "exclude"
                    and evidence.get("observed_at") and evidence.get("evidence_kind") in {
                    "search_excerpt", "repository_metadata", "hn_item", "hn_comment", "yc_company", "github_issue", "web"}):
                hashes.add(source_hash(finding.url))
        except (ValueError, TypeError, AttributeError):
            continue
    return sorted(hashes)


def research_report_usable(session, dossier) -> bool:
    return bool(research_source_hashes(session, dossier))


def current_research_report(session, idea):
    from app.db.supymem_models import ResearchDossier

    from app.ventures.journey_overrides import read
    skipped = read(idea.venture_id, session=session)["skipped"] if idea else {}
    if (not idea or not idea.current_revision_id or (not skipped and (
            idea.state != "confirmed" or idea.current_revision_id != idea.confirmed_revision_id))):
        return None
    for dossier in session.scalars(select(ResearchDossier).where(
            ResearchDossier.workspace_id == idea.workspace_id, ResearchDossier.venture_id == idea.venture_id,
            ResearchDossier.revision_id == idea.current_revision_id, ResearchDossier.kind == "deep",
            ResearchDossier.state.in_(("complete", "partial")),
    ).order_by(ResearchDossier.created_at.desc(), ResearchDossier.id.desc())):
        if research_report_usable(session, dossier):
            return dossier
    return None


def _step(key: str, stage: str, title: str, detail: str, cta: str, href: str,
          chat_action: str | None = None, payload: dict | None = None) -> dict[str, Any]:
    return {"key": key, "stage": stage, "title": title, "detail": detail, "cta_label": cta,
            "chat_action": chat_action, "payload": payload, "href": href}


def _facts(venture_id: str) -> dict[str, Any]:
    """Saved state the journey depends on. Raises only on a missing venture."""
    from app.db.models import Job, Venture, VentureDocument, Workspace
    from app.db.supymem_models import (BuildRun, ClarifierTurn, ExecutionPlan, FounderApp, IdeaClarificationRun, IdeaRevision,
                                       IdeaSession, ResearchDossier)

    f: dict[str, Any] = {}
    with db()() as s:
        ven = s.get(Venture, venture_id)
        if ven is None:
            raise KeyError(venture_id)
        ws = ven.workspace_id
        workspace = s.get(Workspace, ws) if ws else None
        f["workspace_id"] = ws
        from app.ventures.journey_overrides import read as journey_choices
        f["journey_overrides"] = journey_choices(venture_id, session=s)
        from app.gtm.contact_work import NOTE, _json
        f["first_call_booked"] = any(_json(document.provenance_json).get("type") == "manual_outreach" and _json(document.body_md).get("booked_at") for document in s.scalars(
            select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind == NOTE)))
        from app.ventures.stage import has_existing_customers
        f["has_customers"] = has_existing_customers(s, venture_id)
        saved_page_choice = s.get(VentureDocument, f"page_choice:{venture_id}")
        try:
            f["page_skipped"] = bool(saved_page_choice and saved_page_choice.kind == "page_choice"
                and saved_page_choice.venture_id == venture_id
                and json.loads(saved_page_choice.body_md or "{}").get("choice") == "skip")
        except (ValueError, TypeError, AttributeError):
            f["page_skipped"] = False
        f["gtm_entry"] = False
        if ws:
            try:
                from app.workspaces import service as workspaces

                settings = workspaces.get_workspace(ws).settings or {}
                saved_intake = s.get(VentureDocument, f"intake_decision:{venture_id}")
                intake = json.loads(saved_intake.body_md or "{}") if saved_intake else {}
                f["intake_path"] = intake.get("path") if isinstance(intake, dict) else None
                answers = settings.get("answers")
                f["gtm_entry"] = settings.get("entry_goal") == "gtm" or (
                    isinstance(answers, dict) and answers.get("want_first") == "gtm")
                if settings.get("intake_venture_id") not in {None, venture_id}:
                    f["gtm_entry"] = False
            except Exception:  # noqa: BLE001
                pass
        f["active_jobs"] = bool(ws and s.scalar(select(Job.id).where(
            Job.venture_id == venture_id, Job.workspace_id == ws, Job.status.in_(ACTIVE_JOB),
            Job.kind != "idea.page_refresh",
            Job.id.not_in(select(IdeaClarificationRun.job_id).where(
                IdeaClarificationRun.workspace_id == ws, IdeaClarificationRun.venture_id == venture_id,
                IdeaClarificationRun.state.in_(("completed", "superseded"))))).limit(1)))
        f["active_research"] = bool(ws and s.scalar(select(Job.id).where(
            Job.venture_id == venture_id, Job.workspace_id == ws,
            Job.kind == "research.deep", Job.status.in_(ACTIVE_JOB)).limit(1)))
        idea = s.scalars(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.workspace_id == ws, IdeaSession.state != "abandoned",
        ).order_by(IdeaSession.updated_at.desc()).limit(1)).first()
        f["idea_state"] = idea.state if idea else None
        # No agent question or proposal saved yet: the first question is still being prepared.
        f["first_question_pending"] = bool(idea and not idea.questions_asked and idea.state not in {"reviewing", "validating", "confirmed"} and not s.scalar(select(ClarifierTurn.id).where(
            ClarifierTurn.session_id == idea.id, ClarifierTurn.kind.in_(("question", "proposal"))).limit(1))
            and s.scalar(select(Job.status).where(Job.venture_id == venture_id, Job.workspace_id == ws, Job.kind == "idea.clarify_turn")
                .order_by(Job.created_at.desc(), Job.id.desc()).limit(1)) not in {"failed", "failed_dead", "poison"})
        f["idea_confirmed"] = bool(idea and idea.state == "confirmed")
        turn = idea and s.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                                  .order_by(ClarifierTurn.turn.desc()).limit(1)).first()
        f["question_pending"] = bool(idea and idea.state == "validating" and turn
                                     and turn.role != "founder" and turn.kind == "question")
        f["validation_done"] = bool(idea and idea.state in {"reviewing", "validating"} and validation_settled(s, idea, include_accepted=True))
        f["validation_failed"] = bool(idea and idea.state == "validating" and not f["validation_done"]
                                      and _validation_failed(s, venture_id, ws))
        f["personal"] = f.get("intake_path") == "personal_build"
        rev = s.get(IdeaRevision, idea.current_revision_id) if idea and idea.current_revision_id else None
        if rev:
            try:
                f["personal"] |= (json.loads(rev.structured or "{}") or {}).get("project_intent") == "personal"
            except (TypeError, ValueError, AttributeError):
                pass
        from app.ventures.startup_review import enabled, review
        if not f["personal"] and enabled(s, ven.id):
            context = review(s, ven)
            f["startup_review"] = {key: context[key] for key in ("complete", "question", "priorities")}
        plan = s.scalars(select(ExecutionPlan).where(ExecutionPlan.venture_id == venture_id, ExecutionPlan.workspace_id == ws)
                         .order_by(ExecutionPlan.version.desc()).limit(1)).first()
        f["plan_state"] = plan.state if plan else None
        f["plan_id"] = plan.id if plan else None
        plan_revision_id = ((idea.current_revision_id if f["journey_overrides"]["skipped"] else idea.confirmed_revision_id)
                            if idea else None)
        f["plan_current"] = bool(plan and plan.revision_id == plan_revision_id
                                  and (idea or f["journey_overrides"]["skipped"]) and plan.superseded_by is None)
        # claim() inserts a "draft" row before the model call; without a saved
        # proposal that row is an unfinished or failed compile, not a plan to review.
        try:
            f["plan_has_proposal"] = bool(plan and json.loads(plan.structured or "{}").get("proposal"))
        except (TypeError, ValueError, AttributeError):
            f["plan_has_proposal"] = False
        dossier = s.scalars(select(ResearchDossier).where(
            ResearchDossier.workspace_id == ws, ResearchDossier.venture_id == venture_id, ResearchDossier.kind == "deep",
            ResearchDossier.revision_id == idea.current_revision_id,
        ).order_by(ResearchDossier.created_at.desc()).limit(1)).first() if idea and idea.current_revision_id else None
        f["dossier_state"] = dossier.state if dossier else None
        research_doc = json.loads(dossier.summary or "{}") if dossier else {}
        research_job_id = research_doc.get("_runtime", {}).get("job_id")
        research_job = s.get(Job, research_job_id) if research_job_id else None
        f["research_interrupted"] = bool(not f.get("active_research") and research_job
            and research_job.last_error in {"research_outcome_uncertain", "job_outcome_uncertain", "research_interrupted"})
        report = current_research_report(s, idea)
        f["research_report_ready"] = report is not None
        if report and f["dossier_state"] not in {"pending", "running"}:
            f["dossier_state"] = report.state
        f["products"] = len(s.scalars(select(FounderApp.id).where(
            FounderApp.venture_id == venture_id, FounderApp.deleted_at.is_(None))).all())
        f["accepted_build"] = f["product_attempt"] = f["accepted_product"] = f["build_review_pending"] = False
        f["accepted_landing"] = f["accepted_current_landing"] = False
        # Only a build of this confirmed idea in its workspace can advance the journey.
        page_job = None
        if f["idea_confirmed"] and idea.confirmed_revision_id:
            for job in s.scalars(select(Job).where(Job.venture_id == venture_id, Job.workspace_id == ws,
                Job.kind == "pagebuilder.build").order_by(Job.created_at.desc(), Job.id.desc())):
                try:
                    payload = json.loads(job.payload_json or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("revision_id") == idea.confirmed_revision_id:
                    if page_job is None:
                        page_job = job.status
                    if job.status in ACTIVE_JOB:
                        page_job = job.status
                        break
        f["page_build"] = (None if page_job is None else "ready" if page_job == "succeeded"
                           else "running" if page_job in ACTIVE_JOB else "failed")
        # Autopilot and suggestions use the default resolver too. The saved
        # choice never bypasses clarification or hides an active job.
        f["gtm_entry"] |= (f["page_skipped"] and f["idea_confirmed"] and not f["active_jobs"]
                           and not f["personal"])
        for run in s.scalars(select(BuildRun).where(
                BuildRun.venture_id == venture_id, BuildRun.workspace_id == ws,
                BuildRun.runtime_version == 1).order_by(BuildRun.created_at.desc(), BuildRun.id.desc())).all():
            try:
                spec = json.loads(run.spec or "{}")
            except (TypeError, ValueError):
                continue
            if "landing_build" not in f:
                f["landing_build"] = None
                captured_revision = (spec.get("source_import", {}).get("identity", {}).get("scope", {}).get("revision_id")
                                     or spec.get("inputs", {}).get("product_context", {}).get("revision_id"))
                if idea and captured_revision == idea.confirmed_revision_id:
                    from app.planning.publication_evidence import landing_build_result
                    landing = landing_build_result(s, run, spec)
                    if landing is not None:
                        f["landing_build"] = {"page": landing, "state": run.state,
                            "href": f"/v/{venture_id}/site?app={run.app_id}&run={run.id}#build-review"}
                        from app.core.fsm import BUILD_ACTIVE
                        app = s.get(FounderApp, run.app_id)
                        if (run.state in BUILD_ACTIVE and f["idea_confirmed"]
                                and idea.current_revision_id == idea.confirmed_revision_id and app and app.deleted_at is None
                                and (app.workspace_id, app.venture_id) == (ws, venture_id)):
                            f["page_build"] = "running"
            product = spec.get("artifact_profile") == "founder_product_v1"
            accepted = (run.state == "succeeded" and run.closed_at is not None
                        and spec.get("qa_state") == "passed"
                        and (spec.get("lifecycle", {}).get("review") or {}).get("status") == "accepted")
            # finished, QA passed, waiting for the founder's own accept / request changes (God mode never decides this)
            pending_review = (run.state == "succeeded" and run.closed_at is not None and spec.get("qa_state") == "passed"
                              and (spec.get("lifecycle", {}).get("review") or {}).get("status") == "pending")
            f["build_review_pending"] |= pending_review
            if pending_review and spec.get("artifact_profile") == "static_landing_v1":
                f["landing_review_href"] = f"/v/{venture_id}/site?app={run.app_id}&run={run.id}#build-review"
            f["product_attempt"] |= product
            f["accepted_product"] |= product and accepted
            f["accepted_build"] |= accepted
            f["accepted_landing"] |= accepted and spec.get("artifact_profile") == "static_landing_v1"
            if accepted and spec.get("artifact_profile") == "static_landing_v1" and f["idea_confirmed"]:
                inputs = spec.get("inputs") or {}
                context = (inputs.get("product_context") or {}) if isinstance(inputs, dict) else {}
                captured_revision = context.get("revision_id") if isinstance(context, dict) else None
                app = s.get(FounderApp, run.app_id)
                f["accepted_current_landing"] |= bool(captured_revision and idea and
                    captured_revision == idea.current_revision_id == idea.confirmed_revision_id and
                    idea.workspace_id == ws and rev and
                    (rev.workspace_id, rev.venture_id, rev.session_id) == (ws, venture_id, idea.id) and
                    ven.archived_at is None and ven.stage not in {"archived", "purged"} and
                    workspace and workspace.archived_at is None and
                    run.workspace_id == ws and app and app.deleted_at is None and
                    (app.workspace_id, app.venture_id) == (ws, venture_id))
    f["coding_tool"] = _coding_tool(f["workspace_id"])
    try:
        from app.firstlook.service import landing_exists

        f["landing"] = landing_exists(venture_id, f["workspace_id"])
    except Exception:  # noqa: BLE001
        f["landing"] = False
    try:
        from app.chat.recovery import external_site

        f["external_site"] = external_site(venture_id) is not None
    except Exception:  # noqa: BLE001
        f["external_site"] = False
    from app.ventures.stage import get_stage

    stage = get_stage(venture_id)
    f["founder_stage"] = stage["stage"]
    f["founder_stage_confirmed"] = stage["confirmed_by_founder"]
    f["founder_stage_record"] = stage
    f["published_site_url"] = next((item["link"] for item in stage.get("evidence", [])
                                    if item.get("source") == "publication" and item.get("link")), None)
    with db()() as s:
        from app.db.models import VentureDocument
        site = s.get(VentureDocument, f"external_site:{venture_id}")
        try:
            audit = json.loads(site.body_md or "{}") if site and site.venture_id == venture_id and site.kind == "external_site" else {}
            f["site_audited"] = isinstance(audit, dict) and audit.get("status") == "verified" and isinstance(audit.get("audit"), dict)
        except (TypeError, ValueError):
            f["site_audited"] = False
    try:
        from app.ventures.lifecycle import is_paused

        f["paused"] = is_paused(venture_id)
    except Exception:  # noqa: BLE001
        f["paused"] = False
    try:
        from app.experiments.ledger import loop_state

        st = loop_state(venture_id)
        f["exp_review"] = st["ready"]
        f["exp_run"] = st["open"][0] if st["open"] else None
    except Exception:  # noqa: BLE001 - the ledger never blocks the journey
        f["exp_review"], f["exp_run"] = False, None
    return f


def _coding_tool(workspace_id: str | None) -> bool:
    """The workspace owner has a paired Supymem Mac app: the one place Claude Code / Codex can build the page.

    ponytail: a paired Mac app is the server-side proxy; which CLI is installed is checked on the Mac (site page)."""
    if not workspace_id:
        return False
    try:
        from app.db.models import Workspace
        from app.db.supymem_models import DesktopDevice

        with db()() as s:
            ws = s.get(Workspace, workspace_id)
            return bool(ws and ws.owner_id and s.scalar(select(DesktopDevice.id).where(
                DesktopDevice.person_id == ws.owner_id, DesktopDevice.revoked_at.is_(None)).limit(1)))
    except Exception:  # noqa: BLE001 - unknown means "connect first", never a crash
        return False


def _validation_failed(session, venture_id: str, workspace_id: str | None) -> bool:
    """The newest idea.validate job died and no newer one is running or done (see chat/failed_jobs.py)."""
    from app.db.models import Job

    job = session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == "idea.validate",
        Job.workspace_id == workspace_id).order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).first()
    return bool(job and job.status in ("failed", "failed_dead", "poison"))


def validation_settled(session, idea, *, include_accepted=False) -> bool:
    """True when a "validating" session has a finished run for its current revision (waiting on the founder)."""
    from app.db.supymem_models import ValidationRun
    from app.idea.validation import METHODS

    query = select(ValidationRun.id).where(
        ValidationRun.session_id == idea.id, ValidationRun.revision_id == idea.current_revision_id,
        ValidationRun.workspace_id == idea.workspace_id, ValidationRun.venture_id == idea.venture_id,
        ValidationRun.method.in_(METHODS), ValidationRun.state == "complete")
    if not include_accepted:
        query = query.where(ValidationRun.founder_decision.is_(None))
    return session.scalar(query.limit(1)) is not None


def _is_test(title: Any) -> bool:
    return bool(re.search(r"\b(test|experiment|interviews?|survey|validate)\b", str(title), re.I))


def _experiment_run_step(base: str, exp: dict[str, Any]) -> dict[str, Any]:
    running = exp["state"] == "running"
    return _step("experiment.run", "Test it", f"Run: {exp['hypothesis'][:110]}",
                 "You started this test. Tell me what happened, even if it went badly." if running else
                 "One small test of the idea with real people. I will give you a short checklist; nothing is sent for you.",
                 "Tell Charlie what happened" if running else "Start this experiment", f"{base}/experiments",
                 None if running else "experiment.start", None if running else {"experiment_id": exp["id"]})


def next_step(venture_id: str, page_first: bool = False) -> dict[str, Any]:
    """Best next step for the venture; an honest fallback when state is unreadable."""
    base = f"/v/{venture_id}"
    try:
        from app.tenancy import current
        scope = current()
        if scope.venture_id not in {"", venture_id} or scope.workspace_id in {"", "none"}:
            raise PermissionError("next_step_scope_mismatch")
        facts = _facts(venture_id)
        if facts.get("workspace_id") != scope.workspace_id:
            raise PermissionError("next_step_scope_mismatch")
        step = _founder_resolve(venture_id, base, facts)
        from app.ventures.journey_overrides import NOTE
        choices = facts.get("journey_overrides") or {}
        skip_step = ("validation" if step["key"].startswith("idea.") and "clarify" not in step["key"] else
                     "clarification" if step["key"].startswith("idea.") or facts.get("first_question_pending") else
                     "research" if step["key"].startswith("research.") else
                     "landing_page" if step["key"].startswith(("site.", "build.", "plan.")) else
                     "find_customers" if step["key"].startswith("gtm.") else None)
        if choices.get("skipped"):
            step = {**step, "detail": step["detail"] + " " + NOTE}
        return {**step, "journey_overrides": choices, "skip_step": skip_step,
                "has_customers": facts.get("has_customers", False), "has_website": bool(facts.get("external_site") or facts.get("published_site_url")),
                "founder_stage": facts.get("founder_stage_record"), "site_url": step.get("site_url") or facts.get("published_site_url")}
    except Exception:  # noqa: BLE001 - never break a dashboard or chat reply
        return _step("venture.review", "Review", "Review your venture",
                     "Open the venture's saved work to see the next available action.",
                     "Open saved work", base)


def _page_step(venture_id: str, base: str, f: dict[str, Any]) -> dict[str, Any] | None:
    from app.ventures.state_brief import _website_section

    if f.get("page_build") == "running":
        return _step("site.building", "Step 4 of 5 · Landing page", "Building your landing page…",
                     "Your landing page is being built and checked. Open Website to watch it.",
                     "View Website", f"{base}/site")
    site = _website_section(f["workspace_id"], venture_id)
    if site["live"]:
        return None
    if f.get("page_skipped") and f.get("page_build") != "running":
        return _customers_step(venture_id)
    if f.get("accepted_landing"):
        return _step("site.publish", "Step 5 of 5 · Publish", "Publish your landing page",
                     "Your landing page is built and private. Publish it to get a link you can share.",
                     "Open the landing page", f"{base}/site",
                     "site.publish")
    from app.pagebuilder.api import enabled as server_builder_enabled
    from app.core.local_mode import is_local

    local = is_local()
    builder_ready = server_builder_enabled()
    if local and not builder_ready and f.get("idea_confirmed") and not f.get("build_review_pending"):
        from app.core.local_capabilities import capability

        landing = capability("landing_pages")
        if landing["state"] == "needs_setup":
            if f.get("page_build") == "ready":
                return _customers_step(venture_id)
            return _step("site.connect_tool", "Step 4 of 5 · Landing page", "Connect your coding tool",
                         landing["next_step"], "Connect a coding tool", "/settings/local")

    if builder_ready and f.get("idea_confirmed") and not f.get("build_review_pending"):
        # One click on /site?build=1 starts the server builder; that click is the approval. No plan needed.
        if f.get("page_build") == "ready":
            return None  # the private preview is saved; customers come next
        failed = f.get("page_build") == "failed"
        step = _step("site.build", "Step 4 of 5 · Landing page", "Build your landing page again" if failed else "Choose your next step",
                     "The last landing page build did not finish. " + ("Your idea and research are saved. " if f.get("research_report_ready") else "Your idea is saved. ") + "Open Build activity to review the checks, then request a fix on Website and choose Retry."
                     if failed else "Your connected coding tool builds a landing page from your saved idea. Open Website to see the checked private preview."
                     if local else "Supymem builds a landing page from your idea and research. You see a private preview before anything is public.",
                     "Build the landing page and explore customers", f"{base}/site?build=1")
        step["alternatives"] = [
            {"choice": "build", "label": "Build the landing page and explore customers", "href": f"{base}/site?build=1", "recommended": True},
            {"choice": "skip", "label": "Find customers first, skip the landing page", "href": f"/gtm?venture={venture_id}"},
        ]
        return step
    if f["plan_state"] not in EXECUTABLE_PLAN_STATES or f.get("build_review_pending"):
        return None  # research and plan first (the build implements the approved page contract); review comes later
    if not f.get("coding_tool"):
        return _step("site.connect_tool", "Step 4 of 5 · Landing page", "Connect Claude Code or Codex",
                     "Your landing page is built by your own coding tool. Connect one in the Supymem Mac app.",
                     "Connect a coding tool", f"{base}/connections")
    return _step("site.build", "Step 4 of 5 · Landing page", "Build your landing page",
                 "Claude Code or Codex builds a landing page just for this idea. A few minutes.",
                 "Build", f"{base}/site")


def _customers_step(venture_id: str) -> dict[str, Any]:
    return _step("gtm.outreach", "Step 5 of 5 · Customers", "Find your first customers",
                 "Draft outreach to the people your research points to. Nothing is sent without you. A plan is optional.",
                 "Find customers", f"/gtm?venture={venture_id}", "outreach.draft", {})


def _plan_optional(page_first: bool) -> bool:
    """Founder-facing journey with the server page builder: the execution plan never gates page or GTM."""
    from app.pagebuilder.api import enabled as server_builder_enabled

    return page_first and server_builder_enabled()


def founder_next_step(venture_id: str) -> dict[str, Any]:
    """The UI and automation use the same stage-aware decision."""
    return next_step(venture_id)


def _founder_resolve(venture_id: str, base: str, f: dict[str, Any]) -> dict[str, Any]:
    if f.get("paused"):
        return _resolve(venture_id, base, f)
    if f.get("first_call_booked"):
        return _step("gtm.results", "Goal reached", "Prepare your booked call and record what you learn",
                     "Review the conversation and record what you learn. This is your recorded outcome, not a delivery receipt.",
                     "Review results", f"/gtm?venture={venture_id}")
    context = f.get("startup_review")
    if context and not f.get("personal") and not f.get("active_jobs") and f.get("page_build") != "running":
        if not context["complete"]:
            return _step("venture.context", "Your next step", "Choose your next priority" if f.get("idea_confirmed") else
                         "A quick clarification" if context["question"] else "What matters most to you right now?",
                         "Review your saved answers before research or a build plan.", "Review my answers here", base + "/context", "startup.review")
        if not (f.get("journey_overrides") or {}).get("target") and not f.get("research_interrupted"):
            from app.ventures.startup_review import route
            destination = route(venture_id, context["priorities"])
            if destination["key"] == "research.start" and f.get("research_report_ready"):
                return _step("research.review", "Your priority", "Your research is ready", "Review the saved evidence, then tell me which question you want to test next.",
                             "Read research", base + "/research")
            if destination["key"] != "research.start" or f.get("dossier_state") not in {"running", "pending"}:
                return _step(destination["key"], "Your priority", destination["title"],
                             "Your goal is to book a first call with a potential customer or an industry person who can give insights. Nothing is sent without you."
                             if destination.get("goal") else "Your priorities are saved. Review the next step before starting any work.",
                             "Find customers" if destination.get("goal") else "Review next step", destination["href"])
    choices = f.get("journey_overrides") or {}
    if f.get("research_interrupted") and not f.get("personal") and "research" not in choices.get("skipped", {}):
        return _step("research.retry", "Research", "Research was interrupted",
                     ("Your saved research report is still available. Open Research to review it or choose Run research again."
                      if f.get("research_report_ready") else
                      "No usable research report was saved. Open Research and choose Run research again, or tell me to skip research for now."),
                     "Run research again", base + "/research")
    target = choices.get("target")
    if target in {"clarification", "validation"} and f.get("idea_confirmed"):
        return _founder_resolve(venture_id, base, {**f, "journey_overrides": {**choices, "target": None}})
    if target in {"research", "landing_page", "find_customers"} and not f.get("idea_confirmed"):
        return _founder_resolve(venture_id, base, {**f, "journey_overrides": {**choices, "target": None}})
    if target in {"landing_page", "find_customers"} and "research" not in choices.get("skipped", {}) and not f.get("research_report_ready"):
        return _step("research.start", "Research", "Research your idea", "Research is the recommended next check; you can skip it or jump ahead with confirmation.",
                     "Review research", f"{base}/research", "research.start")
    if target == "find_customers":
        return _customers_step(venture_id)
    if target == "landing_page":
        if f.get("landing_build") or f.get("published_site_url") or f.get("page_build") in {"running", "ready"}:
            return _founder_resolve(venture_id, base, {**f, "journey_overrides": {**choices, "target": None}})
        return _step("site.build", "Website", "Build your landing page", "Use your saved idea and website context. Review the cost and private preview first.",
                     "Open Website", f"{base}/site")
    if target == "research":
        if f.get("research_report_ready"):
            return _step("research.review", "Research", "Your research is ready", "Review the saved evidence, then choose what to do next.",
                         "Read research", f"{base}/research")
        if f.get("active_research") or f.get("dossier_state") in {"pending", "running"}:
            return _step("research.running", "Research", "Research is running", "Your saved assets stay in place.", "View research", f"{base}/research")
        return _step("research.start", "Research", "Research your idea", "Review the credit budget before research starts.",
                     "Review research", f"{base}/research", "research.start")
    if target == "validation":
        return _step("idea.review_validation", "Validate", "Review your idea", "Use the saved summary; missing answers stay assumptions.",
                     "Review idea", f"{base}/idea")
    if target == "clarification":
        return _step("idea.clarify", "Clarify", "Review your idea", "Continue from your saved idea and answers.", "Open Idea", f"{base}/idea")
    if "find_customers" in choices.get("skipped", {}):
        return _step("venture.review", "Your journey", "Choose what to do next", "Find customers is skipped by you. Your saved work remains available.",
                     "Review saved work", base)
    path = f.get("intake_path")
    personal = path == "personal_build" or f.get("personal", False)
    stage = f.get("founder_stage") or ("traction" if f.get("has_customers") else
        "page_live" if f.get("external_site") else "outreach" if path in {"gtm", "landing_gtm"} else None)
    if f.get("first_question_pending") and not f.get("idea_confirmed"):
        return _resolve(venture_id, base, {**f, "gtm_entry": False})
    if not personal and f.get("idea_confirmed") and f.get("page_build") == "running":
        return _page_step(venture_id, base, f)
    if f.get("active_research") or f.get("dossier_state") in {"pending", "running"}:
        return _step("research.running", "Research", "Researching your market…",
                     "Your market research is in progress. Your saved assets stay in place.",
                     "View research", f"{base}/research")
    if f.get("active_jobs"):
        return _step("work.running", "Supymem is working",
                     "Your company work is in progress", "Your saved assets stay in place while Supymem works.",
                     "View progress", f"{base}/now")
    landing = f.get("landing_build")
    live_url = (landing["page"].get("live_url") if landing else None) or f.get("published_site_url")
    if live_url and not personal:
        return {**_customers_step(venture_id), "title": "Your landing page is live, find your first customers", "site_url": live_url}
    if landing and not personal:
        if landing["page"]["available"]:
            return _step("site.ready", "Website", "Your landing page is ready",
                         landing["page"]["qa_note"] or "Visit the landing page, copy its link, or continue to Find customers.",
                         "Review landing page", landing["href"])
        if landing["state"] in {"failed", "cancelled", "rejected"}:
            return _step("site.retry", "Website", "Try the landing page again",
                         "No usable page was established. " + ("Your idea and research are saved. " if f.get("research_report_ready") else "Your idea is saved. ") + "Open Build activity to review the checks, then request a fix on Website and choose Retry.",
                         "Try again", f"{base}/site?build=1")
    if f.get("build_review_pending"):
        return _step("build.review", "Build", "Review your first build",
                     "The build finished and passed automatic checks. Review its evidence before accepting it or asking for changes.",
                     "Review landing page" if f.get("landing_review_href") else "Review the build", f.get("landing_review_href") or f"{base}/builds")
    if f.get("plan_id") and not f.get("plan_current"):
        f = {**f, "plan_state": None}  # saved history cannot authorize work for the new idea
    if (f.get("plan_state") in EXECUTABLE_PLAN_STATES and f.get("idea_confirmed")
            and f.get("research_report_ready") and (not f.get("plan_id") or f.get("plan_current"))):
        from app.planning.store import list_current_tasks
        if any(str(task.get("source") or "").startswith("approved_plan:") and task.get("runnable", True)
               and (not f.get("plan_id") or task["source"] == f"approved_plan:{f['plan_id']}")
               for task in list_current_tasks(venture_id)):
            # An explicitly approved task precedes optional page/customer choices.
            return _resolve(venture_id, base, {**f, "gtm_entry": False, "external_site": False})
    if (not personal and f.get("idea_confirmed") and f.get("research_report_ready")
            and f.get("accepted_current_landing") and not f.get("external_site")):
        if step := _page_step(venture_id, base, f):
            return step  # a current accepted private page still needs the founder's publication decision
    if not personal and f.get("has_customers") and f.get("idea_confirmed") and f.get("research_report_ready"):
        return _step("gtm.outreach", "Growth", "Grow what's already working",
                     "Your paying customers are demand evidence. Test the next repeatable acquisition channel.",
                     "Find customers", f"/gtm?venture={venture_id}", "outreach.draft", {})
    if not personal and f.get("external_site") and not f.get("site_audited") and not f.get("active_jobs"):
        return _step("site.audit", "Website", "Review your existing website",
                     "Check the public landing page, copy, sign-up path and measurement before deciding what to improve.",
                     "Audit your website", f"{base}/site")
    if not personal and stage in {"page_live", "outreach", "traction"}:
        if f.get("idea_confirmed") is False:
            return _resolve(venture_id, base, {**f, "gtm_entry": False})
        if not f.get("research_report_ready"):
            running = f.get("dossier_state") in {"pending", "running"}
            return _step("research.running" if running else "research.start", "Research",
                         "Research is running" if running else "Research your market",
                         "Compare alternatives and check the audience using your existing assets.",
                         "View research" if running else "Review research", f"{base}/research",
                         None if running else "research.start" if f.get("idea_confirmed") else None)
        if stage == "traction":
            return _step("experiment.review" if f.get("exp_review") else "experiment.plan", "Learn",
                         "Choose your next growth experiment", "Review feedback and retention, then pick one measurable experiment for the week.",
                         "Review experiments", f"{base}/experiments")
        return _customers_step(venture_id)
    if path == "research" and f.get("idea_confirmed"):
        state = f.get("dossier_state")
        if f.get("research_report_ready"):
            return _step("research.review", "Research", "Your research is ready",
                         "Read the evidence and verdict, then tell Charlie what you want to do next.",
                         "Read research", f"{base}/research")
        if state in {"pending", "running"} or f.get("active_jobs"):
            return _step("research.running", "Research", "Research is running",
                         "I'll compare alternatives and test the assumptions that matter.",
                         "View progress", f"{base}/research")
        return _step("research.start", "Research", "Research your idea",
                     "Review the credit budget before starting research.",
                     "Start research", f"{base}/research", "research.start")
    if personal and path:
        f = {**f, "personal": True, "gtm_entry": False}
    if not personal and stage in {"idea", "building"} and f.get("idea_confirmed") and not f.get("research_report_ready"):
        running = f.get("active_jobs") or f.get("dossier_state") in {"pending", "running"}
        return _step("research.running" if running else "research.start", "Research",
                     "Research is running" if running else "Research your idea",
                     "Compare alternatives and check the assumptions before choosing your next step.",
                     "View research" if running else "Review research", f"{base}/research", None if running else "research.start")
    return _resolve(venture_id, base, f, path != "research")


def _resolve(venture_id: str, base: str, f: dict[str, Any], page_first: bool = False) -> dict[str, Any]:
    if f.get("paused"):  # nothing else can start, so this is the only honest next step
        return _step("venture.paused", "Paused", "Venture paused",
                     "Nothing new will start until you resume. Your saved work is kept.",
                     "Resume", f"{base}/now", "resume")
    if f["gtm_entry"]:  # skipping to go-to-market never demands a build or a publish
        return _step("gtm.outreach", "Go to market", "Draft your first outreach",
                     "Three short emails for your review. Nothing is sent.",
                     "Draft outreach emails", f"/gtm?venture={venture_id}", "outreach.draft", {})
    if f.get("first_question_pending") and not f["idea_confirmed"]:
        return _step("work.running", "Supymem is working", "Preparing your first question",
                     "I'm reading your idea and will ask the one thing I most need to understand.",
                     "View live progress", f"{base}/now")
    if f["active_jobs"]:
        if page_first and f.get("page_build") == "running" and f["idea_confirmed"] and not f["personal"]:
            step = _page_step(venture_id, base, f)
            if step:
                return step  # the page build's own progress lives on the Website page
        return _step("work.running", "Supymem is working", "Your company work is in progress",
                     "The work continues securely in the hosted service even if you close this window.",
                     "View live progress", f"{base}/now")
    if f["personal"]:
        if not f["idea_confirmed"]:
            if f["idea_state"] in {"reviewing", "validating"}:
                return _step("idea.review_validation", "Project review", "Review your saved personal project",
                             "Check the idea and answers before confirming. This step does not run market validation.",
                             "Review project", f"{base}/idea")
            return _step("idea.clarify", "Clarify your project", "Answer the next project question",
                         "Define the smallest useful version for your own use.",
                         "Continue clarification", f"{base}/idea")
        return _step("build.first", "Build your project", "Review a local build",
                     "Prepare the smallest useful version in your local workspace. Review the task, device access and credit limit before approving it.",
                     "Review local build", f"{base}/idea")
    if not f["idea_confirmed"] and f.get("research_report_ready"):
        # Research can only start on a confirmed idea, so a finished dossier plus a live site proves
        # the venture is past the idea gate (a later idea revision reopened it); do not send it back.
        from app.ventures.state_brief import _website_section

        f["idea_confirmed"] = bool(_website_section(f["workspace_id"], venture_id)["live"])
    if not f["idea_confirmed"]:
        if f.get("validation_failed") and not f.get("question_pending"):
            return _step("idea.validation_failed", "Step 2 of 5 · Validate", "Validation did not finish",
                         "Your answers are saved. Run the validation again to continue.",
                         "Retry validation", f"{base}/idea", "job.retry", {"kind": "idea.validate"})
        if f["idea_state"] == "validating" and not f.get("validation_done") and not f.get("question_pending"):
            return _step("idea.validating", "Step 2 of 5 · Validate", "Your idea is being validated",
                         "Supymem is challenging the assumptions that matter before deeper research begins.",
                         "Open validation", f"{base}/idea")
        if f["idea_state"] == "reviewing" and not f.get("validation_done"):
            # Proposal awaiting the founder's explicit "Looks right" in chat (same key: autopilot maps it).
            return _step("idea.review_validation", "Step 2 of 5 · Check", "Check your idea summary",
                         "Your summary is in chat. Tap Looks right to continue, or Edit to change it.",
                         "Review idea", f"{base}?chat=1")
        if f["idea_state"] == "reviewing" or f.get("validation_done"):  # finished run awaiting the founder
            return _step("idea.review_validation", "Step 2 of 5 · Validate", "Review your idea validation",
                         "Supymem has finished clarification. Review the validation before deeper research begins.",
                         "Open validation", f"{base}/idea")
        return _step("idea.clarify", "Step 1 of 5 · Clarify", "Answer the next question about your idea",
                     "Supymem turns your starting idea into a focused MVP before deeper research or paid work begins.",
                     "Continue clarification", f"{base}/idea")
    if f.get("exp_review"):
        return _step("experiment.review", "Learn", "Review what your experiments taught you",
                     "The tests for this direction did not hold up. Look at the evidence and pick the next direction, or keep the idea and test it differently.",
                     "Review what we learned", f"{base}/experiments")
    if page_first and not f["personal"] and not f.get("external_site"):  # founder-facing dashboard passes True
        # Page first: the founder's own Claude Code / Codex builds a page for this idea (no template pages).
        # The build implements the approved plan's landing contract, so research and plan still come before it.
        step = _page_step(venture_id, base, f)
        if step:
            return step
    if f["plan_state"] is None:
        d = f["dossier_state"]
        if f.get("research_report_ready") and _plan_optional(page_first):
            return _customers_step(venture_id)
        if f.get("research_report_ready"):
            return _step("plan.generate", "Step 3 of 5 · Plan", "Generate your execution plan",
                         "Research is saved. Estimate and generate a small plan aimed at real product use, then review it before approving.",
                         "Continue to plan", f"{base}/plan", "plan.generate")
        if d in {"pending", "running"}:
            return _step("research.running", "Step 2 of 5 · Validate", "Research is running",
                         "Supymem is comparing alternatives and testing the assumptions that matter.",
                         "View research", f"{base}/research")
        from app.chat.execute import research_needs_start

        if d is None and research_needs_start(venture_id):
            return _step("research.start", "Step 2 of 5 · Validate", "Start deep research",
                         "Compare alternatives and test the assumptions that matter before planning. Credits apply.",
                         "Start research", f"{base}/research", "research.start")
        return _step("research.review", "Step 2 of 5 · Validate", "Review the research and shape the MVP",
                     "Compare alternatives, test the assumptions that matter, then approve a small plan.",
                     "Continue to research", f"{base}/research")
    if f["plan_state"] not in EXECUTABLE_PLAN_STATES and _plan_optional(page_first):
        return _customers_step(venture_id)  # an unapproved or failed plan never loops the founder back
    if f["plan_state"] == "draft" and not f.get("plan_has_proposal"):
        # active_jobs (above) already covers a compile still running.
        return _step("plan.retry", "Step 3 of 5 · Plan", "Planning did not finish",
                     "The last plan attempt did not produce a usable plan. Your research and answers are saved; generate the plan again.",
                     "Retry plan", f"{base}/plan", "plan.generate")
    if f["plan_state"] not in EXECUTABLE_PLAN_STATES:
        return _step("plan.review", "Step 3 of 5 · Plan", "Review the execution plan",
                     "Confirm the scope, milestones, resources, and credit estimate before Supymem starts building.",
                     "Continue to plan", f"{base}/plan", "plan.approve")
    if f.get("build_review_pending") and not f.get("external_site"):
        return _step("build.review", "Step 4 of 5 · Build", "Review your first build",
                     "The build finished and passed automatic checks, which do not judge the content. Look at it and accept it or ask for changes; only you can do this.",
                     "Review landing page" if f.get("landing_review_href") else "Review the build", f.get("landing_review_href") or f"{base}/builds")
    from app.planning.store import list_current_tasks

    own_site = f.get("external_site")  # a site the founder built themselves replaces the page build
    task = next((t for t in list_current_tasks(venture_id)
                 if str(t.get("source") or "").startswith("approved_plan:") and t.get("runnable", True)
                 and (not f.get("plan_id") or t["source"] == f"approved_plan:{f['plan_id']}")
                 and (not f.get("plan_id") or f.get("plan_current"))
                 and not (own_site and "capability:build.run" in (t.get("tags") or []))), None)
    exp = f.get("exp_run")
    if exp and task and _is_test(task["title"]):  # the plan's next item is itself a test
        return _experiment_run_step(base, exp)
    if task and "capability:build.run" in (task.get("tags") or []):
        plan_task = next((str(t)[len("plan_task:"):] for t in task.get("tags") or [] if str(t).startswith("plan_task:")), "")
        return _step("work.next_task", "Step 4 of 5 · Build", str(task["title"])[:120],
                     "The next build step from your approved plan. I will show the exact credit ceiling before anything starts.",
                     "Review and start the build", f"{base}/builds", "build.prepare",
                     {"plan_task_id": plan_task} if plan_task else {})
    if task:
        return _step("work.next_task", "Step 4 of 5 · Build", str(task["title"])[:120],
                     "The next item from your approved plan. Confirm to start it; nothing is published.",
                     "Start this task", f"{base}/tasks", "work.start", {"task_id": task["id"]})
    if (f["products"] < 1 or not f["accepted_build"]) and not own_site:
        return _step("build.first", "Step 4 of 5 · Build", "Build and verify the first product milestone",
                     "Run the smallest release through independent QA, then review its evidence before accepting it.",
                     "Open builds and QA", f"{base}/builds")
    if f["product_attempt"] and not f["accepted_product"]:
        return _step("build.finish_product", "Step 4 of 5 · Build", "Finish and verify the application",
                     "The application build still needs an accepted result and functional testing.",
                     "Review application builds", f"{base}/builds")
    if f["product_attempt"]:
        return _step("build.verify_product", "Step 5 of 5 · Application review",
                     "Verify application hosting and customer workflows",
                     "An accepted application build still needs a live runtime and real account tests.",
                     "Review application evidence", f"{base}/builds")
    from app.ventures.state_brief import _website_section

    site = _website_section(f["workspace_id"], venture_id)
    # An accepted build that is not published comes before any experiment: publishing is what the founder accepted it for.
    if not site["live"]:
        return _step("site.publish", "Step 5 of 5 · Launch decision", "Decide when to publish your accepted product",
                     "The product passed QA and you accepted the milestone. It remains private until you choose to publish it.",
                     "Open the private product", f"{base}/site",
                     "site.publish" if site["waitlist_enabled"] else "waitlist.enable")
    if exp:
        return _experiment_run_step(base, exp)
    if site["waitlist_enabled"]:
        return _step("gtm.outreach", "Step 5 of 5 · Launch", "Choose a first pilot cohort",
                     "The landing page and waitlist are live. Pick one narrow audience and review the wording before any outreach.",
                     "Open go to market", f"/gtm?venture={venture_id}")
    return _step("gtm.diagnose", "Step 5 of 5 · Launch", "Review the live product and find your first users",
                 "Measure real usage and diagnose the bottleneck to customers.",
                 "Open go to market", f"/gtm?venture={venture_id}", "gtm.diagnosis")
