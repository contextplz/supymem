"""Read-only completion items for the existing opted-in founder digest."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re

from sqlalchemy import inspect, select

from app.credits._txn import naive_utc
from app.db.models import Job, Person, Venture, Workspace
from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
from app.secrets.detect import looks_like_secret


def completed_contract_digest(task: Task) -> str:
    """Completion events bind the agreed definition, excluding moving estimates."""
    definition = {"workspace_id": task.workspace_id, "venture_id": task.venture_id,
        "owner_person_id": task.owner_person_id, "title": task.title, "goal": task.goal,
        "kind": task.kind, "trigger": json.loads(task.trigger_json),
        "done_test": json.loads(task.done_test_json), "scope": json.loads(task.scope_json),
        "budget": json.loads(task.budget_json), "tolerance": task.tolerance}
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _artifact_digest(session, task, checkpoints):
    from app.tasks.verification import MAX_ARTIFACTS, _artifact, _digest
    artifacts = {}
    for step, evidence in checkpoints:
        if step.runs_on != "server" or step.state != "completed" or not isinstance(evidence, list) or not evidence:
            return None
        for item in evidence:
            text = _artifact(session, task, step, item["ref"])
            if not isinstance(text, str) or not text.strip() or looks_like_secret(text):
                return None
            artifacts[item["ref"]] = text
            if len(artifacts) > MAX_ARTIFACTS:
                return None
    return _digest(sorted(artifacts.items())) if artifacts else None


def completed_artifact_digest(session, task: Task, run: TaskRun) -> str | None:
    """Bind saved server artifacts at completion without any device verification."""
    with session.no_autoflush:
        try:
            steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)).all()
            if not steps or any(step.runs_on != "server" for step in steps):
                return None
            return _artifact_digest(session, task, [(step, json.loads(step.evidence_json)) for step in steps])
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


def _settled(session, task):
    runs = session.scalars(select(TaskRun).where(TaskRun.task_id == task.id)).all()
    if any(run.reserved_cost_micro or run.reserved_tokens
            or json.loads(run.evaluation_json or "{}").get("state") in {"calling", "uncertain"} for run in runs):
        return False
    reserved, settled = set(), set()
    run_ids = {run.id for run in runs}
    events = session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id,
        TaskEvent.kind.in_(("call_reserved", "call_uncertain", "call_settled")))).all()
    for event in events:
        invocation = json.loads(event.data_json).get("invocation")
        if not isinstance(invocation, str) or not invocation or event.run_id not in run_ids:
            return False
        (settled if event.kind == "call_settled" else reserved).add((event.run_id, invocation))
    return reserved <= settled


def _receipt(session, task, step):
    """Only branches whose existing source check is pure may enter this reader."""
    from app.tasks import checks, skills
    if step.runs_on != "server" or step.state != "completed":
        raise ValueError("digest_checkpoint_unavailable")
    if step.job_id:
        job = session.get(Job, step.job_id)
        skill = skills.get_skill(step.skill)
        if not job or job.status not in {"succeeded", "done"} or not skill or skill.runs_on != "server":
            raise ValueError("digest_checkpoint_unavailable")
        receipt = skills.verify_result(session, step.skill, {**json.loads(step.inputs_json), "_job_id": job.id},
            workspace_id=task.workspace_id, venture_id=task.venture_id)
    elif step.skill == "tasks.waitlist_summary":
        from app.tasks.reports import verify_report
        receipt = verify_report(session, task_id=task.id, run_id=step.run_id,
            workspace_id=task.workspace_id, venture_id=task.venture_id)
    elif step.skill == "tasks.reply_draft":
        from app.tasks.drafts import verify_draft
        receipt = verify_draft(session, task_id=task.id, run_id=step.run_id, step_id=step.id,
            workspace_id=task.workspace_id, venture_id=task.venture_id)
    else:
        raise ValueError("digest_checkpoint_unavailable")
    saved = json.loads(step.evidence_json)
    if (not isinstance(saved, list) or not saved or not receipt["verified"]
            or {item["ref"] for item in saved} != {item["ref"] for item in receipt["evidence"]}
            or not checks.passes(json.loads(step.done_test_json), receipt["evidence"])):
        raise ValueError("digest_checkpoint_changed")
    return receipt


def _verified(session, task, run, completion):
    from app.tasks import checks
    from app.tasks.verification import _criteria, _digest
    steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
    # Device/either verification can attach a stop receipt. Skip the entire run.
    if not steps or any(step.runs_on != "server" for step in steps):
        return False
    evidence, checkpoints, quiet = [], [], []
    for step in steps:
        receipt = _receipt(session, task, step)
        evidence.extend(receipt["evidence"])
        checkpoints.append((step, receipt["evidence"]))
        quiet.append(step.strategy_id == "quiet" or receipt.get("quiet") is True)
    artifact_digest = _artifact_digest(session, task, checkpoints)
    if (not artifact_digest or completion.get("artifact_digest") != artifact_digest
            or task.kind != "one_time" and all(quiet)):
        return False
    check = json.loads(task.done_test_json)
    if check.get("type") != "rubric":
        return checks.passes(check, evidence)
    evaluation = json.loads(run.evaluation_json or "{}")
    score = evaluation.get("score")
    return (evaluation.get("state") == "complete" and evaluation.get("valid") is True
        and type(score) in {int, float} and math.isfinite(score) and task.tolerance <= score <= 1
        and evaluation.get("artifact_digest") == artifact_digest
        and evaluation.get("contract_digest") == _digest({"goal": task.goal,
            "criteria": _criteria(task), "tolerance": task.tolerance}))


def _title(task, run):
    if not task.title.strip() or looks_like_secret(task.title) or looks_like_secret(task.goal):
        return None
    title = task.title
    for identifier in (task.id, run.id, task.owner_person_id, task.workspace_id, task.venture_id):
        if identifier:
            title = title.replace(identifier, "saved item")
    title = re.sub(r"(?i)https?://\S+|\S+@\S+|<[^>]*>", "saved item", title)
    title = re.sub(r"(?i)\b(?:[a-z]{1,16}_[a-z0-9_-]+|"
        r"[a-f0-9]{16,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|"
        r"(?=[a-z0-9_-]{24,}\b)(?=[a-z0-9_-]*\d)[a-z0-9_-]+)\b", "saved item", title)
    title = re.sub(r"(?i)\b(?:credits?|tokens?|provider\s*(?:id|body)|error\s*code)\s*[:=]?\s*\S+", "", title)
    title = " ".join(title.replace("\u2014", ", ").split()).strip(" .,:;")[:160]
    return title or "Your task"


def digest_items(session, person_id: str, since: datetime, until: datetime) -> list[str]:
    """Return at most eight current verified completions in (since, until]."""
    since, until = naive_utc(since), naive_utc(until)
    if since >= until:
        return []
    tables = set(inspect(session.connection()).get_table_names())
    if not {model.__tablename__ for model in (Task, TaskRun, TaskStep, TaskEvent)} <= tables:
        return []
    items, seen = [], set()
    # Caller may have unrelated pending changes. Reading cannot flush them.
    with session.no_autoflush:
        # ponytail: inspect the newest 64 events; page only if digest volume warrants it.
        candidates = session.execute(select(Task, TaskRun, TaskEvent)
            .join(TaskRun, TaskRun.task_id == Task.id)
            .join(TaskEvent, (TaskEvent.task_id == Task.id) & (TaskEvent.run_id == TaskRun.id))
            .join(Workspace, Workspace.id == Task.workspace_id)
            .join(Venture, (Venture.id == Task.venture_id) & (Venture.workspace_id == Workspace.id))
            .join(Person, Person.id == Task.owner_person_id)
            .where(Task.owner_person_id == person_id, Workspace.owner_id == person_id,
                Workspace.archived_at.is_(None), Venture.archived_at.is_(None),
                Task.state.in_(("completed", "submitted")), Task.blocker_json.is_(None),
                TaskRun.state == "completed", TaskRun.finished_at.is_not(None), TaskEvent.kind == "completed",
                TaskEvent.created_at > since, TaskEvent.created_at <= until)
            .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc()).limit(64)).all()
        for task, run, event in candidates:
            if task.id in seen:
                continue
            seen.add(task.id)
            try:
                workspace, venture = session.get(Workspace, task.workspace_id), session.get(Venture, task.venture_id)
                if (task.owner_person_id != person_id or task.state not in {"completed", "submitted"}
                        or task.blocker_json or run.state != "completed" or not run.finished_at
                        or not workspace or workspace.owner_id != person_id or workspace.archived_at
                        or not venture or venture.workspace_id != workspace.id or venture.archived_at):
                    continue
                latest = session.scalar(select(TaskRun.id).where(TaskRun.task_id == task.id, TaskRun.state != "queued")
                    .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(1))
                completion = json.loads(event.data_json)
                if (latest != run.id or naive_utc(run.finished_at) > until
                        or completion.get("contract_digest") != completed_contract_digest(task)
                        or not _settled(session, task) or not _verified(session, task, run, completion)):
                    continue
                title = _title(task, run)
                if title:
                    items.append(f"Finished: {title}. Open Supymem to review the saved result.")
                    if len(items) == 8:
                        break
            except (AttributeError, KeyError, TypeError, ValueError):
                continue  # Invalid or changed private receipts never become email copy.
    return items
