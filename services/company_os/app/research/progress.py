"""Public progress from the frozen query plan and durable operation receipts."""
from __future__ import annotations

import json

from core.clock import as_utc

from app.db.supymem_models import ResearchDossier
from app.research.execution import identifier


def step_label(key: str) -> str:
    if key.startswith(("review", "questions")):
        return "Writing your report" if key.startswith("review") else "Preparing your questions"
    if key == "prior-art-plan":
        return "Planning the research"
    if key.startswith(("prior_complaints", "customer_voice", "customer_request", "workaround", "prior-hn")):
        return "Reading customer complaints"
    if key.startswith(("prior_failures", "graveyard")):
        return "Checking earlier attempts"
    if key.startswith("scrape_"):
        return "Reading source pages"
    if key == "services":
        return "Checking services and APIs"
    if key.startswith(("github", "oss")):
        return "Checking open-source projects"
    if key in {"pricing", "buyer_budget"}:
        return "Checking pricing"
    return "Searching for similar products"


def compute_progress(job, runtime: dict | None) -> dict:
    operations = (runtime or {}).get("operations", {})
    plan = (runtime or {}).get("progress_plan")
    done = job.status in {"succeeded", "done"}
    percent = None
    if plan:
        # ponytail: discovered follow-ups expand the denominator; freeze an eager plan if needed.
        keys = list(dict.fromkeys([*plan, *operations]))
        completed = sum(operations.get(key, {}).get("state") == "completed" for key in keys)
        percent = min(99, completed * 100 // len(keys))
    pending = [key for key, receipt in operations.items()
               if receipt.get("state") not in {"completed", "failed"}]
    remaining = [key for key in plan or [] if operations.get(key, {}).get("state") != "completed"]
    queued = job.status in {"scheduled", "pending", "queued", "retry_wait"} and not operations
    if done:
        label = "Complete"
    elif queued:
        label = "Queued"
    elif pending or remaining:
        label = step_label(pending[-1] if pending else remaining[0])
    else:
        label = "Saving your report" if plan else "Research in progress"
    return {"state": "done" if done else "queued" if queued else job.status,
            "percent": 100 if done else 0 if queued else percent,
            "step_label": label,
            "started_at": (runtime or {}).get("started_at") or (as_utc(job.started_at).isoformat() if job.started_at else None)}


def job_progress(session, job) -> dict | None:
    if job.kind not in {"idea.validate", "research.deep"}:
        return None
    row = session.get(ResearchDossier, identifier(job.id))
    runtime = None
    if row and (row.workspace_id, row.venture_id) == (job.workspace_id, job.venture_id):
        try:
            document = json.loads(row.summary)
            candidate = document.get("_runtime") if isinstance(document, dict) else None
            if isinstance(candidate, dict) and candidate.get("job_id") == job.id:
                runtime = candidate
        except (ValueError, TypeError):
            pass
    return compute_progress(job, runtime)
