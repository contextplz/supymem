"""Founder progress derived from the saved plan and operation receipts."""

LABELS = {"planning": "Planning the search", "searching": "Searching public sources",
          "judging": "Checking fit", "contacting": "Finding contact routes", "drafting": "Writing drafts"}


def failure_message(code: str) -> str:
    from app.core.local_mode import is_local

    if code == "public_search_unavailable" and is_local():
        from app.research.local_providers import SEARCH_UNAVAILABLE
        return SEARCH_UNAVAILABLE
    if code == "search_engine_limited":
        from app.research.local_providers import SEARCH_LIMITED
        return SEARCH_LIMITED
    if code == "discovery_boundary_blocked":
        return "Your search could not finish because some public pages could not be checked. Your saved work is kept. Choose Search again to try a new search. Nothing was sent."
    reasons = {
        "gtm_problem_confirmation_changed": "Your saved idea was updated after you confirmed this search.",
        "gtm_prepare_authority_revoked": "Your permission to search this workspace changed.",
        "gtm_processing_policy_required": "Your workspace outreach policy needs review.",
        "gtm_discovery_opt_in_required": "Your search confirmation is no longer available.",
        "gtm_recruitment_opt_in_required": "Your community search needs your confirmation.",
        "gtm_call_budget_exhausted": "The search reached its call limit before it finished.",
        "gtm_search_timed_out": "The search did not finish within 30 minutes.",
        "gtm_search_worker_missing": "The saved search no longer has a worker job.",
        "gtm_search_result_missing": "The worker finished without saving a complete search result.",
        "gtm_status_unavailable": "Your saved search could not be read right now.",
        "gtm_invalid_input": "Your search details could not be saved. Check your audience and problem, then retry.",
        "gtm_invalid_auto_search": "Your automatic search settings are invalid. Choose a positive contact target and review your limits.",
        "gtm_preparation_in_progress": "Your earlier search is still running. Check its progress before retrying.",
        "public_search_unavailable": "Your public search could not respond right now. Retry search in a moment.",
        "public_search_blocked": "Your workspace public search integration is unavailable.",
        "public_sources_unavailable": "The public sources could not be opened to check fit.",
        "no_public_candidates": "No usable public matches remain for your audience and location.",
        "credits_low": "You have reached your usage limit. Open Settings to get more, then retry.",
        "insufficient_credits": "You have reached your usage limit. Open Settings to get more, then retry.",
        "gtm_auto_search_paused": "You paused automatic customer searches.",
        "gtm_auto_search_credit_ceiling": "The automatic search reached your per-search usage limit.",
        "job_outcome_uncertain": "The worker stopped before it could confirm the result.",
        "cancelled": "The search was cancelled.",
    }
    from app.chat.blockers import error_copy
    reason = reasons.get(code) or error_copy(code) or "The search worker stopped before saving a complete result."
    if code == "public_sources_unavailable" and is_local():
        reason += " Choose Retry in Find customers, or try a different public website."
    blocked = code in {"gtm_status_unavailable", "gtm_invalid_input", "gtm_invalid_auto_search", "gtm_preparation_in_progress"}
    ending = "Nothing was sent by this request." if blocked else "Nothing was sent."
    return ("" if blocked else "Your customer search stopped. ") + reason + " Your saved work is kept. " + ending


def normalize_search_reply(reply: dict) -> dict:
    import json

    actions = reply.get("actions") or []
    failures = {failure_message(action["payload"].get("error_code") or "gtm_preparation_failed")
        for action in actions if isinstance(action, dict) and action.get("action") == "recovery.alternative"
        and isinstance(action.get("payload"), dict) and action["payload"].get("step") == "gtm.journey.prepare"}
    if not failures and not any(isinstance(action, dict) and action.get("action") == "gtm.find" for action in actions):
        return reply
    seen, unique = set(), []
    for action in actions:
        identity = action.get("id") or json.dumps({key: value for key, value in action.items()
            if key not in {"label", "style"}}, sort_keys=True) if isinstance(action, dict) else None
        if identity is None or identity not in seen:
            unique.append(action)
            seen.add(identity)

    def once(body):
        for failure in failures:
            before, found, after = body.partition(failure)
            if found and failure in after:
                body = before + found + after.replace(failure, "")
                body = "\n\n".join(paragraph for paragraph in body.split("\n\n") if paragraph)
        return body

    result = reply | {"actions": unique}
    for field in ("body_md", "content"):
        if isinstance(result.get(field), str):
            result[field] = once(result[field])
    if isinstance(result.get("parts"), list):
        result["parts"] = [{**part, "text": once(part["text"])}
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) else part
            for part in result["parts"]]
    return result


def compute_progress(data: dict, job=None) -> dict:
    from app.core.jobs import job_wait
    from app.core.local_mode import is_local

    runtime = data.get("_runtime") or {}
    operations = runtime.get("operations") or {}
    plan = runtime.get("progress_plan") or {}
    steps = {stage: {"planned": len(keys), "completed": sum(
        operations.get(key, {}).get("state") in {"completed", "failed", "uncertain"} for key in keys)}
        for stage, keys in ((stage, plan.get(stage, [])) for stage in LABELS)}
    state = data.get("state", "idle")
    if job and job.status in {"failed_dead", "failed", "poison", "cancelled"}:
        state = "failed"
    wait = job_wait(job) if job else None
    if wait and state not in {"ready", "needs_input", "failed"}:
        state = "queued"
    elif job and job.status in {"leased", "running"} and state == "queued":
        state = "running"
    done = state in {"ready", "needs_input"}
    stage = runtime.get("progress_step", "planning")
    weights = (10, 45, 20, 10, 15)
    weighted = sum(weight * steps[stage]["completed"] // steps[stage]["planned"] if steps[stage]["planned"] else 0
        for stage, weight in zip(LABELS, weights))
    ceiling = sum(weights[:list(LABELS).index(stage) + 1]) if stage in LABELS else 99
    percent = 100 if done else min(99, ceiling, max(runtime.get("progress_percent", 0), weighted))
    label = LABELS.get(stage, LABELS["planning"])
    reads = runtime.get("source_reads") or {}
    if is_local() and reads and any(result["state"] in {"pending", "reading"} for result in reads.values()):
        completed = sum(result["state"] in {"read", "failed"} for result in reads.values())
        label = f"Reading {min(completed + 1, len(reads))} of {len(reads)} sources..."
    elif is_local() and stage == "searching":
        label = "Searching..."
    queued_label = wait["label"] if wait else "Starting soon. Your search is saved but has not started yet."
    if is_local() and "has not started yet" in queued_label:
        queued_label = "Your customer search is queued. Check progress here in a moment."
    return {"state": "done" if done else state, "percent": percent, "run_id": data.get("run_id"),
            "step_label": "Ready to review" if done else "Search stopped" if state == "failed" else
                queued_label if state == "queued" else
                label,
            "started_at": runtime.get("started_at"), "steps": steps}
