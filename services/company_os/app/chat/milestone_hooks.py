"""Milestone copy for research, plan and first-look completion (posting itself: chat/milestones.py)."""

from __future__ import annotations

from app.chat.hosting import OFFER as HOST_OFFER
from app.chat.milestones import link, post_milestone
from app.chat.plan_chain import button
from app.core.local_mode import is_local


def started_reply(reply: dict, job_id: str | None = None) -> dict:
    job_id = job_id or reply.get("job_id") or reply.get("run_id")
    if (not isinstance(job_id, str) or not job_id or reply.get("action_started") is False
            or reply.get("kind") in {"confirmation", "needs_you", "site_preview"}):
        return reply
    marker = {"job_started": {"job_id": job_id}}
    citations = reply.get("citations") or []
    return reply if marker in citations else {**reply, "citations": [*citations, marker]}


def started_job(scope, kind: str, citations: list | None):
    from app.db.models import Job
    from app.state import db
    import json

    if kind in {"confirmation", "needs_you", "site_preview"}:
        return None
    candidates = []
    for name in ("job_started", "gtm_search_progress", "browse_progress", "confirmed_action"):
        for item in citations or []:
            if not isinstance(item, dict):
                continue
            value = item.get(name)
            if not isinstance(value, dict):
                continue
            if name == "confirmed_action" and (value.get("state") != "started" or value.get("action") == "job.retry"):
                continue
            # Later browse updates carry the same job: only the queued row is its start.
            if name == "browse_progress" and value.get("state") != "queued":
                continue
            if name.endswith("_progress") and not kind.endswith("_progress"):
                continue
            job_id = value.get("job_id") or value.get("run_id")
            if isinstance(job_id, str) and job_id:
                candidates.append(job_id)
        if candidates:
            break
    if len(set(candidates)) != 1:
        return None
    with db()() as session:
        job = session.get(Job, candidates[0])
        if job is None or (job.venture_id, job.workspace_id) != (scope.venture_id, scope.workspace_id):
            return None
        try:
            payload = json.loads(job.payload_json)
        except (TypeError, ValueError):
            return None
        conversation = payload.get("_chat_conversation") if isinstance(payload, dict) else None
        if conversation is not None and (not isinstance(conversation, dict)
                or conversation.get("conversation_id") != scope.id
                or conversation.get("actor_id") != scope.actor_id):
            return None
        return job


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def research_report_link(venture_id: str, dossier_id: str | None = None) -> dict:
    suffix = f"?dossier={dossier_id}" if dossier_id else ""
    return {**link("Open full report" if dossier_id else "Open Research", f"/v/{venture_id}/research{suffix}"),
            "kind": "research_report"}


def research_status(dossier: dict, *, code: str = "") -> str:
    from app.chat.blockers import error_copy
    from app.chat.failed_jobs import clean_code

    state, summary = dossier.get("state"), dossier.get("summary") or {}
    code = clean_code(code or summary.get("error_code"))
    if state == "complete" and not code:
        return "Your research is ready. Open the full report to review the evidence."
    if state in {"queued", "pending", "scheduled", "running", "leased", "retry_wait"} and not code:
        return "Your research is underway. You can follow its progress in chat."
    if state == "outdated" and not code:
        return "This report covers an earlier idea. Open Research and choose Run research again for your current idea."
    if code == "research_cancelled" or state == "cancelled":
        return "Your research stopped as you asked. Open Research to review any saved sources or run it again."
    if is_local() and (next_step := (summary.get("search_notice") or {}).get("next_step")):
        return next_step
    if is_local() and state == "partial" and (next_step := summary.get("review_next_step")):
        return next_step
    limited = state == "partial" and not code
    sources_only = limited and summary.get("review_kind") == "retrieved_sources_only"
    lead = ("You have some findings, but your research did not answer every question." if limited
            else "Your research did not finish.")
    if sources_only:
        lead = "Your research did not find enough evidence for a full review."
    if is_local():
        from app.core.local_secrets import read_settings
        from app.research.local_providers import setup_reply

        try:
            if code and (setup := setup_reply()):
                return f"{lead} {setup['body_md']}"
            builtin = sources_only and read_settings("search")["values"].get("provider", "builtin") == "builtin"
        except (OSError, ValueError):
            return f"{lead} Check your search and AI connections in Settings > Keys and connections."
        if code in {"research_byok_unavailable", "research_credentials_unavailable", "research_provider_offline"}:
            return f"{lead} Check your model and API key in Settings > Keys and connections > Your AI provider."
        if builtin:
            return f"{lead} For more sources, choose Tavily or Exa and save its API key in Settings > Keys and connections > Search."
    reason = {"research_invalid_output": "I could not verify the review against your sources.",
              "research_call_budget_exhausted": "Your research stopped before all sources could be checked.",
              "research_cost_budget_exhausted": "Your research stopped before all sources could be checked."}.get(code) or error_copy(code)
    if code and reason:
        lead += f" {reason}"
    return (f"{lead} Open the full report to review your sources and what to check next." if limited
            else f"{lead} Open Research to see what was saved, then choose Run research again.")


def _local_research_result(venture_id: str, dossier_id: str, *, code: str = "") -> dict:
    from app.chat.recovery import log_founder_exception
    from app.research.deep import safe_source_url
    from app.research.service import get_dossier

    try:
        dossier = get_dossier(dossier_id)
    except KeyError:
        dossier = {}
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "milestone.research_report")
        dossier = {}
    if dossier.get("venture_id") != venture_id:
        dossier = {}
    if dossier.get("state") == "outdated":
        return {"body_md": research_status(dossier), "actions": [research_report_link(venture_id)], "citations": []}
    report = (dossier.get("summary") or {}).get("founder_report") or {}
    lines, citations = [research_status(dossier, code=code)], {}

    def cite(entry):
        if url := safe_source_url(entry.get("url")):
            return citations.setdefault(url, {"label": f"Source {len(citations) + 1}", "href": url})["label"]
        return None

    findings = []
    for entry in [*report.get("findings", []), *report.get("contradictions", [])]:
        if entry.get("finding") and (source := cite(entry)):
            findings.append(f"- {entry['finding']} ({source})")
        if len(findings) == 3:
            break
    if findings:
        lines.extend(["", *findings])
    competitors = (report.get("competitor_grid") or {}).get("rows") or (
        (report.get("dimensions") or {}).get("competition") or {}).get("support", [])
    names = []
    for entry in competitors[:5]:
        if entry.get("name") and (source := cite(entry)):
            names.append(f"{entry['name']} ({source})")
    if names:
        lines.extend(["", "Competitors you can review: " + "; ".join(names) + "."])
    actions = [research_report_link(venture_id, dossier.get("id"))]
    if settings := ((dossier.get("summary") or {}).get("search_notice") or {}).get("settings_href"):
        actions.append(link("Keys and connections", settings))
    return {"body_md": "\n".join(lines), "actions": actions,
            "citations": list(citations.values())}


def research_done(payload: dict) -> None:
    try:
        from app.research import execution as ex
        from app.research.service import get_dossier, top_prior_art_lesson

        if is_local():
            vid = payload["venture_id"]
            did = ex.identifier(payload["_job_id"])
            stopped = ex.cancel_requested(payload)
            post_milestone(vid, key=f"research_{'stopped' if stopped else 'done'}:{did}",
                           kind="text" if stopped else "research_ready", requested=True,
                           **_local_research_result(vid, did, code="research_cancelled" if stopped else ""))
            return
        if ex.cancel_requested(payload):
            vid = payload["venture_id"]
            from app.chat.build_path import research_card
            prepared = research_card(vid)
            body = "Research stopped as you asked. The sources found so far are saved. Review them or retry research."
            if prepared["kind"] == "needs_you":
                body += "\n\n" + prepared["body_md"]
            post_milestone(vid, key=f"research_stopped:{ex.identifier(payload['_job_id'])}", kind="text",
                           body_md=body,
                           actions=[link("Read full report", f"/v/{vid}/research", "secondary"),
                                    prepared["actions"][0]])
            return
        dossier = get_dossier(ex.identifier(payload["_job_id"]))
        v = dossier["summary"]["verdict"]
        n = v["source_count"]
        if n == 0:
            body = "Research finished but found no usable sources." + (f" {v['why_not'][0]}" if v.get("why_not") else "")
        else:
            body = "Research is ready.\n" + "\n".join(f"- {w}" for w in v["why"][:3])
        lesson = top_prior_art_lesson(dossier)
        if lesson:
            body += "\n" + lesson
        vid = payload["venture_id"]
        from app.pagebuilder.api import enabled as server_builder_enabled
        from app.ventures.next_step import _facts

        facts = _facts(vid)
        actions = [link("Read full report", f"/v/{vid}/research", "secondary")]
        citations = []
        if facts["personal"]:
            actions.append(link("Review local build", f"/v/{vid}/idea", "secondary"))
        elif not facts["idea_confirmed"]:
            actions.append(link("Review your current idea", f"/v/{vid}/idea", "secondary"))
        elif server_builder_enabled():
            citations = [{"question": True}]
            body += "\nChoose a landing page or customers first. Recommended: build the landing page."
            actions += [link("Build the landing page and explore customers", f"/v/{vid}/site?build=1", "primary"),
                link("Find customers first, skip the landing page", f"/gtm?venture={vid}&skip_page=1", "primary")]
        else:
            actions.append(button("Continue to plan", "plan.generate", "secondary"))
        post_milestone(vid, key=f"research_done:{ex.identifier(payload['_job_id'])}", body_md=body, kind="research_ready",
                       actions=actions, citations=citations)
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, payload.get("venture_id"), "milestone.research")


def plan_done(payload: dict) -> None:
    vid = payload["venture_id"]
    body = "Your plan is ready for review. Background preparation may already be running; review the plan before approving its execution."
    key = f"plan_done:{payload.get('_job_id')}"
    try:
        from app.planning import execution as ex
        from app.planning.compiler import get_plan

        pid = ex.identifier(payload["_job_id"])
        key = f"plan_done:{pid}"
        plan = get_plan(pid)
        from app.core.local_mode import is_local
        if is_local():
            body = ("Your provisional plan is ready. Open Plan and review the missing research before choosing work to run."
                if plan.get("provisional") else "Your plan is ready. Open Plan to review it and choose which tasks to run. No tasks from this plan have started.")
        if plan.get("generation_state") == "failed" and plan.get("generation_recovery"):
            recovery = plan["generation_recovery"]
            post_milestone(vid, key=key, body_md=recovery["message"], kind="needs_you",
                           actions=[link(recovery["label"], recovery["href"])])
            return
        n = len((plan.get("proposal") or {}).get("milestones") or [])
        body = f"Your plan is ready: {_plural(n, 'milestone')}. "
        body += " Background preparation may already be running; review the plan before approving its execution."
    except Exception as exc:  # noqa: BLE001 - the milestone must still post with generic copy
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, vid, "milestone.plan_details")
    post_milestone(vid, key=key, body_md=body, kind="plan_ready",
                   actions=[link("Review plan", f"/v/{vid}/plan"), button("Review and approve", "plan.approve", "secondary")])


def failed(session, venture_id: str, job_kind: str, job_id: str, code: str = "") -> None:
    """Post a terminal job failure once, including before the caller commits."""
    from app.chat import failed_jobs
    from app.chat.milestones import _scoped
    from app.db.models import ChatMessage

    kind = job_kind if job_kind in failed_jobs.KINDS else "plan.compile"
    path = failed_jobs.KINDS[kind][2]
    key = f"{path}_failed:{job_id}"
    scoped = _scoped(venture_id, key)
    if any(isinstance(row, ChatMessage) and row.idempotency_key == scoped for row in session.new):
        return
    actions = failed_jobs.failure_actions(venture_id, kind, code)
    if is_local() and kind == "research.deep":
        from app.research.execution import identifier

        reply = _local_research_result(venture_id, identifier(job_id), code=code)
        reply["actions"].extend(action for action in actions if not str(action.get("href", "")).endswith("/research"))
    else:
        reply = {"body_md": failed_jobs.body(kind, code), "actions": actions}
    post_milestone(venture_id, key=key, kind=f"{path}_failed", session=session, requested=is_local(),
        failure={"step": kind, "code": code, "job_id": job_id}, **reply)


# job kind -> (what the founder calls it, chat message kind, recovery step, page to open)
OTHER_JOBS = {
    "content.topics": ("Finding content ideas", "content_failed", "content.draft", "/content?venture={v}"),
    "content.draft": ("Drafting your content", "content_failed", "content.draft", "/content?venture={v}"),
    "gtm.god.run": ("Your go-to-market diagnosis", "gtm_failed", "gtm.diagnosis", "/gtm?venture={v}"),
    "import.analyze": ("Importing your project", "import_failed", "import.analyze", "/v/{v}/idea"),
    "experiment.evaluate": ("Reviewing your experiment", "experiment_failed", "experiment.evaluate", "/v/{v}/experiments"),
}


def content_failed(session, venture_id: str, job_kind: str, job_id: str, code: str = "") -> None:
    """A job outside the main journey died (content, GTM, import, experiments): it used to stop silently."""
    from app.core.local_mode import is_local
    from app.chat.blockers import error_copy
    from app.chat.failed_jobs import clean_code

    if job_kind == "gtm.journey.prepare":
        from app.chat.customers import render
        from app.gtm.journey import _row, _json
        row = _row(session, venture_id)
        data = _json(row.provenance_json) if row else {}
        if data.get("job_id") != job_id:
            return
        reply = render(venture_id, {**data, "state": "failed", "error_code": code})
        post_milestone(venture_id, key=f"gtm_search_failed:{job_id}", kind=reply["kind"], session=session,
            body_md=reply["body_md"], actions=reply["actions"], citations=reply.get("citations"), requested=True,
            failure={"step": job_kind, "code": code, "job_id": job_id})
        return
    what, kind, step, href = OTHER_JOBS[job_kind]
    reason = error_copy(clean_code(code)) or "It stopped with an error."
    post_milestone(venture_id, key=f"{kind}:{job_id}", kind=kind, session=session, requested=is_local(),
        body_md=f"{what} did not finish. {reason} Nothing was posted or published, and your saved work is kept.",
        actions=[link("Open it", href.format(v=venture_id), "secondary")],
        failure={"step": step, "code": code, "job_id": job_id})


def first_look_ready(venture_id: str, version_id: str, copy: dict | None = None) -> None:
    from app.chat.site_actions import headline_summary, rewrite_card, waitlist_card

    lead = f"Your landing page draft is ready: {headline_summary(copy)}. " if copy and copy.get("headline") \
        else "Your landing page draft is ready. "
    post_milestone(venture_id, key=f"first_look:{version_id}", kind="first_look_ready",
        body_md=lead + "It is a preview only and has not been published. Next: open the waitlist so the landing page can collect "
                       "email requests (interest, not proof of demand).",
        actions=[waitlist_card(venture_id), rewrite_card("Tighten and improve the copy", label="Rewrite"),
                 link("Open preview", f"/v/{venture_id}/site", "secondary")])


def waitlist_enabled(venture_id: str) -> None:
    """Posted when the waitlist is switched on from anywhere but chat (chat replies in-thread)."""
    from app.chat.site_actions import after_waitlist

    nxt, actions = after_waitlist(venture_id)
    post_milestone(venture_id, key="waitlist_on", kind="waitlist_on",
        body_md=f"Waitlist is on. {nxt} Sign-ups show interest, not proof of demand.",
        actions=actions)


def _build_href(run) -> str:
    import json

    spec = json.loads(run.spec or "{}")
    landing = (spec.get("artifact_profile") == "static_landing_v1"
        or getattr(run, "trigger", None) == "pagebuilder_source_qa"
        or spec.get("inputs", {}).get("milestone", {}).get("kind") == "landing")
    return f"/v/{run.venture_id}/{'site' if landing else 'builds'}?app={run.app_id}&run={run.id}#build-review"


def _is_landing(run) -> bool:
    import json

    return json.loads(run.spec or "{}").get("artifact_profile") != "founder_product_v1"


def build_ready(session, run, qa, revision: bool = False) -> None:
    """QA passed on a managed build: tell the founder it is ready to review (joins the caller's transaction)."""
    what = ("revised " if revision else "") + ("landing page" if _is_landing(run) else "product")
    total = (qa.passed or 0) + (qa.failed or 0) + (qa.unsupported or 0)
    href = _build_href(run)
    post_milestone(run.venture_id, key=f"build_ready:{run.id}", kind="build_ready", session=session,
        body_md=f"Your {what} build is ready. QA passed {qa.passed}/{total}. Review the evidence, then accept it.",
        actions=[link("Review build", href), {"label": "Accept", "action": "build.accept", "style": "secondary"}])


def build_ended(session, run) -> None:
    """A managed build failed before passing QA: say so plainly, once per run (joins the caller's transaction)."""
    what = "landing page" if _is_landing(run) else "product"
    from app.planning.publication_evidence import landing_build_result
    import json
    landing = landing_build_result(session, run, json.loads(run.spec or "{}"))
    if landing and landing["available"]:
        post_milestone(run.venture_id, key=f"build_failed:{run.id}", kind="build_ready", session=session,
            body_md="Your landing page is ready. " + (landing["qa_note"] or "Review the saved page."),
            actions=[link("Visit landing page", landing["live_url"]) if landing["live_url"] else link("Review page", _build_href(run)),
                     link("Build a new version", f"/v/{run.venture_id}/site?build=1", "secondary"),
                     link("Find customers", f"/gtm?venture={run.venture_id}", "secondary")])
        return
    post_milestone(run.venture_id, key=f"build_failed:{run.id}", kind="build_failed", session=session,
        body_md=f"Your {what} build ended before it passed QA. No usable page was established. "
                "Your saved work is safe; try again or continue to Find customers." if landing is not None else
                f"Your {what} build ended before it passed QA, so there is nothing to review or publish from it. Open the build to see what happened.",
        actions=[link("Try again", f"/v/{run.venture_id}/site?build=1"), link("Review saved result", _build_href(run), "secondary")]
                if landing is not None else [link("Open build", _build_href(run))],
        failure={"step": "build", "code": getattr(run, "error_code", "") or "", "job_id": run.id})


def deployment_live(venture_id: str, deployment_id: str, url: str | None) -> None:
    """Once per deployment: the page is live (posted after the deploy, never promised before it)."""
    where = f" at {url}" if url else ""
    post_milestone(venture_id, key=f"deploy_live:{deployment_id}", kind="site_live",
        body_md=f"Your landing page is live{where}. People who sign up on it appear in your waitlist.",
        actions=[{"label": "Open your page", "href": url, "style": "primary"} if url else link("Open site", f"/v/{venture_id}/site")])


def deployment_failed(venture_id: str, deployment_id: str, code: str = "", *, session=None) -> None:
    """Once per deployment: publishing did not finish, in plain words, with where to retry."""
    from app.chat.site_actions import plain_hosting

    body = (plain_hosting(code) if code == "hosting_public_state_uncertain"
            else f"Publishing did not finish. {plain_hosting(code)} Open Website to review the current public status.")
    post_milestone(venture_id, key=f"deploy_failed:{deployment_id}", kind="site_publish_failed", session=session,
        body_md=body,
        actions=[link("Open Website", f"/v/{venture_id}/site")],
        failure={"step": "publish", "code": code, "job_id": deployment_id})


def deployment_resolved(venture_id: str, deployment_id: str, *, session=None) -> None:
    from app.chat.site_actions import plain_hosting

    code = "hosting_publication_interrupted"
    post_milestone(venture_id, key=f"deploy_resolved:{deployment_id}", kind="site_publish_failed", session=session,
        body_md=plain_hosting(code), actions=[link("Open Website", f"/v/{venture_id}/site")],
        failure={"step": "publish", "code": code, "job_id": deployment_id})


def build_accepted(session, run, revision: bool = False) -> None:
    from app.chat.site_actions import publish_card, waitlist_card, _waitlist_state

    if _is_landing(run):
        state = _waitlist_state(run.venture_id)  # publish needs the waitlist on and tested: offer that first
        if not state.get("enabled"):
            body, actions = "Accepted. Next: turn on the waitlist, test it, then publish. Nothing goes public until you confirm.", [waitlist_card(run.venture_id, label="Turn on waitlist")]
        elif not state.get("submission_verified"):
            body, actions = ("Accepted. Next: test the waitlist, then publish. Nothing goes public until you confirm.",
                             [{"label": "Test waitlist", "action": "waitlist.test", "style": "primary"}])
        else:
            body = ("Accepted. Next: publish the updated landing page." if revision else "Accepted. Next: publish the landing page.") + " It goes public only after you confirm."
            actions = [publish_card(run.venture_id)]
        post_milestone(run.venture_id, key=f"build_accepted:{run.id}", kind="build_accepted", session=session,
            body_md=body, actions=actions)
    else:
        post_milestone(run.venture_id, key=f"build_accepted:{run.id}", kind="build_accepted", session=session,
            body_md="Accepted. Your product build is saved as the reviewed version.",
            actions=[link("Open builds", f"/v/{run.venture_id}/builds", "secondary"), HOST_OFFER])
